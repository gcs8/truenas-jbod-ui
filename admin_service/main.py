from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin_service.config import AdminSettings, get_admin_settings
from admin_service.services.account_bootstrap import ServiceAccountBootstrapService
from admin_service.services.maintenance import AdminMaintenanceService
from admin_service.services.runtime_control import DockerRuntimeError, DockerRuntimeService
from admin_service.services.tls_trust import TLSTrustStoreService
from app import __version__
from app.config import (
    Settings,
    TrueNASConfig,
    get_settings,
)
from app.models.domain import (
    DebugBundleExportRequest,
    DemoSystemRequest,
    EnclosureProfileRequest,
    HistoryAdoptRequest,
    QuantastorNodeDiscoveryRequest,
    SSHKeyGenerateRequest,
    SystemBackupExportRequest,
    SystemSetupBootstrapRequest,
    SystemSetupSudoPreviewRequest,
    SystemSetupRequest,
    TLSCertificateImportRequest,
    TLSCertificateInspectRequest,
    TLSRemoteCertificateTrustRequest,
)
from app.services.profile_builder import ProfileBuilderService, collect_profile_references
from app.services.demo_system_factory import DemoSystemFactory
from app.services.profile_registry import ProfileRegistry
from app.services.inventory_registry import InventoryRegistry
from app.services.quantastor_api import QuantastorRESTClient
from app.services.ssh_key_manager import SSHKeyManager
from app.services.storage_view_templates import list_storage_view_templates
from app.services.storage_views import resolve_system_storage_views
from app.services.system_setup import SystemSetupService, default_ssh_commands_for_platform
from app.services.parsers import normalize_text
from history_service.config import get_history_settings
from history_service.store import HistoryStore
from history_service.system_backup import (
    SystemBackupService,
    default_backup_included_paths,
    default_debug_included_paths,
    describe_bundle_groups,
)


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)

SERVICE_STARTED_AT = datetime.now(timezone.utc)


def reload_app_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@lru_cache
def get_history_store() -> HistoryStore:
    history_settings = get_history_settings()
    return HistoryStore(history_settings.sqlite_path)


def decode_optional_secret_header(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("Backup passphrase header was not valid base64.") from exc
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Backup passphrase header was not valid UTF-8.") from exc


@lru_cache
def get_backup_service() -> SystemBackupService:
    history_settings = get_history_settings()
    return SystemBackupService(history_settings, get_history_store())


@lru_cache
def get_runtime_service() -> DockerRuntimeService:
    return DockerRuntimeService(get_admin_settings())


@lru_cache
def get_maintenance_service() -> AdminMaintenanceService:
    admin_settings = get_admin_settings()
    return AdminMaintenanceService(
        get_backup_service(),
        get_runtime_service(),
        clean_backup_targets=admin_settings.clean_backup_targets,
    )


def _format_count(value: int, singular: str, plural: str | None = None) -> str:
    label = singular if value == 1 else (plural or f"{singular}s")
    return f"{value} {label}"


def format_history_cleanup_summary(summary: dict[str, Any]) -> str:
    tracked_slots = int(summary.get("tracked_slots", 0) or 0)
    event_count = int(summary.get("event_count", 0) or 0)
    metric_sample_count = int(summary.get("metric_sample_count", 0) or 0)
    return ", ".join(
        (
            _format_count(tracked_slots, "tracked slot"),
            _format_count(event_count, "event"),
            _format_count(metric_sample_count, "metric sample"),
        )
    )


def format_history_system_summary(summary: dict[str, Any]) -> str:
    total_rows = int(summary.get("total_rows", 0) or 0)
    return (
        f"{summary.get('system_label') or summary.get('system_id')} "
        f"({_format_count(total_rows, 'saved history row')}; {format_history_cleanup_summary(summary)})"
    )


def create_app() -> FastAPI:
    admin_settings = get_admin_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        shutdown_task: asyncio.Task[None] | None = None
        if admin_settings.auto_stop_seconds > 0:
            shutdown_task = asyncio.create_task(_shutdown_after_ttl(admin_settings.auto_stop_seconds))
        try:
            yield
        finally:
            if shutdown_task is not None and not shutdown_task.done():
                shutdown_task.cancel()
                try:
                    await shutdown_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title=admin_settings.app_name,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
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

    @app.get("/api/admin/state")
    async def get_admin_state(request: Request) -> JSONResponse:
        return JSONResponse(await build_admin_state_payload(request))

    @app.post("/api/admin/runtime/containers/{container_key}/stop")
    async def stop_container(container_key: str) -> JSONResponse:
        if container_key == "admin":
            raise HTTPException(status_code=400, detail="The admin sidecar cannot stop itself from the UI.")
        runtime_service = get_runtime_service()
        try:
            await asyncio.to_thread(runtime_service.stop_container, container_key)
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "runtime": runtime_service.status_payload()})

    @app.post("/api/admin/runtime/containers/{container_key}/start")
    async def start_container(container_key: str) -> JSONResponse:
        if container_key == "admin":
            raise HTTPException(status_code=400, detail="The admin sidecar is already running.")
        runtime_service = get_runtime_service()
        try:
            await asyncio.to_thread(runtime_service.start_container, container_key)
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "runtime": runtime_service.status_payload()})

    @app.post("/api/admin/runtime/containers/{container_key}/restart")
    async def restart_container(container_key: str) -> JSONResponse:
        if container_key == "admin":
            raise HTTPException(status_code=400, detail="The admin sidecar cannot restart itself from the UI.")
        runtime_service = get_runtime_service()
        try:
            await asyncio.to_thread(runtime_service.restart_container, container_key)
        except DockerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "runtime": runtime_service.status_payload()})

    @app.post("/api/admin/backup/export")
    async def export_backup(
        payload: SystemBackupExportRequest,
        stop_services: bool = Query(default=False),
        restart_services: bool = Query(default=True),
    ) -> Response:
        maintenance_service = get_maintenance_service()
        try:
            artifact, maintenance = await asyncio.to_thread(
                maintenance_service.export_bundle,
                payload,
                stop_services=stop_services,
                restart_services=restart_services,
            )
        except (ValueError, DockerRuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return Response(
            content=artifact.content,
            media_type=artifact.media_type or "application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "X-Backup-Encrypted": "true" if payload.encrypt else "false",
                "X-Backup-Packaging": str(artifact.manifest.get("packaging") or payload.packaging),
                "X-Backup-Schema-Version": str(artifact.manifest.get("schema_version") or 1),
                "X-Admin-Stopped-Containers": ",".join(maintenance.stopped_containers),
                "X-Admin-Restarted-Containers": ",".join(maintenance.restarted_containers),
            },
        )

    @app.post("/api/admin/debug/export")
    async def export_debug_bundle(
        payload: DebugBundleExportRequest,
        stop_services: bool = Query(default=True),
        restart_services: bool = Query(default=True),
    ) -> Response:
        maintenance_service = get_maintenance_service()
        try:
            artifact, maintenance = await asyncio.to_thread(
                maintenance_service.export_debug_bundle,
                payload,
                stop_services=stop_services,
                restart_services=restart_services,
            )
        except (ValueError, DockerRuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return Response(
            content=artifact.content,
            media_type=artifact.media_type or "application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "X-Debug-Encrypted": "true" if payload.encrypt else "false",
                "X-Debug-Packaging": str(artifact.manifest.get("packaging") or payload.packaging),
                "X-Debug-Schema-Version": str(artifact.manifest.get("schema_version") or 1),
                "X-Debug-Scrubbed": "true" if (payload.scrub_secrets or payload.scrub_disk_identifiers) else "false",
                "X-Debug-Scrub-Secrets": "true" if payload.scrub_secrets else "false",
                "X-Debug-Scrub-Disk-Identifiers": "true" if payload.scrub_disk_identifiers else "false",
                "X-Admin-Stopped-Containers": ",".join(maintenance.stopped_containers),
                "X-Admin-Restarted-Containers": ",".join(maintenance.restarted_containers),
            },
        )

    @app.post("/api/admin/backup/import")
    async def import_backup(
        request: Request,
        stop_services: bool = Query(default=True),
        restart_services: bool = Query(default=True),
    ) -> JSONResponse:
        content = await request.body()
        if not content:
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
                maintenance_service.import_bundle,
                content,
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
        if restart_services:
            await asyncio.to_thread(runtime_service.clear_restart_required, impacted)
        else:
            await asyncio.to_thread(runtime_service.mark_restart_required, impacted)
        return JSONResponse(
            {
                **result,
                "systems": serialize_systems(settings),
                "default_system_id": settings.default_system_id,
                "stopped_containers": maintenance.stopped_containers,
                "restarted_containers": maintenance.restarted_containers,
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.get("/api/admin/ssh-keys")
    async def list_ssh_keys() -> JSONResponse:
        settings = reload_app_settings()
        key_manager = SSHKeyManager(settings.config_file)
        keys = await asyncio.to_thread(key_manager.list_keys)
        return JSONResponse({"ok": True, "keys": keys})

    @app.post("/api/admin/ssh-keys/generate")
    async def generate_ssh_key(payload: SSHKeyGenerateRequest) -> JSONResponse:
        settings = reload_app_settings()
        key_manager = SSHKeyManager(settings.config_file)
        try:
            generated_key = await asyncio.to_thread(key_manager.generate_keypair, payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        keys = await asyncio.to_thread(key_manager.list_keys)
        return JSONResponse({"ok": True, "key": generated_key, "keys": keys})

    @app.post("/api/admin/tls/inspect")
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

    @app.post("/api/admin/tls/import")
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

    @app.post("/api/admin/tls/trust-remote")
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

    @app.post("/api/admin/system-setup/quantastor-nodes")
    async def discover_quantastor_nodes(payload: QuantastorNodeDiscoveryRequest) -> JSONResponse:
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
        return JSONResponse({"ok": True, "nodes": nodes})

    @app.post("/api/admin/system-setup")
    async def create_system(payload: SystemSetupRequest) -> JSONResponse:
        settings = reload_app_settings()
        setup_service = SystemSetupService(settings.config_file)
        try:
            saved_system, updated_existing = await asyncio.to_thread(setup_service.save_system, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        return JSONResponse(
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
                "restart_required": ["ui"],
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.post("/api/admin/system-setup/demo")
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
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        return JSONResponse(
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
                "restart_required": ["ui"],
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.delete("/api/admin/system-setup/{system_id}")
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
            except Exception as exc:  # noqa: BLE001 - config delete already succeeded, so surface purge failure as warning payload.
                logger.exception("History purge failed after deleting saved system %s", system_id)
                history_purge = {
                    "requested": True,
                    "ok": False,
                    "summary": None,
                    "detail": f"Saved history purge failed: {exc}",
                }

        refreshed_settings = reload_app_settings()
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        detail = f"Removed {deleted_label}."
        if purge_history:
            detail = f"{detail} {history_purge['detail']}"
        detail = f"{detail} Restart the Read UI container to drop the deleted system from the live runtime."
        return JSONResponse(
            {
                "ok": True,
                "system_id": system_id,
                "deleted_label": deleted_label,
                "systems": serialize_systems(refreshed_settings),
                "default_system_id": next_default_id,
                "detail": detail,
                "history_purge": history_purge,
                "restart_required": ["ui"],
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.post("/api/admin/history/purge-orphaned")
    async def purge_orphaned_history() -> JSONResponse:
        settings = reload_app_settings()
        valid_system_ids = [system.id for system in settings.systems]
        history_store = get_history_store()
        try:
            summary = await asyncio.to_thread(history_store.purge_orphaned_history, valid_system_ids)
        except Exception as exc:  # noqa: BLE001 - surface maintenance failures directly in admin.
            raise HTTPException(status_code=500, detail=f"Unable to purge orphaned history: {exc}") from exc

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

    @app.get("/api/admin/history/orphaned")
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
            raise HTTPException(status_code=500, detail=f"Unable to inspect orphaned history: {exc}") from exc

        return JSONResponse(
            {
                "ok": True,
                "orphaned_systems": orphaned_systems,
                "valid_system_ids": valid_system_ids,
            }
        )

    @app.post("/api/admin/history/adopt-removed-system")
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
            raise HTTPException(status_code=500, detail=f"Unable to inspect orphaned history: {exc}") from exc

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
            raise HTTPException(status_code=500, detail=f"Unable to adopt removed system history: {exc}") from exc

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

    @app.post("/api/admin/system-setup/bootstrap")
    async def bootstrap_service_account(payload: SystemSetupBootstrapRequest) -> JSONResponse:
        settings = reload_app_settings()
        bootstrap_service = ServiceAccountBootstrapService(settings.config_file)
        try:
            result = await asyncio.to_thread(bootstrap_service.bootstrap_service_account, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/admin/system-setup/sudoers-preview")
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

    @app.get("/api/admin/storage-views/candidates")
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

    @app.get("/api/admin/storage-views/live-enclosures")
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

    @app.post("/api/admin/profiles")
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
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        serialized_profiles = serialize_profiles(refreshed_settings)
        serialized_profile = next(
            (profile for profile in serialized_profiles if profile["id"] == saved_profile.id),
            None,
        )
        return JSONResponse(
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
                "restart_required": ["ui"],
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.delete("/api/admin/profiles/{profile_id}")
    async def delete_profile(profile_id: str) -> JSONResponse:
        settings = reload_app_settings()
        profile_service = ProfileBuilderService(settings.config_file, settings.paths.profile_file)
        try:
            deleted_label = await asyncio.to_thread(profile_service.delete_profile, profile_id, settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed_settings = reload_app_settings()
        runtime_service = get_runtime_service()
        await asyncio.to_thread(runtime_service.mark_restart_required, ("ui",))
        return JSONResponse(
            {
                "ok": True,
                "profile_id": profile_id,
                "deleted_label": deleted_label,
                "profiles": serialize_profiles(refreshed_settings),
                "detail": (
                    f"Deleted custom profile {deleted_label}. Restart the Read UI container when you are ready to drop it from the runtime profile list too."
                ),
                "restart_required": ["ui"],
                "runtime": runtime_service.status_payload(),
            }
        )

    @app.get("/healthz")
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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled admin service error")
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    return app


async def build_admin_state_payload(request: Request) -> dict[str, Any]:
    settings = reload_app_settings()
    runtime_service = get_runtime_service()
    runtime_payload = await asyncio.to_thread(runtime_service.status_payload)
    key_manager = SSHKeyManager(settings.config_file)
    ssh_keys = await asyncio.to_thread(key_manager.list_keys)
    admin_settings = get_admin_settings()
    history_settings = get_history_settings()
    expires_at = compute_expires_at(admin_settings)
    return {
        "ok": True,
        "app_version": __version__,
        "admin": {
            "title": admin_settings.app_name,
            "started_at": SERVICE_STARTED_AT.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "auto_stop_seconds": admin_settings.auto_stop_seconds,
            "public_origin": resolve_public_origin(admin_settings, request),
        },
        "systems": serialize_systems(settings),
        "default_system_id": settings.default_system_id,
        "profiles": serialize_profiles(settings),
        "storage_view_templates": serialize_storage_view_templates(),
        "setup_platform_defaults": serialize_platform_defaults(),
        "ssh_keys": ssh_keys,
        "runtime": runtime_payload,
        "backup_defaults": {
            "packaging": "tar.zst",
            "stop_services": False,
            "restart_services": True,
            "import_stop_services": True,
            "import_restart_services": True,
            "included_paths": default_backup_included_paths(),
            "debug_packaging": "tar.zst",
            "debug_stop_services": True,
            "debug_restart_services": True,
            "debug_included_paths": default_debug_included_paths(),
            "debug_scrub_secrets": True,
            "debug_scrub_disk_identifiers": True,
            "debug_scrub_sensitive": True,
            "path_groups": describe_bundle_groups(settings, history_settings),
            "clean_backup_targets": list(admin_settings.clean_backup_targets),
        },
        "paths": {
            "config_file": settings.config_file,
            "profile_file": settings.paths.profile_file,
            "mapping_file": settings.paths.mapping_file,
            "slot_detail_cache_file": settings.paths.slot_detail_cache_file,
            "history_db": history_settings.sqlite_path,
            "tls_dir": str(Path(settings.config_file).parent / "tls"),
        },
    }


def serialize_systems(settings: Settings) -> list[dict[str, Any]]:
    profile_registry = ProfileRegistry(settings)
    return [
        {
            "id": system.id,
            "label": system.label,
            "platform": system.truenas.platform,
            "default_profile_id": system.default_profile_id,
            "is_default": system.id == settings.default_system_id,
            "truenas_host": system.truenas.host,
            "api_key": system.truenas.api_key,
            "api_user": system.truenas.api_user,
            "api_password": system.truenas.api_password,
            "verify_ssl": bool(system.truenas.verify_ssl),
            "tls_ca_bundle_path": system.truenas.tls_ca_bundle_path,
            "tls_server_name": system.truenas.tls_server_name,
            "enclosure_filter": system.truenas.enclosure_filter,
            "timeout_seconds": system.truenas.timeout_seconds,
            "ssh_enabled": bool(system.ssh.enabled),
            "ssh_host": system.ssh.host,
            "ssh_extra_hosts": list(system.ssh.extra_hosts),
            "ha_enabled": bool(
                system.ssh.ha_enabled
                or (
                    system.truenas.platform == "quantastor"
                    and (system.ssh.ha_nodes or system.ssh.extra_hosts)
                )
            ),
            "ha_nodes": serialize_system_ha_nodes(system),
            "ssh_port": system.ssh.port,
            "ssh_user": system.ssh.user,
            "ssh_key_path": system.ssh.key_path,
            "ssh_password": system.ssh.password,
            "ssh_sudo_password": system.ssh.sudo_password,
            "ssh_known_hosts_path": system.ssh.known_hosts_path,
            "ssh_strict_host_key_checking": bool(system.ssh.strict_host_key_checking),
            "ssh_timeout_seconds": system.ssh.timeout_seconds,
            "ssh_commands": list(system.ssh.commands),
            "storage_views": serialize_storage_views(system, profile_registry),
        }
        for system in settings.systems
    ]


def serialize_system_ha_nodes(system: Any) -> list[dict[str, Any]]:
    explicit_nodes = list(getattr(system.ssh, "ha_nodes", []) or [])
    if explicit_nodes:
        return [
            {
                "system_id": node.system_id,
                "label": node.label,
                "host": node.host,
            }
            for node in explicit_nodes[:3]
            if node.system_id or node.label or node.host
        ]

    if getattr(system.truenas, "platform", None) != "quantastor":
        return []

    legacy_hosts = [
        normalize_text(system.ssh.host),
        *[
            normalize_text(value)
            for value in (system.ssh.extra_hosts or [])
        ],
    ]
    nodes: list[dict[str, Any]] = []
    for index, host in enumerate(host for host in legacy_hosts if host):
        nodes.append(
            {
                "system_id": None,
                "label": f"Configured Node {index + 1}",
                "host": host,
            }
        )
    return nodes[:3]


def serialize_storage_views(system: Any, profile_registry: ProfileRegistry) -> list[dict[str, Any]]:
    stored_views = resolve_system_storage_views(system, profile_registry)
    return [
        {
            "id": storage_view.id,
            "label": storage_view.label,
            "kind": storage_view.kind,
            "template_id": storage_view.template_id,
            "profile_id": storage_view.profile_id,
            "enabled": bool(storage_view.enabled),
            "order": storage_view.order,
            "render": storage_view.render.model_dump(mode="json"),
            "binding": storage_view.binding.model_dump(mode="json", exclude_none=True),
            "layout_overrides": (
                storage_view.layout_overrides.model_dump(mode="json")
                if storage_view.layout_overrides is not None
                else None
            ),
        }
        for storage_view in sorted(
            stored_views,
            key=lambda item: (item.order, item.label.lower(), item.id),
        )
    ]


def serialize_storage_view_templates() -> list[dict[str, Any]]:
    return [
        template.model_dump(mode="json")
        for template in list_storage_view_templates()
    ]


def serialize_profiles(settings: Settings) -> list[dict[str, Any]]:
    registry = ProfileRegistry(settings)
    custom_profile_ids = {profile.id for profile in settings.profiles}
    reference_map = collect_profile_references(settings)
    profiles = []
    for profile in registry.list_profiles():
        slot_count = sum(
            1
            for row in profile.slot_layout
            for slot in row
            if isinstance(slot, int)
        )
        references = reference_map.get(profile.id, {})
        is_custom = profile.id in custom_profile_ids
        profiles.append(
            {
                **profile.model_dump(mode="json"),
                "slot_count": slot_count,
                "is_custom": is_custom,
                "source": "custom" if is_custom else "built-in",
                "reference_count": int(references.get("count", 0) or 0),
            }
        )
    return profiles


def serialize_quantastor_nodes(raw_data: Any) -> list[dict[str, Any]]:
    if raw_data is None:
        return []

    hardware_system_ids = {
        system_id
        for system_id in (
            normalize_text(str(item.get("storageSystemId")) if item.get("storageSystemId") is not None else None)
            for item in [*(getattr(raw_data, "hw_disks", []) or []), *(getattr(raw_data, "hw_enclosures", []) or [])]
        )
        if system_id
    }
    nodes: list[dict[str, Any]] = []
    for system_row in getattr(raw_data, "systems", []) or []:
        system_id = normalize_text(str(system_row.get("id")) if system_row.get("id") is not None else None)
        if not system_id:
            continue
        if hardware_system_ids and system_id not in hardware_system_ids:
            continue
        nodes.append(
            {
                "system_id": system_id,
                "label": (
                    normalize_text(
                        str(system_row.get("name") or system_row.get("hostname") or system_row.get("description") or system_id)
                    )
                    or system_id
                ),
                "host": normalize_text(system_row.get("hostname") or system_row.get("ipAddress")),
                "cluster_id": normalize_text(
                    str(system_row.get("storageSystemClusterId"))
                    if system_row.get("storageSystemClusterId") is not None
                    else None
                ),
                "is_master": bool(system_row.get("isMaster")),
            }
        )
    return nodes


def serialize_platform_defaults() -> dict[str, dict[str, object]]:
    return {
        platform: {
            "ssh_commands": default_ssh_commands_for_platform(platform),
        }
        for platform in ("core", "scale", "linux", "quantastor")
    }


def serialize_live_enclosures(service: Any, enclosures: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for enclosure in enclosures:
        resolved_profile = service.profile_registry.resolve_for_enclosure(
            service.system,
            enclosure,
            fallback_label=enclosure.label,
            fallback_rows=enclosure.rows if enclosure.rows else service.settings.layout.rows,
            fallback_columns=enclosure.columns if enclosure.columns else service.settings.layout.columns,
            fallback_slot_count=enclosure.slot_count if enclosure.slot_count else service.settings.layout.slot_count,
            fallback_slot_layout=enclosure.slot_layout,
        )
        serialized.append(
            {
                "id": enclosure.id,
                "label": enclosure.label,
                "name": enclosure.name,
                "slot_count": enclosure.slot_count,
                "profile_id": resolved_profile.id if resolved_profile else enclosure.profile_id,
                "profile_label": resolved_profile.label if resolved_profile else None,
            }
        )
    return serialized


def compute_expires_at(settings: AdminSettings) -> datetime | None:
    if settings.auto_stop_seconds <= 0:
        return None
    return SERVICE_STARTED_AT + timedelta(seconds=settings.auto_stop_seconds)


def resolve_public_origin(settings: AdminSettings, request: Request) -> str:
    if settings.public_origin:
        return settings.public_origin.rstrip("/")
    return str(request.base_url).rstrip("/")


async def _shutdown_after_ttl(auto_stop_seconds: int) -> None:
    await asyncio.sleep(auto_stop_seconds)
    os.kill(os.getpid(), signal.SIGTERM)


app = create_app()
