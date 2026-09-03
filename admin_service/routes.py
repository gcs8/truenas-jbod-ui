from __future__ import annotations

# Handler globals are populated from admin_service.main by MainModuleAPIRouter.
# pyright: reportUndefinedVariable=false
# ruff: noqa: F821

from types import ModuleType
from typing import Any

from app.route_compat import MainModuleAPIRouter


def build_router(main_module: ModuleType, admin_settings: Any) -> MainModuleAPIRouter:
    router = MainModuleAPIRouter(main_module, globals())

    async def container_action_response(
        container_key: str,
        *,
        action: str,
        admin_detail: str,
    ) -> JSONResponse:
        if container_key == "admin":
            raise HTTPException(status_code=400, detail=admin_detail)
        runtime_service = get_runtime_service()
        try:
            await asyncio.to_thread(getattr(runtime_service, f"{action}_container"), container_key)
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "runtime": await build_runtime_payload(runtime_service)})

    def export_file_response(artifact: Any, headers: dict[str, str]) -> Response:
        try:
            return TemporaryFileResponse(
                path=artifact.path,
                media_type=artifact.media_type or "application/octet-stream",
                headers=headers,
                cleanup=artifact.cleanup,
            )
        except Exception:
            artifact.cleanup()
            raise

    async def config_mutation_response(content: dict[str, Any]) -> JSONResponse:
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        content["restart_required"] = ["ui"]
        content["runtime"] = await build_runtime_payload(runtime_service)
        return JSONResponse(content)

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        bootstrap = await build_admin_state_payload(request)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "admin_bootstrap_json": json.dumps(bootstrap),
            },
        )

    @router.get("/api/admin/state")
    async def get_admin_state(request: Request) -> JSONResponse:
        return JSONResponse(await build_admin_state_payload(request))

    @router.get("/api/admin/runtime")
    async def get_admin_runtime() -> JSONResponse:
        runtime_payload = await build_runtime_payload()
        return JSONResponse({"ok": True, "runtime": project_runtime_observation(runtime_payload)})

    @router.post("/api/admin/runtime-behavior")
    async def update_runtime_behavior(payload: dict[str, Any]) -> JSONResponse:
        settings = reload_app_settings()
        values = payload.get("values") if isinstance(payload, dict) else None
        try:
            runtime_behavior = await asyncio.to_thread(
                save_runtime_behavior_overrides,
                settings,
                values if isinstance(values, dict) else {},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        return JSONResponse(
            {
                "ok": True,
                "runtime_behavior": runtime_behavior,
                "runtime": await build_runtime_payload(runtime_service),
                "detail": "Runtime behavior overrides saved. Restart the Read UI container to apply them.",
            }
        )

    @router.post("/api/admin/runtime/containers/{container_key}/stop")
    async def stop_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="stop",
            admin_detail="The admin sidecar cannot stop itself from the UI.",
        )

    @router.post("/api/admin/runtime/containers/{container_key}/start")
    async def start_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="start",
            admin_detail="The admin sidecar is already running.",
        )

    @router.post("/api/admin/runtime/containers/{container_key}/restart")
    async def restart_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="restart",
            admin_detail="The admin sidecar cannot restart itself from the UI.",
        )

    @router.post("/api/admin/backup/export")
    async def export_backup(
        payload: SystemBackupExportRequest,
        stop_services: bool = Query(default=False),
        restart_services: bool = Query(default=True),
    ) -> Response:
        try:
            validate_admin_export_policy(
                admin_settings,
                encrypt=payload.encrypt,
                scrub_secrets=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        maintenance_service = get_maintenance_service()
        try:
            artifact, maintenance = await run_file_export_worker(
                maintenance_service.export_bundle,
                payload,
                stop_services=stop_services,
                restart_services=restart_services,
            )
        except (ValueError, DockerRuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        headers = {
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-Backup-Encrypted": "true" if payload.encrypt else "false",
            "X-Backup-Packaging": str(artifact.manifest.get("packaging") or payload.packaging),
            "X-Backup-Schema-Version": str(artifact.manifest.get("schema_version") or 1),
            "X-Admin-Stopped-Containers": ",".join(maintenance.stopped_containers),
            "X-Admin-Restarted-Containers": ",".join(maintenance.restarted_containers),
            "X-Admin-Restart-Failures": ",".join(maintenance.restart_failures),
        }
        return export_file_response(artifact, headers)

    @router.post("/api/admin/debug/export")
    async def export_debug_bundle(
        payload: DebugBundleExportRequest,
        stop_services: bool = Query(default=True),
        restart_services: bool = Query(default=True),
    ) -> Response:
        try:
            validate_admin_export_policy(
                admin_settings,
                encrypt=payload.encrypt,
                scrub_secrets=payload.scrub_secrets,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        maintenance_service = get_maintenance_service()
        try:
            artifact, maintenance = await run_file_export_worker(
                maintenance_service.export_debug_bundle,
                payload,
                stop_services=stop_services,
                restart_services=restart_services,
            )
        except (ValueError, DockerRuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        headers = {
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
            "X-Debug-Encrypted": "true" if payload.encrypt else "false",
            "X-Debug-Packaging": str(artifact.manifest.get("packaging") or payload.packaging),
            "X-Debug-Schema-Version": str(artifact.manifest.get("schema_version") or 1),
            "X-Debug-Scrubbed": "true" if (payload.scrub_secrets or payload.scrub_disk_identifiers) else "false",
            "X-Debug-Scrub-Secrets": "true" if payload.scrub_secrets else "false",
            "X-Debug-Scrub-Disk-Identifiers": "true" if payload.scrub_disk_identifiers else "false",
            "X-Admin-Stopped-Containers": ",".join(maintenance.stopped_containers),
            "X-Admin-Restarted-Containers": ",".join(maintenance.restarted_containers),
            "X-Admin-Restart-Failures": ",".join(maintenance.restart_failures),
        }
        return export_file_response(artifact, headers)

    @router.post("/api/admin/backup/inspect")
    @observe_backup_route("inspect")
    async def inspect_backup(request: Request) -> JSONResponse:
        archive_path = await stream_limited_request_body_to_file(
            request,
            body_description="Backup inspection",
        )
        try:
            if archive_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Backup inspection request body was empty.")
            try:
                passphrase = decode_optional_secret_header(
                    request.headers.get("X-Backup-Passphrase-Base64")
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if passphrase is None:
                passphrase = request.headers.get("X-Backup-Passphrase") or None
            try:
                result = await asyncio.to_thread(
                    get_backup_service().inspect_bundle_file,
                    archive_path,
                    passphrase=passphrase,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(result)
        finally:
            archive_path.unlink(missing_ok=True)
            archive_path.parent.rmdir()

    @router.post("/api/admin/backup/import")
    @observe_backup_route("import")
    async def import_backup(
        request: Request,
        stop_services: bool = Query(default=True),
        restart_services: bool = Query(default=True),
    ) -> JSONResponse:
        archive_path = await stream_limited_request_body_to_file(request)
        try:
            if archive_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="Backup import request body was empty.")
            try:
                passphrase = decode_optional_secret_header(
                    request.headers.get("X-Backup-Passphrase-Base64")
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if passphrase is None:
                passphrase = request.headers.get("X-Backup-Passphrase") or None
            maintenance_service = get_maintenance_service()
            try:
                result, maintenance = await asyncio.to_thread(
                    maintenance_service.import_bundle_from_file,
                    archive_path,
                    passphrase=passphrase,
                    stop_services=stop_services,
                    restart_services=restart_services,
                )
            except (ValueError, DockerRuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            settings = reload_app_settings()
            runtime_service = get_runtime_service()
            impacted = tuple(
                key for key in admin_settings.clean_backup_targets
                if key in runtime_service.managed_containers
            )
            restarted = tuple(
                key for key in maintenance.restarted_containers
                if key in impacted
            )
            await asyncio.to_thread(runtime_service.clear_restart_required, restarted)
            await asyncio.to_thread(
                runtime_service.mark_restart_required,
                tuple(key for key in impacted if key not in restarted),
            )
            return JSONResponse(
                {
                    **result,
                    "systems": serialize_systems(settings),
                    "default_system_id": settings.default_system_id,
                    "stopped_containers": maintenance.stopped_containers,
                    "restarted_containers": maintenance.restarted_containers,
                    "restart_failures": dict(maintenance.restart_failures),
                    "runtime": await build_runtime_payload(runtime_service),
                }
            )
        finally:
            archive_path.unlink(missing_ok=True)
            archive_path.parent.rmdir()

    @router.post("/api/admin/esxi-host-prep/upload")
    async def upload_esxi_host_prep_package(
        request: Request,
        filename: str = Query(..., min_length=1),
    ) -> JSONResponse:
        upload_path = await stream_limited_request_body_to_file(
            request,
            max_bytes=MAX_ESXI_HOST_PREP_UPLOAD_BYTES,
            body_description="ESXi host-prep upload",
        )
        try:
            if upload_path.stat().st_size == 0:
                raise HTTPException(status_code=400, detail="ESXi host-prep upload request body was empty.")
            content = await asyncio.to_thread(upload_path.read_bytes)
            service = get_esxi_host_prep_service()
            try:
                package = await asyncio.to_thread(service.stage_package, filename, content)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(
                {
                    "ok": True,
                    "package": package,
                    "packages": await asyncio.to_thread(service.list_staged_packages),
                }
            )
        finally:
            upload_path.unlink(missing_ok=True)
            upload_path.parent.rmdir()

    @router.post("/api/admin/esxi-host-prep/install")
    async def install_esxi_host_prep_package(payload: ESXiHostPrepInstallRequest) -> JSONResponse:
        settings = reload_app_settings()
        try:
            payload = payload.model_copy(
                update={
                    "password": resolve_saved_secondary_secret(
                        settings,
                        payload.system_id,
                        payload.password,
                        lambda system: system.ssh.password,
                        lambda system: (
                            system.truenas.platform == "esxi"
                            and same_saved_endpoint(payload.host, system.ssh.host)
                            and payload.port == system.ssh.port
                            and same_saved_text(payload.user, system.ssh.user)
                            and payload.strict_host_key_checking
                            == system.ssh.strict_host_key_checking
                        ),
                    )
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service = get_esxi_host_prep_service()
        try:
            result = await asyncio.to_thread(
                service.install_package,
                payload,
                known_hosts_path=settings.ssh.known_hosts_path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "install_ok": bool(result.get("ok")),
                **{key: value for key, value in result.items() if key != "ok"},
                "packages": await asyncio.to_thread(service.list_staged_packages),
            }
        )

    @router.get("/api/admin/ssh-keys")
    async def list_ssh_keys() -> JSONResponse:
        settings = reload_app_settings()
        key_manager = SSHKeyManager(settings.config_file)
        keys = await asyncio.to_thread(key_manager.list_keys)
        return JSONResponse({"ok": True, "keys": keys})

    @router.post("/api/admin/ssh-keys/generate")
    async def generate_ssh_key(payload: SSHKeyGenerateRequest) -> JSONResponse:
        settings = reload_app_settings()
        key_manager = SSHKeyManager(settings.config_file)
        try:
            generated_key = await asyncio.to_thread(key_manager.generate_keypair, payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        keys = await asyncio.to_thread(key_manager.list_keys)
        return JSONResponse({"ok": True, "key": generated_key, "keys": keys})

    @router.post("/api/admin/tls/inspect")
    async def inspect_tls_certificate(payload: TLSCertificateInspectRequest) -> JSONResponse:
        settings = reload_app_settings()
        trust_service = TLSTrustStoreService(settings.config_file)
        try:
            inspection = await asyncio.to_thread(
                trust_service.inspect_remote_certificate,
                payload.host,
                payload.timeout_seconds,
                tls_server_name=payload.tls_server_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "inspection": inspection})

    @router.post("/api/admin/tls/import")
    async def import_tls_bundle(payload: TLSCertificateImportRequest) -> JSONResponse:
        settings = reload_app_settings()
        trust_service = TLSTrustStoreService(settings.config_file)
        try:
            imported = await asyncio.to_thread(
                trust_service.import_pem_bundle,
                payload.pem_text,
                bundle_name=payload.bundle_name,
                system_id=payload.system_id,
                host=payload.host,
            )
            validation = None
            if payload.host:
                validation = await asyncio.to_thread(
                    trust_service.validate_bundle_for_host,
                    payload.host,
                    imported["bundle_path"],
                    tls_server_name=payload.tls_server_name,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **imported, "validation": validation})

    @router.post("/api/admin/tls/trust-remote")
    async def trust_remote_tls_certificate(payload: TLSRemoteCertificateTrustRequest) -> JSONResponse:
        settings = reload_app_settings()
        trust_service = TLSTrustStoreService(settings.config_file)
        try:
            trusted = await asyncio.to_thread(
                trust_service.trust_remote_certificate,
                payload.host,
                timeout_seconds=payload.timeout_seconds,
                bundle_name=payload.bundle_name,
                system_id=payload.system_id,
                tls_server_name=payload.tls_server_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **trusted})

    @router.post("/api/admin/system-setup/quantastor-nodes")
    async def discover_quantastor_nodes(payload: QuantastorNodeDiscoveryRequest) -> JSONResponse:
        settings = reload_app_settings()
        try:
            payload = payload.model_copy(
                update={
                    "api_password": resolve_saved_secondary_secret(
                        settings,
                        payload.system_id,
                        payload.api_password,
                        lambda system: system.truenas.api_password,
                        lambda system: (
                            system.truenas.platform == "quantastor"
                            and same_saved_endpoint(
                                payload.truenas_host,
                                system.truenas.host,
                            )
                            and same_saved_text(
                                payload.api_user,
                                system.truenas.api_user,
                            )
                            and payload.verify_ssl == system.truenas.verify_ssl
                            and same_saved_text(
                                payload.tls_ca_bundle_path,
                                system.truenas.tls_ca_bundle_path,
                            )
                            and same_saved_text(
                                payload.tls_server_name,
                                system.truenas.tls_server_name,
                            )
                        ),
                    ),
                    "ssh_password": resolve_saved_secondary_secret(
                        settings,
                        payload.system_id,
                        payload.ssh_password,
                        lambda system: system.ssh.password,
                        lambda system: (
                            system.truenas.platform == "quantastor"
                            and system.ssh.enabled
                            and same_saved_endpoint(
                                payload.ssh_host,
                                system.ssh.host,
                            )
                            and payload.ssh_port == system.ssh.port
                            and same_saved_text(
                                payload.ssh_user,
                                system.ssh.user,
                            )
                            and payload.ssh_strict_host_key_checking
                            == system.ssh.strict_host_key_checking
                        ),
                    ),
                }
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        client = QuantastorRESTClient(
            TrueNASConfig(
                host=payload.truenas_host,
                api_user=payload.api_user,
                api_password=payload.api_password,
                platform="quantastor",
                verify_ssl=payload.verify_ssl,
                tls_ca_bundle_path=payload.tls_ca_bundle_path,
                tls_server_name=payload.tls_server_name,
                timeout_seconds=payload.timeout_seconds,
            )
        )
        try:
            raw_data = await client.fetch_all()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface discovery failures directly in setup.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        nodes = serialize_quantastor_nodes(raw_data)
        merge_quantastor_node_hosts(nodes, quantastor_request_node_host_map(payload))
        host_discovery = await enrich_quantastor_nodes_from_ssh(
            payload,
            raw_data,
            nodes,
            known_hosts_path=settings.ssh.known_hosts_path,
        )
        return JSONResponse({"ok": True, "nodes": nodes, "host_discovery": host_discovery})

    @router.post("/api/admin/system-setup")
    async def create_system(payload: SystemSetupRequest) -> JSONResponse:
        settings = reload_app_settings()
        setup_service = SystemSetupService(settings.config_file)
        try:
            saved_system, updated_existing = await asyncio.to_thread(setup_service.save_system, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        return await config_mutation_response(
            {
                "ok": True,
                "system": {
                    "id": saved_system.id,
                    "label": saved_system.label,
                    "platform": saved_system.truenas.platform,
                },
                "systems": serialize_systems(refreshed_settings),
                "default_system_id": refreshed_settings.default_system_id,
                "detail": (
                    "Config updated. Restart the Read UI container to pick up the revised system."
                    if updated_existing
                    else "Config saved. Restart the Read UI container to pick up the new system."
                ),
                "updated_existing": updated_existing,
            }
        )

    @router.post("/api/admin/system-setup/demo")
    async def create_demo_system(payload: DemoSystemRequest | None = None) -> JSONResponse:
        settings = reload_app_settings()
        demo_factory = DemoSystemFactory(settings.config_file, settings.paths.profile_file)
        try:
            result = await asyncio.to_thread(
                demo_factory.create_demo_system,
                payload or DemoSystemRequest(),
                settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        saved_system = result["system"]
        saved_profile = result["profile"]
        return await config_mutation_response(
            {
                "ok": True,
                "system": {
                    "id": saved_system.id,
                    "label": saved_system.label,
                    "platform": saved_system.truenas.platform,
                },
                "profile": {
                    "id": saved_profile.id,
                    "label": saved_profile.label,
                },
                "systems": serialize_systems(refreshed_settings),
                "profiles": serialize_profiles(refreshed_settings),
                "default_system_id": refreshed_settings.default_system_id,
                "updated_existing": bool(result.get("updated_existing")),
                "updated_profile": bool(result.get("updated_profile")),
                "detail": (
                    f"Demo builder system {saved_system.label} saved. Restart the Read UI container to pick the synthetic chassis and views up cleanly."
                ),
            }
        )

    @router.delete("/api/admin/system-setup/{system_id}")
    async def delete_system(system_id: str, purge_history: bool = False) -> JSONResponse:
        settings = reload_app_settings()
        setup_service = SystemSetupService(settings.config_file)
        try:
            deleted_label, next_default_id = await asyncio.to_thread(setup_service.delete_system, system_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        history_purge: dict[str, Any] = {
            "requested": purge_history,
            "ok": True,
            "summary": None,
            "detail": "Saved history left in place.",
        }
        if purge_history:
            history_store = get_history_store()
            try:
                purge_summary = await asyncio.to_thread(history_store.delete_system_history, system_id)
                if purge_summary["total_rows"]:
                    purge_detail = (
                        f"Purged {_format_count(int(purge_summary['total_rows']), 'saved history row')} "
                        f"({format_history_cleanup_summary(purge_summary)})."
                    )
                else:
                    purge_detail = f"No saved history rows matched {system_id}."
                history_purge = {
                    "requested": True,
                    "ok": True,
                    "summary": purge_summary,
                    "detail": purge_detail,
                }
            except Exception:  # noqa: BLE001 - config delete already succeeded, so surface purge failure as warning payload.
                logger.exception("History purge failed after deleting saved system %s", system_id)
                history_purge = {
                    "requested": True,
                    "ok": False,
                    "summary": None,
                    "detail": "Saved history purge failed; see admin logs.",
                }

        refreshed_settings = reload_app_settings()
        detail = f"Removed {deleted_label}."
        if purge_history:
            detail = f"{detail} {history_purge['detail']}"
        detail = f"{detail} Restart the Read UI container to drop the deleted system from the live runtime."
        return await config_mutation_response(
            {
                "ok": True,
                "system_id": system_id,
                "deleted_label": deleted_label,
                "systems": serialize_systems(refreshed_settings),
                "default_system_id": next_default_id,
                "detail": detail,
                "history_purge": history_purge,
            }
        )

    @router.post("/api/admin/history/purge-orphaned")
    async def purge_orphaned_history() -> JSONResponse:
        settings = reload_app_settings()
        valid_system_ids = [system.id for system in settings.systems]
        history_store = get_history_store()
        try:
            summary = await asyncio.to_thread(history_store.purge_orphaned_history, valid_system_ids)
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            logger.exception("Unable to purge orphaned history")
            raise HTTPException(status_code=500, detail="Unable to purge orphaned history; see admin logs.") from exc

        removed_system_ids = list(summary.get("removed_system_ids") or [])
        if summary["total_rows"]:
            removed_text = ", ".join(removed_system_ids)
            detail = (
                f"Purged orphaned history for {removed_text}: "
                f"{_format_count(int(summary['total_rows']), 'saved history row')} "
                f"({format_history_cleanup_summary(summary)})."
            )
        else:
            detail = "No orphaned history rows matched the current config."
        return JSONResponse(
            {
                "ok": True,
                "detail": detail,
                "summary": summary,
                "valid_system_ids": valid_system_ids,
            }
        )

    @router.get("/api/admin/history/orphaned")
    async def list_orphaned_history() -> JSONResponse:
        settings = reload_app_settings()
        valid_system_ids = [system.id for system in settings.systems]
        history_store = get_history_store()
        try:
            orphaned_systems = await asyncio.to_thread(
                history_store.list_history_system_summaries,
                valid_system_ids,
            )
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            logger.exception("Unable to inspect orphaned history")
            raise HTTPException(status_code=500, detail="Unable to inspect orphaned history; see admin logs.") from exc

        return JSONResponse(
            {
                "ok": True,
                "orphaned_systems": orphaned_systems,
                "valid_system_ids": valid_system_ids,
            }
        )

    @router.post("/api/admin/history/adopt-removed-system")
    async def adopt_removed_system_history(payload: HistoryAdoptRequest) -> JSONResponse:
        settings = reload_app_settings()
        valid_system_ids = [system.id for system in settings.systems]
        source_system_id = normalize_text(payload.source_system_id)
        target_system_id = normalize_text(payload.target_system_id)
        if not source_system_id:
            raise HTTPException(status_code=400, detail="Source system id is required.")
        if not target_system_id:
            raise HTTPException(status_code=400, detail="Target system id is required.")
        if source_system_id == target_system_id:
            raise HTTPException(status_code=400, detail="Source and target system ids must be different.")

        target_system = next((system for system in settings.systems if system.id == target_system_id), None)
        if target_system is None:
            raise HTTPException(status_code=400, detail=f"Target system {target_system_id} is not in the saved config.")

        history_store = get_history_store()
        try:
            orphaned_systems = await asyncio.to_thread(
                history_store.list_history_system_summaries,
                valid_system_ids,
            )
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            logger.exception("Unable to inspect orphaned history before adoption")
            raise HTTPException(status_code=500, detail="Unable to inspect orphaned history; see admin logs.") from exc

        source_summary = next(
            (summary for summary in orphaned_systems if summary.get("system_id") == source_system_id),
            None,
        )
        if source_summary is None:
            raise HTTPException(
                status_code=400,
                detail=f"Source system {source_system_id} is not currently orphaned history.",
            )

        try:
            summary = await asyncio.to_thread(
                history_store.adopt_system_history,
                source_system_id,
                target_system_id,
                target_system_label=target_system.label,
            )
            remaining_orphaned_systems = await asyncio.to_thread(
                history_store.list_history_system_summaries,
                valid_system_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            logger.exception("Unable to adopt removed system history")
            raise HTTPException(status_code=500, detail="Unable to adopt removed system history; see admin logs.") from exc

        if summary["total_rows"]:
            detail = (
                f"Adopted {format_history_system_summary(source_summary)} into "
                f"{target_system.label}. Refresh an open History drawer to pull the updated rows."
            )
            if int(summary.get("slot_state_conflicts", 0) or 0) > 0:
                detail = (
                    f"{detail} Kept {_format_count(int(summary['slot_state_conflicts']), 'current-slot row')} "
                    "already present on the target where scopes overlapped."
                )
        else:
            detail = f"No saved history rows matched {source_system_id}."

        return JSONResponse(
            {
                "ok": True,
                "detail": detail,
                "summary": summary,
                "source": source_summary,
                "target_system_id": target_system.id,
                "target_system_label": target_system.label,
                "orphaned_systems": remaining_orphaned_systems,
                "valid_system_ids": valid_system_ids,
            }
        )

    @router.post("/api/admin/system-setup/bootstrap")
    async def bootstrap_service_account(payload: SystemSetupBootstrapRequest) -> JSONResponse:
        settings = reload_app_settings()
        bootstrap_service = ServiceAccountBootstrapService(settings.config_file)
        try:
            result = await asyncio.to_thread(bootstrap_service.bootstrap_service_account, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @router.post("/api/admin/system-setup/sudoers-preview")
    async def preview_sudoers_file(payload: SystemSetupSudoPreviewRequest) -> JSONResponse:
        try:
            result = await asyncio.to_thread(
                ServiceAccountBootstrapService.build_sudoers_preview,
                payload.service_user,
                payload.platform,
                install_sudo_rules=payload.install_sudo_rules,
                requested_commands=payload.sudo_commands,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **result})

    @router.get("/api/admin/storage-views/candidates")
    async def list_storage_view_candidates(
        system_id: str | None = None,
        target_system_id: str | None = None,
        force: bool = Query(default=False),
    ) -> JSONResponse:
        settings = reload_app_settings()
        registry = InventoryRegistry(settings)
        service = registry.get_service(system_id)
        try:
            candidates = await service.get_storage_view_candidates(
                force_refresh=force,
                target_system_id=target_system_id,
            )
        except Exception as exc:  # noqa: BLE001 - surface inventory issues as an admin-side error.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "system_id": service.system.id,
                "candidates": candidates,
            }
        )

    @router.get("/api/admin/storage-views/live-enclosures")
    async def list_storage_view_live_enclosures(
        system_id: str | None = None,
        force: bool = Query(default=False),
    ) -> JSONResponse:
        settings = reload_app_settings()
        registry = InventoryRegistry(settings)
        service = registry.get_service(system_id)
        try:
            snapshot = await service.get_snapshot(force_refresh=force)
        except Exception as exc:  # noqa: BLE001 - surface inventory issues as an admin-side error.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(
            {
                "ok": True,
                "system_id": service.system.id,
                "enclosures": serialize_live_enclosures(service, snapshot.enclosures),
            }
        )

    @router.post("/api/admin/profiles")
    async def save_profile(payload: EnclosureProfileRequest) -> JSONResponse:
        settings = reload_app_settings()
        profile_service = ProfileBuilderService(settings.config_file, settings.paths.profile_file)
        try:
            saved_profile, updated_existing = await asyncio.to_thread(
                profile_service.save_profile,
                payload,
                settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        serialized_profiles = serialize_profiles(refreshed_settings)
        serialized_profile = next(
            (profile for profile in serialized_profiles if profile["id"] == saved_profile.id),
            None,
        )
        return await config_mutation_response(
            {
                "ok": True,
                "profile": serialized_profile,
                "profiles": serialized_profiles,
                "detail": (
                    "Custom enclosure profile updated. Restart the Read UI container to pick up the revised profile."
                    if updated_existing
                    else "Custom enclosure profile saved. Restart the Read UI container to pick up the new profile."
                ),
                "updated_existing": updated_existing,
            }
        )

    @router.delete("/api/admin/profiles/{profile_id}")
    async def delete_profile(profile_id: str) -> JSONResponse:
        settings = reload_app_settings()
        profile_service = ProfileBuilderService(settings.config_file, settings.paths.profile_file)
        try:
            deleted_label = await asyncio.to_thread(profile_service.delete_profile, profile_id, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        return await config_mutation_response(
            {
                "ok": True,
                "profile_id": profile_id,
                "deleted_label": deleted_label,
                "profiles": serialize_profiles(refreshed_settings),
                "detail": (
                    f"Deleted custom profile {deleted_label}. Restart the Read UI container when you are ready to drop it from the runtime profile list too."
                ),
            }
        )

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "started_at": SERVICE_STARTED_AT.isoformat(),
                "expires_at": compute_expires_at(get_admin_settings()).isoformat()
                if compute_expires_at(get_admin_settings())
                else None,
            }
        )

    @router.get("/livez")
    async def livez() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
            },
            status_code=200,
        )


    return router
