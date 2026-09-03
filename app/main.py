from __future__ import annotations

# Route collaborators remain public here for runtime monkeypatch compatibility.
# ruff: noqa: F401

import asyncio
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin_service.config import get_admin_settings
from app import __version__
from app.config import Settings, get_settings
from app.http_auth import (
    basic_auth_matches,
    configured_origin_identity,
    request_origin_allowed,
)
from app.logging_config import configure_logging
from app.request_context import request_id_headers
from app.models.domain import (
    InventorySnapshot,
    LedAction,
    LedRequest,
    MappingBundle,
    MappingImportConfirmation,
    MappingRequest,
    SasFabricAliasRequest,
    SnapshotExportRequest,
    SmartBatchRequest,
    SmartBatchResponse,
    SasFabricSnapshot,
    SmartSummaryView,
    StorageViewRuntimePayload,
    SystemLocatorRequest,
    SystemLocatorStatusView,
)
from app.metrics import install_metrics
from app.perf import add_perf_metadata, install_perf_timing_middleware, perf_stage
from app.script_json import register_script_json_filters
from app.services.history_backend import HistoryBackendClient
from app.services.inventory_registry import InventoryRegistry
from app.services.mapping_store import MappingImportDigestMismatch, MappingRevisionConflict
from app.services.profile_registry import build_profile_reference_warnings
from app.services.release_status import ReleaseStatusService
from app.services.snapshot_export import (
    SnapshotExportService,
    SnapshotExportTooLargeError,
    collect_configured_hostnames,
)
from app.services.truenas_ws import TrueNASAPIError

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
register_script_json_filters(templates.env)

logger = logging.getLogger(__name__)
INVALID_MAPPING_BUNDLE_DETAIL = "Mapping bundle is invalid."


@dataclass(slots=True)
class SnapshotExportSourceCacheEntry:
    stored_at_monotonic: float
    snapshot: InventorySnapshot
    smart_summary_cache: dict[str, dict[str, Any]]


SNAPSHOT_EXPORT_SOURCE_CACHE: OrderedDict[str, SnapshotExportSourceCacheEntry] = OrderedDict()


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


@lru_cache
def get_release_status_service() -> ReleaseStatusService:
    settings = get_settings()
    configure_logging(settings)
    return ReleaseStatusService(
        current_version=__version__,
        enabled=settings.app.release_check_enabled,
        repo_full_name=settings.app.release_check_repo,
        interval_seconds=settings.app.release_check_interval_seconds,
        timeout_seconds=settings.app.release_check_timeout_seconds,
    )


def _snapshot_export_source_cache_key(
    *,
    system_id: str,
    enclosure_id: str | None,
    payload: SnapshotExportRequest,
) -> str:
    request_basis = payload.model_dump(mode="json")
    request_basis.pop("packaging", None)
    request_basis.pop("allow_oversize", None)
    request_basis["system_id"] = system_id
    request_basis["enclosure_id"] = enclosure_id
    return json.dumps(request_basis, sort_keys=True, separators=(",", ":"))


def _get_snapshot_export_source_cache_entry(
    cache_key: str,
    settings: Settings,
) -> SnapshotExportSourceCacheEntry | None:
    ttl_seconds = max(0, int(settings.app.export_cache_ttl_seconds))
    if ttl_seconds <= 0:
        return None
    entry = SNAPSHOT_EXPORT_SOURCE_CACHE.get(cache_key)
    if entry is None:
        return None
    if time.monotonic() - entry.stored_at_monotonic > ttl_seconds:
        SNAPSHOT_EXPORT_SOURCE_CACHE.pop(cache_key, None)
        return None
    SNAPSHOT_EXPORT_SOURCE_CACHE.move_to_end(cache_key)
    return entry


def _store_snapshot_export_source_cache_entry(
    cache_key: str,
    *,
    snapshot: InventorySnapshot,
    smart_summary_cache: dict[str, dict[str, Any]],
    settings: Settings,
) -> None:
    ttl_seconds = max(0, int(settings.app.export_cache_ttl_seconds))
    max_entries = max(0, int(settings.app.export_cache_max_entries))
    if ttl_seconds <= 0 or max_entries <= 0:
        return
    SNAPSHOT_EXPORT_SOURCE_CACHE[cache_key] = SnapshotExportSourceCacheEntry(
        stored_at_monotonic=time.monotonic(),
        snapshot=snapshot,
        smart_summary_cache=smart_summary_cache,
    )
    SNAPSHOT_EXPORT_SOURCE_CACHE.move_to_end(cache_key)
    while len(SNAPSHOT_EXPORT_SOURCE_CACHE) > max_entries:
        SNAPSHOT_EXPORT_SOURCE_CACHE.popitem(last=False)


async def _load_snapshot_export_source(
    *,
    service: Any,
    payload: SnapshotExportRequest,
    enclosure_id: str | None,
    stage_prefix: str,
    settings: Settings,
) -> tuple[InventorySnapshot, dict[str, dict[str, Any]]]:
    cache_key = _snapshot_export_source_cache_key(
        system_id=service.system.id,
        enclosure_id=enclosure_id,
        payload=payload,
    )
    cached_entry = _get_snapshot_export_source_cache_entry(cache_key, settings)
    if cached_entry is not None:
        add_perf_metadata(snapshot_export_source_cache="hit")
        return cached_entry.snapshot, cached_entry.smart_summary_cache

    add_perf_metadata(snapshot_export_source_cache="miss")
    with perf_stage(f"{stage_prefix}.load_snapshot"):
        snapshot = await service.get_snapshot(selected_enclosure_id=enclosure_id)
    with perf_stage(f"{stage_prefix}.load_smart_summaries", slot_count=len(snapshot.slots)):
        smart_summaries = await service.get_slot_smart_summaries(
            [slot.slot for slot in snapshot.slots],
            selected_enclosure_id=enclosure_id,
            allow_stale_cache=True,
        )
    smart_summary_cache = {
        str(item.slot): item.summary.model_dump(mode="json")
        for item in smart_summaries
    }
    _store_snapshot_export_source_cache_entry(
        cache_key,
        snapshot=snapshot,
        smart_summary_cache=smart_summary_cache,
        settings=settings,
    )
    return snapshot, smart_summary_cache


def _filter_storage_view_runtime(
    runtime: StorageViewRuntimePayload,
    selected_view_ids: list[str],
) -> StorageViewRuntimePayload:
    selected_ids = {view_id for view_id in selected_view_ids if view_id}
    views = [
        view
        for view in runtime.views
        if view.enabled is not False
        and view.render.show_in_main_ui is not False
        and (not selected_ids or view.id in selected_ids)
    ]
    return StorageViewRuntimePayload(
        system_id=runtime.system_id,
        system_label=runtime.system_label,
        views=views,
    )


async def _load_storage_view_export_source(
    *,
    service: Any,
    payload: SnapshotExportRequest,
    snapshot: InventorySnapshot,
    enclosure_id: str | None,
) -> tuple[StorageViewRuntimePayload | None, dict[str, dict[str, dict[str, Any]]]]:
    if not payload.include_storage_views:
        return None, {}
    runtime = await service.get_storage_view_runtime(
        selected_enclosure_id=enclosure_id,
        snapshot=snapshot,
    )
    filtered_runtime = _filter_storage_view_runtime(runtime, payload.storage_view_ids)
    if not filtered_runtime.views:
        return filtered_runtime, {}

    smart_summary_cache: dict[str, dict[str, dict[str, Any]]] = {}
    for view in filtered_runtime.views:
        slot_cache: dict[str, dict[str, Any]] = {}
        for runtime_slot in view.slots:
            if not runtime_slot.occupied:
                continue
            try:
                summary = await service.get_storage_view_slot_smart_summary(
                    view.id,
                    runtime_slot.slot_index,
                    selected_enclosure_id=enclosure_id,
                    allow_stale_cache=True,
                )
            except TrueNASAPIError as exc:
                slot_cache[str(runtime_slot.slot_index)] = {
                    "available": False,
                    "message": str(exc),
                }
                continue
            slot_cache[str(runtime_slot.slot_index)] = summary.model_dump(mode="json")
        smart_summary_cache[view.id] = slot_cache
    return filtered_runtime, smart_summary_cache


def _selected_snapshot_export_enclosure_ids(
    *,
    payload: SnapshotExportRequest,
    snapshot: InventorySnapshot,
    current_enclosure_id: str | None,
) -> list[str]:
    primary_enclosure_id = snapshot.selected_enclosure_id or current_enclosure_id
    available_ids: list[str] = []
    seen_available: set[str] = set()
    for enclosure in snapshot.enclosures:
        if enclosure.id and enclosure.id not in seen_available:
            seen_available.add(enclosure.id)
            available_ids.append(enclosure.id)

    requested_ids = payload.enclosure_ids or available_ids
    selected_ids: list[str] = []
    seen_selected: set[str] = set()
    if primary_enclosure_id:
        selected_ids.append(primary_enclosure_id)
        seen_selected.add(primary_enclosure_id)
    if not payload.include_live_enclosures:
        return selected_ids

    for enclosure_id in requested_ids:
        if enclosure_id not in seen_available or enclosure_id in seen_selected:
            continue
        seen_selected.add(enclosure_id)
        selected_ids.append(enclosure_id)
    return selected_ids


async def _load_live_enclosure_export_sources(
    *,
    service: Any,
    payload: SnapshotExportRequest,
    snapshot: InventorySnapshot,
    smart_summary_cache: dict[str, dict[str, Any]],
    enclosure_id: str | None,
    stage_prefix: str,
    settings: Settings,
) -> tuple[dict[str, InventorySnapshot] | None, dict[str, dict[str, dict[str, Any]]] | None]:
    selected_enclosure_ids = _selected_snapshot_export_enclosure_ids(
        payload=payload,
        snapshot=snapshot,
        current_enclosure_id=enclosure_id,
    )
    if not payload.include_live_enclosures or len(selected_enclosure_ids) <= 1:
        return None, None

    snapshots_by_enclosure: dict[str, InventorySnapshot] = {}
    smart_summaries_by_enclosure: dict[str, dict[str, dict[str, Any]]] = {}
    primary_enclosure_id = snapshot.selected_enclosure_id or enclosure_id
    if primary_enclosure_id:
        snapshots_by_enclosure[primary_enclosure_id] = snapshot
        smart_summaries_by_enclosure[primary_enclosure_id] = {
            str(slot_number): summary
            for slot_number, summary in smart_summary_cache.items()
        }

    for selected_enclosure_id in selected_enclosure_ids:
        if selected_enclosure_id in snapshots_by_enclosure:
            continue
        next_snapshot, next_smart_summary_cache = await _load_snapshot_export_source(
            service=service,
            payload=payload,
            enclosure_id=selected_enclosure_id,
            stage_prefix=stage_prefix,
            settings=settings,
        )
        resolved_enclosure_id = next_snapshot.selected_enclosure_id or selected_enclosure_id
        if not resolved_enclosure_id:
            continue
        snapshots_by_enclosure[resolved_enclosure_id] = next_snapshot
        smart_summaries_by_enclosure[resolved_enclosure_id] = {
            str(slot_number): summary
            for slot_number, summary in next_smart_summary_cache.items()
        }

    add_perf_metadata(snapshot_export_live_enclosure_count=len(snapshots_by_enclosure))
    return snapshots_by_enclosure, smart_summaries_by_enclosure


def _clear_snapshot_export_source_cache_for_tests() -> None:
    SNAPSHOT_EXPORT_SOURCE_CACHE.clear()


def require_read_ui_mutation_authorization(request: Request) -> None:
    auth_settings = request.app.state.operator_auth_settings
    if auth_settings.auth_mode != "basic":
        raise HTTPException(
            status_code=403,
            detail="Read UI mutations require ADMIN_AUTH_MODE=basic.",
        )
    if not basic_auth_matches(
        request.headers.get("authorization"),
        auth_settings.auth_username,
        auth_settings.auth_password,
    ):
        raise HTTPException(
            status_code=401,
            detail="Read UI authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="truenas-jbod-ui"'},
        )
    if not request_origin_allowed(request, request.app.state.read_ui_public_origin):
        raise HTTPException(
            status_code=403,
            detail="Cross-origin Read UI mutation rejected.",
        )


def create_app() -> FastAPI:
    startup_settings = get_settings()
    operator_auth_settings = get_admin_settings()
    if (
        operator_auth_settings.auth_mode == "basic"
        and configured_origin_identity(startup_settings.app.public_origin) is None
    ):
        raise ValueError(
            "APP_PUBLIC_ORIGIN must be an absolute HTTP(S) origin when ADMIN_AUTH_MODE=basic."
        )
    configure_logging(startup_settings)
    for warning in build_profile_reference_warnings(startup_settings):
        logger.warning("Configuration warning: %s", warning["message"])

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        warm_task: asyncio.Task[None] | None = None
        release_task: asyncio.Task[None] | None = None
        if startup_settings.app.startup_warm_cache_enabled:
            registry = get_inventory_registry()
            warm_task = asyncio.create_task(
                registry.prewarm_all(warm_smart=startup_settings.app.startup_warm_smart_enabled)
            )
        release_task = asyncio.create_task(get_release_status_service().run_periodic_refresh())
        try:
            yield
        finally:
            if release_task is not None and not release_task.done():
                release_task.cancel()
                try:
                    await release_task
                except asyncio.CancelledError:
                    pass
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
    app.state.operator_auth_settings = operator_auth_settings
    app.state.read_ui_public_origin = startup_settings.app.public_origin

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    install_metrics(app, service_name="enclosure-ui", version=__version__)
    install_perf_timing_middleware(app, startup_settings)

    from app.route_compat import include_router_preserving_route_objects
    from app.routes import build_router

    include_router_preserving_route_objects(app, build_router(sys.modules[__name__]))


    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"ok": False, "detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled application error", exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(
            {"ok": False, "detail": "Unhandled application error; see application logs."},
            status_code=500,
        )

    return app


def build_index_context(
    *,
    request: Request,
    snapshot: InventorySnapshot,
    storage_view_runtime: StorageViewRuntimePayload,
    settings: Settings,
    history_configured: bool,
    admin_launch_url: str | None = None,
    app_version: str = __version__,
    release_status: dict[str, object] | None = None,
    snapshot_mode: bool = False,
    snapshot_export_meta: dict[str, object] | None = None,
    snapshot_export_meta_json: str = "null",
    preloaded_history_json: str = "{}",
    preloaded_smart_summary_json: str = "{}",
    preloaded_snapshots_json: str = "{}",
    preloaded_snapshot_smart_summary_json: str = "{}",
    preloaded_storage_view_smart_summary_json: str = "{}",
    preloaded_history_summary_json: str = "{\"counts\": {}, \"collector\": {}}",
    initial_selected_slot_json: str = "null",
    initial_selected_storage_view_id_json: str = "null",
    initial_history_timeframe_hours_json: str = "24",
    initial_history_panel_open_json: str = "false",
    initial_history_io_chart_mode_json: str = '"total"',
) -> dict[str, object]:
    sas_fabric_view_url = (
        "#sas-fabric-panel"
        if snapshot_mode
        else request.url_for("sas_fabric_view").path
    )
    return {
        "request": request,
        "snapshot": snapshot,
        "storage_view_runtime": storage_view_runtime,
        "settings": settings,
        "initial_snapshot_json": json.dumps(snapshot.model_dump(mode="json")),
        "initial_storage_view_runtime_json": json.dumps(storage_view_runtime.model_dump(mode="json")),
        "history_configured": history_configured,
        "app_version": app_version,
        "release_status": release_status or {},
        "snapshot_mode": snapshot_mode,
        "sas_fabric_view_url": sas_fabric_view_url,
        "snapshot_export_meta": snapshot_export_meta or {},
        "snapshot_export_meta_json": snapshot_export_meta_json,
        "preloaded_history_json": preloaded_history_json,
        "preloaded_smart_summary_json": preloaded_smart_summary_json,
        "preloaded_snapshots_json": preloaded_snapshots_json,
        "preloaded_snapshot_smart_summary_json": preloaded_snapshot_smart_summary_json,
        "preloaded_storage_view_smart_summary_json": preloaded_storage_view_smart_summary_json,
        "preloaded_history_summary_json": preloaded_history_summary_json,
        "initial_selected_slot_json": initial_selected_slot_json,
        "initial_selected_storage_view_id_json": initial_selected_storage_view_id_json,
        "initial_history_timeframe_hours_json": initial_history_timeframe_hours_json,
        "initial_history_panel_open_json": initial_history_panel_open_json,
        "initial_history_io_chart_mode_json": initial_history_io_chart_mode_json,
        "admin_launch_url": admin_launch_url,
    }


def check_slot_bounds(slot: int, slot_count: int) -> None:
    if slot < 0 or slot >= slot_count:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is outside configured layout.")


async def resolve_layout_slot_count(
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> int:
    """Return the selected enclosure's authoritative physical bay count.

    A global ``LAYOUT_SLOT_COUNT`` cannot represent mixed shelves or systems
    with large disk inventories (#168, #213). Missing, empty, or mismatched
    snapshot evidence therefore fails closed instead of permitting a mutation
    against an unrelated global bound.
    """
    if service is None:
        raise HTTPException(status_code=503, detail="Unable to resolve selected enclosure layout.")
    try:
        snapshot = await service.get_snapshot(
            selected_enclosure_id=selected_enclosure_id,
            allow_stale_cache=True,
        )
    except Exception as exc:  # noqa: BLE001 - expose a stable route error, not source details
        logger.debug("Slot bounds: selected enclosure snapshot unavailable (%s)", exc)
        raise HTTPException(
            status_code=503,
            detail="Unable to resolve selected enclosure layout.",
        ) from exc
    if selected_enclosure_id and snapshot.selected_enclosure_id != selected_enclosure_id:
        raise HTTPException(
            status_code=404,
            detail=f"Enclosure {selected_enclosure_id!r} is not available for this system.",
        )
    layout_slot_count = int(getattr(snapshot, "layout_slot_count", 0) or 0)
    if layout_slot_count <= 0:
        raise HTTPException(status_code=503, detail="Unable to resolve selected enclosure layout.")
    return layout_slot_count


async def ensure_slot_bounds(
    slot: int,
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> None:
    if slot < 0:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is outside configured layout.")
    check_slot_bounds(slot, await resolve_layout_slot_count(service, selected_enclosure_id))


def resolve_admin_launch_url(request: Request, settings: Settings) -> str | None:
    service_url = str(settings.admin.service_url or "").strip()
    if not service_url:
        return None

    health_url = f"{service_url.rstrip('/')}/healthz"
    health_request = urllib.request.Request(
        health_url,
        headers=request_id_headers({"Accept": "application/json"}),
    )
    try:
        with urllib.request.urlopen(health_request, timeout=settings.admin.timeout_seconds) as response:
            if getattr(response, "status", 200) >= 400:
                return None
    except (TimeoutError, urllib.error.URLError, ValueError):
        return None

    public_url = str(settings.admin.public_url or "").strip()
    if public_url:
        return public_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.hostname}:{settings.admin.port}"


app = create_app()
