from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import iterate_in_threadpool

from admin_service.config import get_admin_settings
from admin_service.route_support import (
    SERVICE_STARTED_AT,
    TemporaryFileResponse,
    _format_count,
    build_admin_state_payload,
    build_runtime_payload,
    compute_expires_at,
    decode_optional_secret_header,
    enrich_quantastor_nodes_from_ssh,
    format_history_cleanup_summary,
    format_history_system_summary,
    get_backup_receipt_store,
    get_backup_scheduler_client,
    get_backup_service,
    get_esxi_host_prep_service,
    get_history_store,
    get_maintenance_service,
    get_runtime_service,
    limited_request_content_length,
    logger,
    merge_quantastor_node_hosts,
    observe_backup_route,
    project_runtime_observation,
    quantastor_node_discovery_seed_hosts,
    quantastor_request_node_host_map,
    reload_app_settings,
    resolve_saved_secondary_secret,
    run_file_export_worker,
    serialize_live_enclosures,
    serialize_profiles,
    serialize_quantastor_nodes,
    serialize_systems,
    stream_limited_request_body_to_file,
    templates,
    validate_admin_export_policy,
)
from admin_service.services.account_bootstrap import (
    ServiceAccountBootstrapService,
    saved_sudo_commands_for_system,
)
from admin_service.services.backup_scheduler_client import SchedulerUnavailableError
from admin_service.services.esxi_host_prep import (
    MAX_UPLOAD_BYTES as MAX_ESXI_HOST_PREP_UPLOAD_BYTES,
)
from admin_service.services.esxi_host_prep import (
    STAGING_QUOTA_ERROR,
    HostPrepStagingQuotaError,
)
from admin_service.services.runtime_control import DockerRuntimeError
from admin_service.services.tls_trust import TLSTrustStoreService
from app import __version__
from app.config import (
    TrueNASConfig,
    known_hosts_path_for_target,
    save_runtime_behavior_overrides,
)
from app.models.domain import (
    DebugBundleExportRequest,
    DemoSystemRequest,
    EnclosureProfileRequest,
    ESXiHostPrepInstallRequest,
    HistoryAdoptRequest,
    QuantastorNodeDiscoveryRequest,
    SSHKeyGenerateRequest,
    SystemBackupExportRequest,
    SystemSetupBootstrapRequest,
    SystemSetupRequest,
    SystemSetupSudoPreviewRequest,
    TLSCertificateImportRequest,
    TLSCertificateInspectRequest,
    TLSRemoteCertificateTrustRequest,
)
from app.services.config_change_journal import record_config_change
from app.services.credential_authority import (
    api_credential_authority,
    credential_authorities_are_approved,
    same_credential_authority,
    ssh_credential_authorities,
)
from app.services.demo_system_factory import DemoSystemFactory
from app.services.inventory_registry import InventoryRegistry
from app.services.parsers import normalize_text
from app.services.profile_builder import ProfileBuilderService
from app.services.quantastor_api import QuantastorRESTClient
from app.services.ssh_key_manager import SSHKeyManager
from app.services.system_setup import _CONFIG_WRITE_LOCK, SystemSetupService


def build_router(admin_settings: Any) -> APIRouter:
    router = APIRouter()
    # Bounded, short-lived, one-use proof of the exact preview shown to the operator.
    purge_previews: dict[str, tuple[float, list[str], list[dict[str, Any]]]] = {}

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

    async def run_retained_thread_worker(
        function: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            await asyncio.wait((operation,))
        except asyncio.CancelledError as cancellation:
            while not operation.done():
                try:
                    await asyncio.wait((operation,))
                except asyncio.CancelledError:
                    continue
            if not operation.cancelled():
                operation.exception()
            raise cancellation
        return operation.result()

    def expected_backup_encryption_mode(request: Request) -> str:
        mode = request.headers.get("X-Backup-Expected-Encryption", "")
        if mode not in {"encrypted", "plaintext"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    "X-Backup-Expected-Encryption must be exactly encrypted or plaintext."
                ),
            )
        return mode

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
        await asyncio.to_thread(record_config_change, "runtime_overrides.save", ",".join(sorted(values or {})))

        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        return JSONResponse(
            {
                "ok": True,
                "runtime_behavior": runtime_behavior,
                "runtime": await build_runtime_payload(runtime_service),
                "restart_required": ["ui"],
                "detail": "Timing saved. Restart the main UI to apply it.",
            }
        )

    @router.post("/api/admin/runtime/containers/{container_key}/stop")
    async def stop_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="stop",
            admin_detail="Admin can't stop itself from this page.",
        )

    @router.post("/api/admin/runtime/containers/{container_key}/start")
    async def start_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="start",
            admin_detail="Admin is already running.",
        )

    @router.post("/api/admin/runtime/containers/{container_key}/restart")
    async def restart_container(container_key: str) -> JSONResponse:
        return await container_action_response(
            container_key,
            action="restart",
            admin_detail="Admin can't restart itself from this page.",
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
        stop_services: bool = Query(default=False),
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

    async def upload_archive_source(request: Request, body_description: str) -> Path:
        return await stream_limited_request_body_to_file(request, body_description=body_description)

    @router.post("/api/admin/backup/inspect")
    @observe_backup_route("inspect")
    async def inspect_backup(request: Request) -> JSONResponse:
        return await inspect_archive(request, upload_archive_source)

    async def inspect_archive(request: Request, archive_source: Any) -> JSONResponse:
        archive_path = await archive_source(request, "Backup inspection")
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
            def inspect_and_issue_receipt() -> tuple[dict[str, Any], dict[str, Any]]:
                issued: dict[str, Any] = {}

                def issue_for_identity(archive_digest: str, mode: str) -> None:
                    issued.update(
                        get_backup_receipt_store().issue_digest(
                            archive_digest,
                            observed_encryption_mode=mode,
                        )
                    )

                result = get_backup_service().inspect_bundle_file(
                    archive_path,
                    passphrase=passphrase,
                    identity_callback=issue_for_identity,
                )
                if not issued:
                    raise RuntimeError("Backup inspection identity was not bound.")
                return result, issued

            try:
                result, issued = await run_retained_thread_worker(
                    inspect_and_issue_receipt,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            encryption_mode = "encrypted" if result.get("encrypted") is True else "plaintext"
            return JSONResponse(
                {
                    **result,
                    "encryption_mode": encryption_mode,
                    "inspection_receipt": issued["receipt"],
                    "inspection_receipt_expires_at": issued["expires_at"],
                }
            )
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
        return await import_archive(request, upload_archive_source, stop_services, restart_services)

    async def import_archive(
        request: Request,
        archive_source: Any,
        stop_services: bool,
        restart_services: bool,
    ) -> JSONResponse:
        admission_started_at = int(time.time())
        expected_mode = expected_backup_encryption_mode(request)
        receipt = request.headers.get("X-Backup-Inspection-Receipt", "")
        if not receipt:
            raise HTTPException(
                status_code=400,
                detail="X-Backup-Inspection-Receipt is required before import.",
            )
        receipt_store = get_backup_receipt_store()
        admission: str | None = None
        try:
            try:
                admission = receipt_store.begin_admission(
                    receipt,
                    expected_encryption_mode=expected_mode,
                    now=admission_started_at,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            archive_path = await archive_source(request, "Backup import")
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

                def admitted_import() -> Any:
                    def consume_admission(archive_digest: str, observed_mode: str) -> None:
                        if observed_mode != expected_mode:
                            raise ValueError(
                                "Backup inspection receipt encryption mode does not match the import mode."
                            )
                        receipt_store.consume_digest(
                            receipt,
                            archive_digest,
                            expected_encryption_mode=expected_mode,
                            admission=admission,
                            now=admission_started_at,
                        )

                    return maintenance_service.import_bundle_from_file(
                        archive_path,
                        passphrase=passphrase,
                        expected_encrypted=expected_mode == "encrypted",
                        stop_services=stop_services,
                        restart_services=restart_services,
                        admission_callback=consume_admission,
                    )

                try:
                    result, maintenance = await run_retained_thread_worker(
                        admitted_import,
                    )
                except (ValueError, DockerRuntimeError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                record_config_change("backup.restore", "system backup import")
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
                        "final_running_containers": getattr(
                            maintenance,
                            "final_running_containers",
                            [],
                        ),
                        "runtime": await build_runtime_payload(runtime_service),
                    }
                )
            finally:
                archive_path.unlink(missing_ok=True)
                archive_path.parent.rmdir()
        finally:
            if admission is not None:
                receipt_store.release_admission(admission)

    @router.post("/api/admin/esxi-host-prep/upload")
    async def upload_esxi_host_prep_package(
        request: Request,
        filename: str = Query(..., min_length=1),
    ) -> JSONResponse:
        declared_bytes = limited_request_content_length(
            request,
            max_bytes=MAX_ESXI_HOST_PREP_UPLOAD_BYTES,
            body_description="ESXi host-prep upload",
        )
        service = get_esxi_host_prep_service()
        try:
            with service.reserve_stage_upload(declared_bytes) as reservation:
                upload_path = await stream_limited_request_body_to_file(
                    request,
                    max_bytes=reservation.max_bytes,
                    body_description="ESXi host-prep upload",
                    workspace_parent=service.staging_root,
                    workspace_prefix=reservation.workspace_prefix,
                )
                try:
                    if upload_path.stat().st_size == 0:
                        raise HTTPException(
                            status_code=400,
                            detail="ESXi host-prep upload request body was empty.",
                        )
                    content = await run_retained_thread_worker(upload_path.read_bytes)
                    upload_path.unlink()
                    upload_path.parent.rmdir()
                    package = await run_retained_thread_worker(
                        service.stage_reserved_package,
                        reservation,
                        filename,
                        content,
                    )
                    return JSONResponse(
                        {
                            "ok": True,
                            "package": package,
                            "packages": await asyncio.to_thread(service.list_staged_packages),
                        }
                    )
                finally:
                    upload_path.unlink(missing_ok=True)
                    try:
                        upload_path.parent.rmdir()
                    except FileNotFoundError:
                        pass
        except HostPrepStagingQuotaError as exc:
            raise HTTPException(status_code=507, detail=STAGING_QUOTA_ERROR) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
                        lambda system: credential_authorities_are_approved(
                            ssh_credential_authorities(
                                platform="esxi",
                                hosts=[payload.host],
                                port=payload.port,
                                username=payload.user,
                                strict_host_key_checking=payload.strict_host_key_checking,
                            ),
                            ssh_credential_authorities(
                                platform=system.truenas.platform,
                                hosts=[system.ssh.host],
                                port=system.ssh.port,
                                username=system.ssh.user,
                                strict_host_key_checking=system.ssh.strict_host_key_checking,
                            ),
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
                known_hosts_path=known_hosts_path_for_target(
                    settings,
                    system_id=payload.system_id,
                    target_host=payload.host,
                ),
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

        def matches_saved_api_credential_authority(system: Any) -> bool:
            if not same_credential_authority(
                api_credential_authority(
                    platform="quantastor",
                    host=payload.truenas_host,
                    username=payload.api_user,
                    verify_tls=payload.verify_ssl,
                    tls_ca_bundle_path=payload.tls_ca_bundle_path,
                    tls_server_name=payload.tls_server_name,
                ),
                api_credential_authority(
                    platform=system.truenas.platform,
                    host=system.truenas.host,
                    username=system.truenas.api_user,
                    verify_tls=system.truenas.verify_ssl,
                    tls_ca_bundle_path=system.truenas.tls_ca_bundle_path,
                    tls_server_name=system.truenas.tls_server_name,
                ),
            ):
                return False

            ssh_sink_hosts = quantastor_node_discovery_seed_hosts(payload)
            if (
                not payload.api_user
                or not payload.ssh_enabled
                or not payload.ssh_user
                or not (payload.ssh_key_path or payload.ssh_password)
                or not ssh_sink_hosts
            ):
                return True
            return system.ssh.enabled and credential_authorities_are_approved(
                ssh_credential_authorities(
                    platform="quantastor",
                    hosts=ssh_sink_hosts,
                    port=payload.ssh_port,
                    username=payload.ssh_user,
                    strict_host_key_checking=payload.ssh_strict_host_key_checking,
                ),
                ssh_credential_authorities(
                    platform=system.truenas.platform,
                    hosts=[
                        system.ssh.host,
                        *system.ssh.extra_hosts,
                        *(node.host for node in system.ssh.ha_nodes),
                    ],
                    port=system.ssh.port,
                    username=system.ssh.user,
                    strict_host_key_checking=system.ssh.strict_host_key_checking,
                ),
            )

        try:
            payload = payload.model_copy(
                update={
                    "api_password": resolve_saved_secondary_secret(
                        settings,
                        payload.system_id,
                        payload.api_password,
                        lambda system: system.truenas.api_password,
                        matches_saved_api_credential_authority,
                    ),
                    "ssh_password": resolve_saved_secondary_secret(
                        settings,
                        payload.system_id,
                        payload.ssh_password,
                        lambda system: system.ssh.password,
                        lambda system: system.ssh.enabled
                        and credential_authorities_are_approved(
                            ssh_credential_authorities(
                                platform="quantastor",
                                hosts=[
                                    payload.ssh_host,
                                    *(node.host for node in payload.ha_nodes),
                                ],
                                port=payload.ssh_port,
                                username=payload.ssh_user,
                                strict_host_key_checking=payload.ssh_strict_host_key_checking,
                            ),
                            ssh_credential_authorities(
                                platform=system.truenas.platform,
                                hosts=[
                                    system.ssh.host,
                                    *system.ssh.extra_hosts,
                                    *(node.host for node in system.ssh.ha_nodes),
                                ],
                                port=system.ssh.port,
                                username=system.ssh.user,
                                strict_host_key_checking=system.ssh.strict_host_key_checking,
                            ),
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
            known_hosts_path=known_hosts_path_for_target(
                settings,
                system_id=payload.system_id,
                target_host=payload.ssh_host,
            ),
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
                    "Saved. Restart the main UI to show the updated system."
                    if updated_existing
                    else "Saved. Restart the main UI to show the new system."
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
                    f"Demo builder system {saved_system.label} saved. Restart the main UI to show it."
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
        detail = f"{detail} Restart the main UI to remove it there too."
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
    async def purge_orphaned_history(payload: dict[str, Any]) -> JSONResponse:
        token = payload.get("preview_token")
        proof = purge_previews.pop(token, None) if isinstance(token, str) else None
        if payload.get("confirm_irreversible") is not True or proof is None or proof[0] < time.monotonic():
            raise HTTPException(status_code=409, detail="Preview orphaned history again and confirm irreversible deletion.")

        def purge_confirmed() -> tuple[dict[str, Any], list[str]]:
            # Keep config writers out until the history transaction has committed.
            with _CONFIG_WRITE_LOCK:
                settings = reload_app_settings()
                valid_ids = sorted(system.id for system in settings.systems)
                if valid_ids != proof[1]:
                    raise ValueError("Saved systems changed. Preview again before purging.")
                summary = get_history_store().purge_orphaned_history(valid_ids, expected_summaries=proof[2])
                return summary, valid_ids

        try:
            summary, valid_system_ids = await run_retained_thread_worker(purge_confirmed)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Orphaned history or saved systems changed. Preview again before purging.") from exc
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

        now = time.monotonic()
        for token, proof in list(purge_previews.items()):
            if proof[0] < now:
                del purge_previews[token]
        while len(purge_previews) >= 128:
            del purge_previews[next(iter(purge_previews))]
        token = secrets.token_urlsafe(32)
        purge_previews[token] = (now + 300, sorted(valid_system_ids), orphaned_systems)
        return JSONResponse(
            {
                "ok": True,
                "orphaned_systems": orphaned_systems,
                "valid_system_ids": valid_system_ids,
                "purge_preview_token": token,
            }
        )

    @router.get("/api/admin/history/systems")
    async def list_history_systems() -> JSONResponse:
        history_store = get_history_store()
        try:
            systems = await asyncio.to_thread(history_store.list_history_system_summaries)
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            logger.exception("Unable to inspect saved history")
            raise HTTPException(status_code=500, detail="Unable to inspect saved history; see admin logs.") from exc
        return JSONResponse({"ok": True, "systems": systems})

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
        bootstrap_service = ServiceAccountBootstrapService(
            settings.config_file,
            known_hosts_path=known_hosts_path_for_target(
                settings,
                system_id=payload.ssh_commands_source_system_id,
                target_host=payload.host,
            ),
        )
        try:
            if not payload.sudo_commands and payload.ssh_commands_source_system_id:
                # Re-validate so saved commands pass the same request-model sanitizer
                # (per-item length cap) as commands typed into the editor.
                payload = SystemSetupBootstrapRequest.model_validate(
                    {
                        **payload.model_dump(),
                        "sudo_commands": saved_sudo_commands_for_system(
                            settings,
                            payload.ssh_commands_source_system_id,
                        ),
                    }
                )
            result = await asyncio.to_thread(bootstrap_service.bootstrap_service_account, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @router.post("/api/admin/system-setup/sudoers-preview")
    async def preview_sudoers_file(payload: SystemSetupSudoPreviewRequest) -> JSONResponse:
        try:
            if not payload.sudo_commands and payload.ssh_commands_source_system_id:
                # Same re-validation as the bootstrap route: one sanitizer for both sources.
                payload = SystemSetupSudoPreviewRequest.model_validate(
                    {
                        **payload.model_dump(),
                        "sudo_commands": saved_sudo_commands_for_system(
                            reload_app_settings(),
                            payload.ssh_commands_source_system_id,
                        ),
                    }
                )
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
                    "Custom enclosure profile updated. Restart the main UI to use the updated profile."
                    if updated_existing
                    else "Custom enclosure profile saved. Restart the main UI to use the new profile."
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
                    f"Deleted custom profile {deleted_label}. Restart the main UI to remove it from the profile list too."
                ),
            }
        )

    # -- backup library (#398/#573): proxied to the backup scheduler sidecar -----------

    _BACKUP_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    _UNAVAILABLE_LIBRARY = {
        "available": False,
        "running": None,
        "classes": {
            "config": {"enabled": False, "last_run": None, "pending_changes": 0},
            "full": {"enabled": False, "last_run": None, "next_run_at": None},
        },
        "targets": [],
        "artifacts": [],
        "storage": {},
    }

    def backup_id_or_404(artifact_id: str) -> str:
        import re

        if not re.fullmatch(_BACKUP_ID_PATTERN, artifact_id or ""):
            raise HTTPException(status_code=404, detail="Backup not found.")
        return artifact_id

    async def scheduler_call(method: str, path: str, body: dict[str, Any] | None = None) -> JSONResponse:
        client = get_backup_scheduler_client()
        try:
            response = await asyncio.to_thread(client.request, method, path, body=body, actor="admin")
        except SchedulerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        payload = response.payload if isinstance(response.payload, dict) else {"detail": "Unexpected answer."}
        if response.status >= 400:
            raise HTTPException(status_code=response.status, detail=str(payload.get("detail") or "Backup request failed."))
        return JSONResponse(payload, status_code=response.status)

    @router.get("/api/admin/backups")
    async def list_backups() -> JSONResponse:
        client = get_backup_scheduler_client()
        try:
            response = await asyncio.to_thread(client.request, "GET", "/internal/backups")
        except SchedulerUnavailableError as exc:
            return JSONResponse({**_UNAVAILABLE_LIBRARY, "detail": str(exc)})
        if response.status >= 400 or not isinstance(response.payload, dict):
            return JSONResponse({**_UNAVAILABLE_LIBRARY, "detail": "The backup scheduler could not list backups."})
        return JSONResponse(response.payload)

    @router.post("/api/admin/backups/run")
    async def run_backup(payload: dict[str, Any]) -> JSONResponse:
        backup_class = payload.get("backup_class") if isinstance(payload, dict) else None
        if backup_class not in ("config", "full"):
            raise HTTPException(status_code=400, detail="backup_class must be config or full.")
        return await scheduler_call("POST", "/internal/backups/run", {"backup_class": backup_class})

    # Policy and target editor (#573). Secrets stay file-only: the view reports
    # present/missing per *_file setting and never returns the path itself.
    @router.get("/api/admin/backups/policy")
    async def get_backup_policy() -> JSONResponse:
        from history_service.backup_archive.editor import (
            PolicyEditError,
            load_editor_view,
        )

        settings = reload_app_settings()
        try:
            view = await asyncio.to_thread(load_editor_view, settings.config_file, dict(os.environ))
        except PolicyEditError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(view)

    @router.put("/api/admin/backups/policy")
    async def save_backup_policy(payload: dict[str, Any]) -> JSONResponse:
        from history_service.backup_archive.editor import (
            PolicyEditError,
            apply_editor_change,
        )

        settings = reload_app_settings()
        try:
            view = await asyncio.to_thread(
                apply_editor_change,
                settings.config_file,
                payload,
                dict(os.environ),
                write_lock=_CONFIG_WRITE_LOCK,
                record_change=record_config_change,
            )
        except PolicyEditError as exc:
            raise HTTPException(
                status_code=409 if exc.conflict else 400,
                detail=str(exc),
            ) from exc
        return JSONResponse({"ok": True, "restart_required": True, **view})

    @router.get("/api/admin/backups/lifecycle/plan")
    async def plan_backup_grooming() -> JSONResponse:
        return await scheduler_call("GET", "/internal/backups/lifecycle/plan")

    @router.post("/api/admin/backups/lifecycle/apply")
    async def apply_backup_grooming(payload: dict[str, Any]) -> JSONResponse:
        token = payload.get("plan_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token or len(token) > 128:
            raise HTTPException(status_code=400, detail="plan_token is required.")
        return await scheduler_call("POST", "/internal/backups/lifecycle/apply", {"plan_token": token})

    @router.post("/api/admin/backups/targets/{target_id}/test")
    async def test_backup_target(target_id: str) -> JSONResponse:
        return await scheduler_call("POST", f"/internal/backups/targets/{backup_id_or_404(target_id)}/test")

    @router.get("/api/admin/backups/{artifact_id}")
    async def get_backup(artifact_id: str) -> JSONResponse:
        return await scheduler_call("GET", f"/internal/backups/{backup_id_or_404(artifact_id)}")

    @router.post("/api/admin/backups/{artifact_id}/verify")
    async def verify_backup(artifact_id: str) -> JSONResponse:
        return await scheduler_call("POST", f"/internal/backups/{backup_id_or_404(artifact_id)}/verify")

    @router.post("/api/admin/backups/{artifact_id}/preserve")
    async def preserve_backup(artifact_id: str, payload: dict[str, Any]) -> JSONResponse:
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            raise HTTPException(status_code=400, detail="reason is required.")
        return await scheduler_call(
            "POST", f"/internal/backups/{backup_id_or_404(artifact_id)}/preserve", {"reason": reason}
        )

    @router.delete("/api/admin/backups/{artifact_id}/preserve")
    async def unpreserve_backup(artifact_id: str) -> JSONResponse:
        return await scheduler_call("DELETE", f"/internal/backups/{backup_id_or_404(artifact_id)}/preserve")

    @router.get("/api/admin/backups/{artifact_id}/download")
    async def download_backup(artifact_id: str) -> Response:
        client = get_backup_scheduler_client()
        try:
            response, chunks = await asyncio.to_thread(client.stream, backup_id_or_404(artifact_id))
        except SchedulerUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if chunks is None:
            detail = response.payload.get("detail") if isinstance(response.payload, dict) else None
            raise HTTPException(status_code=response.status, detail=str(detail or "Backup could not be read."))
        headers = {"Content-Disposition": f'attachment; filename="{response.payload["filename"]}"'}
        if response.payload.get("length"):
            headers["Content-Length"] = str(response.payload["length"])
        if response.payload.get("sha256"):
            headers["X-Backup-Sha256"] = str(response.payload["sha256"])
        return StreamingResponse(
            iterate_in_threadpool(chunks), media_type="application/octet-stream", headers=headers
        )

    def catalog_archive_source(artifact_id: str) -> Any:
        async def source(_request: Request, body_description: str) -> Path:
            client = get_backup_scheduler_client()
            workspace = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-admin-catalog-"))
            archive_path = workspace / "bundle.archive"

            def fetch() -> Any:
                descriptor = os.open(
                    archive_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    result = client.download_to(artifact_id, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                return result

            try:
                result = await asyncio.to_thread(fetch)
            except SchedulerUnavailableError as exc:
                archive_path.unlink(missing_ok=True)
                workspace.rmdir()
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except BaseException:
                archive_path.unlink(missing_ok=True)
                workspace.rmdir()
                raise
            if result.status != 200:
                archive_path.unlink(missing_ok=True)
                workspace.rmdir()
                detail = result.payload.get("detail") if isinstance(result.payload, dict) else None
                raise HTTPException(
                    status_code=result.status if result.status in (404, 409, 502, 503) else 502,
                    detail=str(detail or f"{body_description} could not read the backup."),
                )
            return archive_path

        return source

    @router.post("/api/admin/backups/{artifact_id}/restore/inspect")
    @observe_backup_route("inspect")
    async def inspect_catalog_backup(artifact_id: str, request: Request) -> JSONResponse:
        return await inspect_archive(request, catalog_archive_source(backup_id_or_404(artifact_id)))

    @router.post("/api/admin/backups/{artifact_id}/restore/import")
    @observe_backup_route("import")
    async def import_catalog_backup(
        artifact_id: str,
        request: Request,
        stop_services: bool = Query(default=True),
        restart_services: bool = Query(default=True),
    ) -> JSONResponse:
        return await import_archive(
            request,
            catalog_archive_source(backup_id_or_404(artifact_id)),
            stop_services,
            restart_services,
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
