from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app import __version__
from app.logging_config import configure_logging
from app.models.domain import (
    InventorySnapshot,
    LedAction,
    LedRequest,
    MappingBundle,
    MappingRequest,
    SnapshotExportRequest,
    SmartBatchRequest,
    SmartBatchResponse,
    SmartSummaryView,
    StorageViewRuntimePayload,
)
from app.perf import add_perf_metadata, install_perf_timing_middleware, perf_stage
from app.services.history_backend import HistoryBackendClient
from app.services.inventory_registry import InventoryRegistry
from app.services.snapshot_export import SnapshotExportService, SnapshotExportTooLargeError
from app.services.truenas_ws import TrueNASAPIError

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

logger = logging.getLogger(__name__)


@lru_cache
def get_inventory_registry() -> InventoryRegistry:
    settings = get_settings()
    configure_logging(settings)
    return InventoryRegistry(settings)


@lru_cache
def get_history_backend() -> HistoryBackendClient:
    settings = get_settings()
    configure_logging(settings)
    return HistoryBackendClient(settings.history)


@lru_cache
def get_snapshot_export_service() -> SnapshotExportService:
    settings = get_settings()
    configure_logging(settings)
    return SnapshotExportService(settings, get_history_backend(), templates)


def create_app() -> FastAPI:
    startup_settings = get_settings()
    configure_logging(startup_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        warm_task: asyncio.Task[None] | None = None
        if startup_settings.app.startup_warm_cache_enabled:
            registry = get_inventory_registry()
            warm_task = asyncio.create_task(
                registry.prewarm_all(warm_smart=startup_settings.app.startup_warm_smart_enabled)
            )
        try:
            yield
        finally:
            if warm_task is not None and not warm_task.done():
                warm_task.cancel()
                try:
                    await warm_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="TrueNAS JBOD Enclosure UI",
        version=__version__,
        docs_url="/docs" if startup_settings.app.debug else None,
        redoc_url="/redoc" if startup_settings.app.debug else None,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    install_perf_timing_middleware(app, startup_settings)

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> HTMLResponse:
        current_settings = get_settings()
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        admin_launch_url = await asyncio.to_thread(resolve_admin_launch_url, request, current_settings)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, enclosure_id=enclosure_id)
        snapshot = await service.get_snapshot(
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=True,
        )
        storage_view_runtime = await service.get_storage_view_runtime(
            selected_enclosure_id=enclosure_id,
            snapshot=snapshot,
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            build_index_context(
                request=request,
                snapshot=snapshot,
                storage_view_runtime=storage_view_runtime,
                settings=current_settings,
                history_configured=bool(current_settings.history.service_url),
                admin_launch_url=admin_launch_url,
            ),
        )

    @app.get("/api/inventory", response_model=InventorySnapshot)
    async def get_inventory(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> InventorySnapshot:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            force_refresh=force,
        )
        return await service.get_snapshot(
            force_refresh=force,
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=not force,
        )

    @app.get("/api/storage-views", response_model=StorageViewRuntimePayload)
    async def get_storage_views(
        force: bool = False,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> StorageViewRuntimePayload:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            force_refresh=force,
        )
        return await service.get_storage_view_runtime(
            force_refresh=force,
            selected_enclosure_id=enclosure_id,
        )

    @app.post("/api/slots/{slot}/led")
    async def set_slot_led(
        slot: int,
        payload: LedRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(get_settings(), slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        try:
            await service.set_slot_led(
                slot,
                payload.action,
                selected_enclosure_id=enclosure_id,
                invalidate_snapshot=False,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        service.invalidate_snapshot_cache(reason="route.set_slot_led", invalidate_source_bundle=True)
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": True, "snapshot": snapshot.model_dump(mode="json")})

    @app.post("/api/slots/{slot}/mapping")
    async def save_mapping(
        slot: int,
        payload: MappingRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(get_settings(), slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        mapping_payload = {
            "serial": payload.serial,
            "device_name": payload.device_name,
            "gptid": payload.gptid,
            "notes": payload.notes,
        }
        mapping = await service.save_mapping(
            slot,
            mapping_payload,
            selected_enclosure_id=enclosure_id,
            invalidate_snapshot=False,
        )

        led_warning = None
        if payload.clear_identify_after_save:
            try:
                await service.set_slot_led(
                    slot,
                    LedAction.clear,
                    selected_enclosure_id=enclosure_id,
                    invalidate_snapshot=False,
                )
            except Exception as exc:  # noqa: BLE001 - surface as non-fatal warning.
                led_warning = str(exc)

        service.invalidate_snapshot_cache(
            reason="route.save_mapping",
            invalidate_source_bundle=payload.clear_identify_after_save,
        )
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                "mapping": mapping.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "warning": led_warning,
            }
        )

    @app.delete("/api/slots/{slot}/mapping")
    async def clear_mapping(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(get_settings(), slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        cleared = await service.clear_mapping(slot, selected_enclosure_id=enclosure_id, invalidate_snapshot=False)
        if cleared:
            service.invalidate_snapshot_cache(reason="route.clear_mapping")
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return JSONResponse({"ok": cleared, "snapshot": snapshot.model_dump(mode="json")})

    @app.get("/api/mappings/export", response_model=MappingBundle)
    async def export_mappings(
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> MappingBundle:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        return await service.export_mapping_bundle(selected_enclosure_id=enclosure_id)

    @app.post("/api/mappings/import")
    async def import_mappings(
        payload: MappingBundle,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        imported = await service.import_mapping_bundle(
            payload,
            selected_enclosure_id=enclosure_id,
            invalidate_snapshot=False,
        )
        service.invalidate_snapshot_cache(reason="route.import_mappings")
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        return JSONResponse(
            {
                "ok": True,
                "imported": imported,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )

    @app.get("/api/slots/{slot}/smart", response_model=SmartSummaryView)
    async def get_slot_smart_summary(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartSummaryView:
        ensure_slot_bounds(get_settings(), slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, slot=slot, enclosure_id=enclosure_id)
        try:
            return await service.get_slot_smart_summary(
                slot,
                selected_enclosure_id=enclosure_id,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/storage-views/{view_id}/slots/{slot_index}/smart", response_model=SmartSummaryView)
    async def get_storage_view_slot_smart_summary(
        view_id: str,
        slot_index: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartSummaryView:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot=slot_index,
            enclosure_id=enclosure_id,
        )
        try:
            return await service.get_storage_view_slot_smart_summary(
                view_id,
                slot_index,
                selected_enclosure_id=enclosure_id,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/storage-views/{view_id}/slots/{slot_index}/history")
    async def get_storage_view_slot_history(
        view_id: str,
        slot_index: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        history_slot, history_enclosure_id = await service.resolve_storage_view_slot_history_target(
            view_id,
            slot_index,
            selected_enclosure_id=enclosure_id,
        )
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            storage_view_id=view_id,
            slot=slot_index,
            history_slot=history_slot,
            enclosure_id=history_enclosure_id,
        )
        history_backend = get_history_backend()
        payload = await history_backend.get_slot_history(
            history_slot,
            service.system.id,
            history_enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse(payload)

    @app.post("/api/slots/smart-batch", response_model=SmartBatchResponse)
    async def get_slot_smart_summaries(
        payload: SmartBatchRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        fresh: bool = False,
    ) -> SmartBatchResponse:
        for slot in payload.slots:
            ensure_slot_bounds(get_settings(), slot)
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(
            system_id=service.system.id,
            platform=service.system.truenas.platform,
            enclosure_id=enclosure_id,
            slot_count=len(payload.slots),
            smart_batch_max_concurrency=payload.max_concurrency,
        )
        try:
            summaries = await service.get_slot_smart_summaries(
                payload.slots,
                selected_enclosure_id=enclosure_id,
                max_concurrency=payload.max_concurrency,
                allow_stale_cache=not fresh,
            )
        except TrueNASAPIError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SmartBatchResponse(summaries=summaries)

    @app.get("/api/history/status")
    async def get_history_status() -> JSONResponse:
        history_backend = get_history_backend()
        return JSONResponse(await history_backend.get_status())

    @app.get("/api/slots/{slot}/history")
    async def get_slot_history(
        slot: int,
        system_id: str | None = None,
        enclosure_id: str | None = None,
        window_hours: int | None = None,
    ) -> JSONResponse:
        ensure_slot_bounds(get_settings(), slot)
        history_backend = get_history_backend()
        payload = await history_backend.get_slot_history(
            slot,
            system_id,
            enclosure_id,
            window_hours=window_hours,
        )
        return JSONResponse(payload)

    @app.post("/api/export/enclosure-snapshot")
    async def export_enclosure_snapshot(
        request: Request,
        payload: SnapshotExportRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> Response:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, enclosure_id=enclosure_id)
        with perf_stage("route.export_snapshot.load_snapshot"):
            snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        with perf_stage("route.export_snapshot.load_smart_summaries", slot_count=len(snapshot.slots)):
            smart_summaries = await service.get_slot_smart_summaries(
                [slot.slot for slot in snapshot.slots],
                selected_enclosure_id=enclosure_id,
            )
        smart_summary_cache = {
            str(item.slot): item.summary.model_dump(mode="json")
            for item in smart_summaries
        }
        exporter = get_snapshot_export_service()
        try:
            with perf_stage("route.export_snapshot.build_artifact"):
                artifact = await exporter.build_enclosure_snapshot_export(
                    request=request,
                    snapshot=snapshot,
                    smart_summary_cache=smart_summary_cache,
                    selected_slot=payload.selected_slot,
                    history_window_hours=payload.history_window_hours,
                    history_panel_open=payload.history_panel_open,
                    io_chart_mode=payload.io_chart_mode,
                    redact_sensitive=payload.redact_sensitive,
                    packaging=payload.packaging,
                    allow_oversize=payload.allow_oversize,
                )
        except SnapshotExportTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "X-Export-Size-Bytes": str(artifact.size_bytes),
                "X-Export-HTML-Size-Bytes": str(artifact.html_size_bytes),
                "X-Export-Packaging": artifact.packaging,
                "X-Export-Redaction": artifact.redaction,
                "X-Export-Size-Limit-Bytes": str(artifact.size_limit_bytes),
            },
        )

    @app.post("/api/export/enclosure-snapshot/estimate")
    async def estimate_enclosure_snapshot(
        request: Request,
        payload: SnapshotExportRequest,
        system_id: str | None = None,
        enclosure_id: str | None = None,
    ) -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(system_id)
        add_perf_metadata(system_id=service.system.id, platform=service.system.truenas.platform, enclosure_id=enclosure_id)
        with perf_stage("route.export_snapshot_estimate.load_snapshot"):
            snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
        with perf_stage("route.export_snapshot_estimate.load_smart_summaries", slot_count=len(snapshot.slots)):
            smart_summaries = await service.get_slot_smart_summaries(
                [slot.slot for slot in snapshot.slots],
                selected_enclosure_id=enclosure_id,
            )
        smart_summary_cache = {
            str(item.slot): item.summary.model_dump(mode="json")
            for item in smart_summaries
        }
        exporter = get_snapshot_export_service()
        with perf_stage("route.export_snapshot_estimate.build_estimate"):
            estimate = await exporter.estimate_enclosure_snapshot_export(
                request=request,
                snapshot=snapshot,
                smart_summary_cache=smart_summary_cache,
                selected_slot=payload.selected_slot,
                history_window_hours=payload.history_window_hours,
                history_panel_open=payload.history_panel_open,
                io_chart_mode=payload.io_chart_mode,
                redact_sensitive=payload.redact_sensitive,
                packaging=payload.packaging,
                allow_oversize=payload.allow_oversize,
            )
        return JSONResponse(estimate)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        registry = get_inventory_registry()
        service = registry.get_service(None)
        snapshot = await service.get_snapshot()
        api_status = snapshot.sources.get("api")
        return JSONResponse(
            {
                "status": "ok",
                "dependency_status": "ok" if api_status and api_status.ok else "degraded",
                "last_updated": snapshot.last_updated.isoformat(),
                "sources": snapshot.model_dump(mode="json").get("sources", {}),
                "warnings": snapshot.warnings,
            },
            status_code=200,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"ok": False, "detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled application error")
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    return app


def build_index_context(
    *,
    request: Request,
    snapshot: InventorySnapshot,
    storage_view_runtime: StorageViewRuntimePayload,
    settings: Settings,
    history_configured: bool,
    admin_launch_url: str | None = None,
    snapshot_mode: bool = False,
    snapshot_export_meta: dict[str, object] | None = None,
    snapshot_export_meta_json: str = "null",
    preloaded_history_json: str = "{}",
    preloaded_smart_summary_json: str = "{}",
    preloaded_history_summary_json: str = "{\"counts\": {}, \"collector\": {}}",
    initial_selected_slot_json: str = "null",
    initial_history_timeframe_hours_json: str = "24",
    initial_history_panel_open_json: str = "false",
    initial_history_io_chart_mode_json: str = '"total"',
) -> dict[str, object]:
    return {
        "request": request,
        "snapshot": snapshot,
        "storage_view_runtime": storage_view_runtime,
        "settings": settings,
        "initial_snapshot_json": json.dumps(snapshot.model_dump(mode="json")),
        "initial_storage_view_runtime_json": json.dumps(storage_view_runtime.model_dump(mode="json")),
        "history_configured": history_configured,
        "snapshot_mode": snapshot_mode,
        "snapshot_export_meta": snapshot_export_meta or {},
        "snapshot_export_meta_json": snapshot_export_meta_json,
        "preloaded_history_json": preloaded_history_json,
        "preloaded_smart_summary_json": preloaded_smart_summary_json,
        "preloaded_history_summary_json": preloaded_history_summary_json,
        "initial_selected_slot_json": initial_selected_slot_json,
        "initial_history_timeframe_hours_json": initial_history_timeframe_hours_json,
        "initial_history_panel_open_json": initial_history_panel_open_json,
        "initial_history_io_chart_mode_json": initial_history_io_chart_mode_json,
        "admin_launch_url": admin_launch_url,
    }


def ensure_slot_bounds(settings: Settings, slot: int) -> None:
    if slot < 0 or slot >= settings.layout.slot_count:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is outside configured layout.")


def resolve_admin_launch_url(request: Request, settings: Settings) -> str | None:
    service_url = str(settings.admin.service_url or "").strip()
    if not service_url:
        return None

    health_url = f"{service_url.rstrip('/')}/healthz"
    try:
        with urllib.request.urlopen(health_url, timeout=settings.admin.timeout_seconds) as response:
            if getattr(response, "status", 200) >= 400:
                return None
    except (urllib.error.URLError, ValueError):
        return None

    public_url = str(settings.admin.public_url or "").strip()
    if public_url:
        return public_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.hostname}:{settings.admin.port}"


app = create_app()
