from __future__ import annotations

# Route collaborators remain public here for runtime monkeypatch compatibility.
# ruff: noqa: F401

import asyncio
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Collection, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from app.read_ui_auth_config import load_read_ui_auth_settings
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
    DiskInventorySyncRequest,
    SMART_BATCH_MAX_SLOTS,
    InventoryReadResponse,
    InventorySnapshot,
    LedAction,
    LedRequest,
    MappingBundle,
    MappingImportConfirmation,
    MappingRequest,
    SasFabricAliasRequest,
    SnapshotExportRequest,
    SmartBatchItem,
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
from app.services.history_backend import HistoryBackendClient, HistoryBackendPolicyError
from app.services.inventory import (
    DiskInventorySyncBusy,
    SnapshotStateBusyError,
    UnknownEnclosureError,
)
from app.services.inventory_registry import InventoryRegistry, SystemNotConfiguredError
from app.services.mapping_store import (
    MappingDurabilityError,
    MappingImportDigestMismatch,
    MappingRevisionConflict,
    MappingScopeConflict,
)
from app.services.profile_registry import build_profile_reference_warnings
from app.services.release_status import ReleaseStatusService
from app.services.snapshot_export import (
    SnapshotExportBusyError,
    SnapshotExportService,
    SnapshotExportTooLargeError,
    collect_configured_hostnames,
)
from app.services.storage_writability import probe_writable_directories
from app.services.truenas_ws import TrueNASAPIError
from app.services import upgrade_notice
from history_service.operation_bounds import (
    ALLOWED_HISTORY_METRICS,
    HistoryBudgetExceeded,
    HistoryRequestShapeError,
    build_history_read_plan,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
register_script_json_filters(templates.env)

logger = logging.getLogger(__name__)
INVALID_MAPPING_BUNDLE_DETAIL = "Mapping bundle is invalid."


class HistoryRefreshProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fast", "full"]


class HistoryScopeProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_id: str
    enclosure_id: str | None = None
    slots: list[int]


class HistoryScopesProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[HistoryScopeProxyRequest]
    metrics: list[str]
    since: str
    event_limit: int
    metric_limit: int


@dataclass(frozen=True)
class ErrorResponseSpec:
    """How one service exception reaches the browser: status, wording, and retry hint."""

    status_code: int
    detail: str | None = None
    retry_after_seconds: int | None = None

    def headers(self) -> dict[str, str] | None:
        if self.retry_after_seconds is None:
            return None
        return {"Retry-After": str(self.retry_after_seconds)}

    def detail_for(self, exc: Exception) -> str:
        return self.detail if self.detail is not None else str(exc)


UNKNOWN_ENCLOSURE_DETAIL = "Requested enclosure is not available for this system."
ENCLOSURE_LAYOUT_UNAVAILABLE_DETAIL = "Unable to resolve selected enclosure layout."

EXCEPTION_RESPONSES: dict[type[Exception], ErrorResponseSpec] = {
    SystemNotConfiguredError: ErrorResponseSpec(status_code=404),
    UnknownEnclosureError: ErrorResponseSpec(status_code=404, detail=UNKNOWN_ENCLOSURE_DETAIL),
    SnapshotStateBusyError: ErrorResponseSpec(status_code=503, retry_after_seconds=1),
    SnapshotExportBusyError: ErrorResponseSpec(status_code=503, retry_after_seconds=5),
}


def error_response_spec(exc: Exception) -> ErrorResponseSpec:
    for exc_type in type(exc).__mro__:
        spec = EXCEPTION_RESPONSES.get(exc_type)
        if spec is not None:
            return spec
    raise KeyError(type(exc).__name__)


def http_exception_for(exc: Exception) -> HTTPException:
    spec = error_response_spec(exc)
    return HTTPException(
        status_code=spec.status_code,
        detail=spec.detail_for(exc),
        headers=spec.headers(),
    )


async def mapped_exception_handler(
    _: Request,
    exc: Exception,
) -> JSONResponse:
    spec = error_response_spec(exc)
    return JSONResponse(
        {"ok": False, "detail": spec.detail_for(exc)},
        status_code=spec.status_code,
        headers=spec.headers(),
    )


async def mapping_scope_conflict_exception_handler(
    _: Request,
    _exc: Exception,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "mapping_scope_conflict",
            "detail": MappingScopeConflict.public_detail,
        },
        status_code=409,
    )


async def mapping_durability_exception_handler(
    _: Request,
    _exc: Exception,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "mapping_durability_indeterminate",
            "detail": MappingDurabilityError.public_detail,
        },
        status_code=503,
    )


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


READ_UI_SIGN_IN_REQUIRED_REASON = "Sign in to make changes."
READ_UI_WRITE_POLICY_UNAVAILABLE_REASON = "Changes are disabled because the sign-in settings could not be read."


def build_read_ui_write_policy(auth_settings: Any | None) -> dict[str, object]:
    """Describe whether the main UI's write controls can succeed (#273).

    Network mode is the no-auth default. Basic mode keeps writes disabled until
    the operator signs in. Missing or unknown settings fail closed.
    """

    auth_mode = getattr(auth_settings, "auth_mode", None)
    if auth_mode == "basic":
        return {
            "enabled": False,
            "mode": "basic",
            "reason": READ_UI_SIGN_IN_REQUIRED_REASON,
        }
    if auth_mode == "network":
        return {
            "enabled": True,
            "mode": "network",
            "reason": "",
        }
    return {
        "enabled": False,
        "mode": "",
        "reason": READ_UI_WRITE_POLICY_UNAVAILABLE_REASON,
    }


def resolve_read_ui_write_policy(request: Request) -> dict[str, object]:
    try:
        app_state = getattr(request.app, "state", None)
    except (KeyError, AttributeError):
        app_state = None
    return build_read_ui_write_policy(getattr(app_state, "operator_auth_settings", None))


def require_read_ui_basic_credentials(request: Request) -> None:
    auth_settings = request.app.state.operator_auth_settings
    if auth_settings.auth_mode != "basic":
        raise HTTPException(
            status_code=403,
            detail="Sign-in is not enabled on this server.",
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


def require_read_ui_mutation_authorization(request: Request) -> None:
    auth_settings = request.app.state.operator_auth_settings
    if auth_settings.auth_mode == "network":
        public_origin = (
            request.app.state.read_ui_public_origin
            or f"{request.url.scheme}://{request.url.netloc}"
        )
        if not request_origin_allowed(request, public_origin):
            raise HTTPException(
                status_code=403,
                detail="This request came from a different site and was blocked.",
            )
        return
    if auth_settings.auth_mode != "basic":
        raise HTTPException(
            status_code=403,
            detail="Changes are disabled because the sign-in settings could not be read.",
        )
    require_read_ui_basic_credentials(request)
    if not request_origin_allowed(request, request.app.state.read_ui_public_origin):
        raise HTTPException(
            status_code=403,
            detail="This request came from a different site and was blocked.",
        )


def create_app() -> FastAPI:
    startup_settings = get_settings()
    operator_auth_settings = load_read_ui_auth_settings()
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
    writable_directories = tuple(ui_writable_directories(startup_settings))
    startup_problems = probe_writable_directories(writable_directories)
    for problem in startup_problems:
        logger.error("%s", problem)
    app.state.startup_problems = tuple(startup_problems)
    app.state.writable_directories = writable_directories
    app.state.storage_checked_at_monotonic = time.monotonic()

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    install_metrics(app, service_name="enclosure-ui", version=__version__)
    install_perf_timing_middleware(app, startup_settings)

    from app.route_compat import include_router_preserving_route_objects
    from app.routes import build_router

    include_router_preserving_route_objects(app, build_router(sys.modules[__name__]))
    for mapped_exception_type in EXCEPTION_RESPONSES:
        app.add_exception_handler(mapped_exception_type, mapped_exception_handler)
    app.add_exception_handler(
        MappingScopeConflict,
        mapping_scope_conflict_exception_handler,
    )
    app.add_exception_handler(
        MappingDurabilityError,
        mapping_durability_exception_handler,
    )

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
            {"ok": False, "detail": "Something went wrong on the server. The application log has details."},
            status_code=500,
        )

    return app


def upgrade_notice_data_dir(settings: Settings) -> Path:
    """The data directory that holds the saved bay assignments and the version record."""
    return Path(settings.paths.mapping_file).parent


def build_index_context(
    *,
    request: Request,
    snapshot: InventorySnapshot,
    storage_view_runtime: StorageViewRuntimePayload,
    settings: Settings,
    history_configured: bool,
    read_ui_mutation_auth_mode: str = "network",
    admin_launch_url: str | None = None,
    admin_launch_stopped: bool = False,
    app_version: str = __version__,
    release_status: dict[str, object] | None = None,
    upgrade_notice_payload: dict[str, str] | None = None,
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
    system_notice: str | None = None,
) -> dict[str, object]:
    sas_fabric_view_url = (
        "#sas-fabric-panel"
        if snapshot_mode
        else request.url_for("sas_fabric_view").path
    )
    write_policy = resolve_read_ui_write_policy(request)
    return {
        "request": request,
        "snapshot": snapshot,
        "storage_view_runtime": storage_view_runtime,
        "settings": settings,
        "initial_snapshot_json": json.dumps(snapshot.model_dump(mode="json")),
        "initial_storage_view_runtime_json": json.dumps(storage_view_runtime.model_dump(mode="json")),
        "history_configured": history_configured,
        "read_ui_mutation_auth_mode": read_ui_mutation_auth_mode,
        "app_version": app_version,
        "release_status": release_status or {},
        "upgrade_notice": upgrade_notice_payload if not snapshot_mode else None,
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
        "system_notice": system_notice,
        "admin_launch_stopped": admin_launch_stopped,
        "write_policy": write_policy,
        "write_policy_json": json.dumps(write_policy),
    }


def check_slot_bounds(slot: int, layout_slots: Collection[int]) -> None:
    if slot < 0 or slot not in layout_slots:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is not part of this enclosure.")


def snapshot_layout_slots(snapshot: Any) -> frozenset[int]:
    """Return the set of slot numbers the snapshot actually renders.

    ``layout_slot_count`` only says how many bays a view shows; a drawer
    sub-view such as the MD1280 bottom drawer shows 42 bays numbered 42-83,
    and operator profiles may use absolute or noncontiguous bay ids (#275).
    The rendered ``SlotView`` numbers are authoritative; the layout rows stand
    in before any bay has been rendered, and a bare count is only trusted when
    the snapshot carries neither (a zero-based ``range``).
    """
    rendered = {int(view.slot) for view in (getattr(snapshot, "slots", None) or [])}
    if rendered:
        return frozenset(rendered)
    layout_rows = getattr(snapshot, "layout_rows", None) or []
    positioned = {int(slot) for row in layout_rows for slot in (row or []) if slot is not None}
    if positioned:
        return frozenset(positioned)
    layout_slot_count = int(getattr(snapshot, "layout_slot_count", 0) or 0)
    return frozenset(range(max(layout_slot_count, 0)))


async def resolve_layout_slots(
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> frozenset[int]:
    """Return the slot numbers the selected enclosure view actually renders.

    A global ``LAYOUT_SLOT_COUNT`` cannot represent mixed shelves or systems
    with large disk inventories (#168, #213), and a bay *count* cannot
    represent a view whose bays do not start at zero (#275). Missing, empty,
    or mismatched snapshot evidence therefore fails closed instead of
    permitting a mutation against an unrelated bound.
    """
    if service is None:
        raise HTTPException(status_code=503, detail=ENCLOSURE_LAYOUT_UNAVAILABLE_DETAIL)
    try:
        snapshot = await service.get_snapshot(
            selected_enclosure_id=selected_enclosure_id,
            allow_stale_cache=True,
        )
    except (UnknownEnclosureError, SnapshotStateBusyError) as exc:
        raise http_exception_for(exc) from exc
    except Exception as exc:  # noqa: BLE001 - expose a stable route error, not source details
        logger.debug("Slot bounds: selected enclosure snapshot unavailable (%s)", exc)
        raise HTTPException(
            status_code=503,
            detail=ENCLOSURE_LAYOUT_UNAVAILABLE_DETAIL,
        ) from exc
    if selected_enclosure_id and snapshot.selected_enclosure_id != selected_enclosure_id:
        raise HTTPException(status_code=404, detail=UNKNOWN_ENCLOSURE_DETAIL)
    layout_slots = snapshot_layout_slots(snapshot)
    if not layout_slots:
        raise HTTPException(status_code=503, detail=ENCLOSURE_LAYOUT_UNAVAILABLE_DETAIL)
    return layout_slots


async def ensure_slot_bounds(
    slot: int,
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> None:
    if slot < 0:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is not part of this enclosure.")
    check_slot_bounds(slot, await resolve_layout_slots(service, selected_enclosure_id))


async def resolve_read_layout_slots(
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> tuple[frozenset[int] | None, str]:
    try:
        return await resolve_layout_slots(service, selected_enclosure_id), "verified"
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return None, "unavailable"


async def ensure_read_slot_bounds(
    slot: int,
    service: Any | None = None,
    selected_enclosure_id: str | None = None,
) -> str:
    if slot < 0:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is not part of this enclosure.")
    layout_slots, layout_bounds = await resolve_read_layout_slots(service, selected_enclosure_id)
    if layout_slots is not None:
        check_slot_bounds(slot, layout_slots)
    return layout_bounds


@dataclass(frozen=True, slots=True)
class AdminLaunchState:
    """What the System Setup button shows.

    ``url`` is set when admin answered its health probe; ``stopped`` is set when
    admin is configured but did not answer, the normal state once it has
    stopped itself after its idle timeout.
    """

    url: str | None
    stopped: bool


@dataclass(slots=True)
class AdminProbeCacheEntry:
    reachable: bool
    expires_at_monotonic: float


ADMIN_PROBE_SUCCESS_TTL_SECONDS = 30.0
ADMIN_PROBE_FAILURE_TTL_SECONDS = 10.0
ADMIN_PROBE_CACHE: dict[str, AdminProbeCacheEntry] = {}


def _probe_admin_service(service_url: str, timeout_seconds: float) -> bool:
    health_url = f"{service_url.rstrip('/')}/healthz"
    health_request = urllib.request.Request(
        health_url,
        headers=request_id_headers({"Accept": "application/json"}),
    )
    try:
        with urllib.request.urlopen(health_request, timeout=timeout_seconds) as response:
            return getattr(response, "status", 200) < 400
    except (TimeoutError, urllib.error.URLError, ValueError):
        return False


def admin_service_reachable(service_url: str, timeout_seconds: float) -> bool:
    """Probe admin's health endpoint, remembering the answer briefly.

    Admin is stopped by design most of the time, so without this every page
    load would wait on a refused connection or a DNS miss.
    """

    now = time.monotonic()
    cached = ADMIN_PROBE_CACHE.get(service_url)
    if cached is not None and cached.expires_at_monotonic > now:
        return cached.reachable
    reachable = _probe_admin_service(service_url, timeout_seconds)
    ttl_seconds = ADMIN_PROBE_SUCCESS_TTL_SECONDS if reachable else ADMIN_PROBE_FAILURE_TTL_SECONDS
    ADMIN_PROBE_CACHE[service_url] = AdminProbeCacheEntry(
        reachable=reachable,
        expires_at_monotonic=now + ttl_seconds,
    )
    return reachable


def resolve_admin_launch_url(request: Request, settings: Settings) -> AdminLaunchState | None:
    service_url = str(settings.admin.service_url or "").strip()
    if not service_url:
        return None
    if not admin_service_reachable(service_url, settings.admin.timeout_seconds):
        return AdminLaunchState(url=None, stopped=True)

    public_url = str(settings.admin.public_url or "").strip()
    if public_url:
        return AdminLaunchState(url=public_url.rstrip("/"), stopped=False)
    return AdminLaunchState(
        url=f"{request.url.scheme}://{request.url.hostname}:{settings.admin.port}",
        stopped=False,
    )


def ui_writable_directories(settings: Settings) -> list[str]:
    """Directories the main UI writes: data files, logs and the known-hosts file.

    The config directory is left out on purpose: the UI only reads it, and the
    default Compose file mounts it read-only.
    """

    paths = settings.paths
    candidates = [
        paths.mapping_file,
        paths.sas_fabric_alias_file,
        paths.slot_detail_cache_file,
        paths.log_file,
        settings.ssh.known_hosts_path,
    ]
    return [str(Path(candidate).parent) for candidate in candidates if candidate]


def startup_problems_for(request: Request) -> list[str]:
    app_state = getattr(getattr(request, "app", None), "state", None)
    return [str(problem) for problem in (getattr(app_state, "startup_problems", None) or ())]


STORAGE_REPROBE_SECONDS = 30.0


def refresh_storage_problems(request: Request) -> list[str]:
    """Re-run the writability probe at most every 30 seconds and return its lines.

    A chown on the Docker host then clears the red health state without a
    container restart, and a bind mount that turns read-only while the app runs
    is noticed. Only the startup lines are returned when the app state carries
    no probe directories (tests, or an app built without ``create_app``).
    """

    app_state = getattr(getattr(request, "app", None), "state", None)
    directories = tuple(getattr(app_state, "writable_directories", None) or ())
    previous = tuple(str(problem) for problem in (getattr(app_state, "startup_problems", None) or ()))
    if app_state is None or not directories:
        return list(previous)
    now = time.monotonic()
    checked_at = getattr(app_state, "storage_checked_at_monotonic", None)
    if isinstance(checked_at, (int, float)) and now - checked_at < STORAGE_REPROBE_SECONDS:
        return list(previous)
    current = tuple(probe_writable_directories(directories))
    for problem in current:
        if problem not in previous:
            logger.error("%s", problem)
    if previous and not current:
        logger.info("Data, log and known-hosts folders are writable again.")
    app_state.startup_problems = current
    app_state.storage_checked_at_monotonic = now
    return list(current)


@dataclass(slots=True)
class HistoryProbeCacheEntry:
    problem: str | None
    expires_at_monotonic: float


HISTORY_PROBE_SUCCESS_TTL_SECONDS = 30.0
HISTORY_PROBE_FAILURE_TTL_SECONDS = 10.0
HISTORY_PROBE_MAX_TIMEOUT_SECONDS = 2.0
HISTORY_PROBE_CACHE: dict[str, HistoryProbeCacheEntry] = {}
HISTORY_UNAVAILABLE_PROBLEM = "History service unavailable"


def _probe_history_service(service_url: str, timeout_seconds: float) -> str | None:
    """Return one plain problem line for the history sidecar, or None.

    A host name that does not resolve means the optional history container is
    not deployed (Docker only resolves service names of running containers), so
    it is not reported: the default Compose file names the sidecar even when
    its profile is off. Everything else that fails is outside this container,
    so it can only ever degrade health, never take it down.
    """

    health_url = f"{service_url.rstrip('/')}/healthz"
    health_request = urllib.request.Request(
        health_url,
        headers=request_id_headers({"Accept": "application/json"}),
    )
    try:
        with urllib.request.urlopen(health_request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read(64 * 1024) or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            return f"{HISTORY_UNAVAILABLE_PROBLEM}: it reports a local storage fault; see the history service log."
        return f"{HISTORY_UNAVAILABLE_PROBLEM}: it answered HTTP {exc.code}."
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            return None
        if isinstance(exc.reason, ConnectionRefusedError):
            return f"{HISTORY_UNAVAILABLE_PROBLEM}: connection refused."
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return f"{HISTORY_UNAVAILABLE_PROBLEM}: no answer within {timeout_seconds:g} seconds."
        return f"{HISTORY_UNAVAILABLE_PROBLEM}: it could not be reached."
    except (TimeoutError, socket.timeout):
        return f"{HISTORY_UNAVAILABLE_PROBLEM}: no answer within {timeout_seconds:g} seconds."
    except (OSError, ValueError):
        return f"{HISTORY_UNAVAILABLE_PROBLEM}: it returned an unreadable answer."
    if not isinstance(payload, dict):
        return f"{HISTORY_UNAVAILABLE_PROBLEM}: it returned an unreadable answer."
    if payload.get("status") == "degraded":
        detail = str(payload.get("detail") or "").strip() or "see the history service log."
        return f"History service degraded: {detail}"
    return None


def history_service_problem(settings: Settings) -> str | None:
    """Probe the configured history sidecar, remembering the answer briefly."""

    service_url = str(settings.history.service_url or "").strip()
    if not service_url:
        return None
    now = time.monotonic()
    cached = HISTORY_PROBE_CACHE.get(service_url)
    if cached is not None and cached.expires_at_monotonic > now:
        return cached.problem
    timeout_seconds = min(float(settings.history.timeout_seconds), HISTORY_PROBE_MAX_TIMEOUT_SECONDS)
    problem = _probe_history_service(service_url, timeout_seconds)
    ttl_seconds = HISTORY_PROBE_FAILURE_TTL_SECONDS if problem else HISTORY_PROBE_SUCCESS_TTL_SECONDS
    HISTORY_PROBE_CACHE[service_url] = HistoryProbeCacheEntry(
        problem=problem,
        expires_at_monotonic=now + ttl_seconds,
    )
    return problem


HEALTH_SUMMARY_ALL_OK = "All sources OK"
HEALTH_SUMMARY_WAITING = "Waiting for the first inventory"
HEALTH_SOURCE_LABELS = {"api": "TrueNAS API", "ssh": "SSH", "bmc": "BMC/IPMI"}


def build_health_payload(
    snapshot: InventorySnapshot | None,
    *,
    startup_problems: Collection[str] = (),
    remote_problems: Collection[str] = (),
) -> dict[str, object]:
    """Describe main-UI health in three levels (#429).

    - ``ok``: nothing to act on. Waiting for the first inventory is not a problem.
    - ``degraded``: something outside this container is unhealthy: the TrueNAS
      API, SSH, the BMC, or the history sidecar. The app keeps serving what it
      has, so the route still answers HTTP 200.
    - ``down``: a local fault the container cannot operate through: a data,
      log or known-hosts folder it cannot write (``startup_problems``). The
      route answers HTTP 503.

    ``summary`` is one sentence; ``problems`` lists every reason, local first.
    """

    unwritable = [str(problem) for problem in startup_problems]
    remote: list[str] = []
    if snapshot is None:
        dependency_status = "unknown"
        last_updated = None
        sources: dict[str, object] = {}
        warnings: list[str] = []
        cache_state = "empty"
    else:
        api_status = snapshot.sources.get("api")
        dependency_status = "ok" if api_status and api_status.ok else "degraded"
        last_updated = snapshot.last_updated.isoformat()
        sources = {name: status.model_dump(mode="json") for name, status in snapshot.sources.items()}
        warnings = list(snapshot.warnings)
        cache_state = "cached"
        if dependency_status == "degraded":
            # ok=False covers both a failed API and one that answers with degraded
            # enclosure data, so name the state neutrally and keep the recorded cause.
            api_message = (api_status.message if api_status else None) or "no details recorded"
            remote.append(f"TrueNAS API degraded: {api_message}")
        for name in ("ssh", "bmc"):
            status = snapshot.sources.get(name)
            if status is not None and status.enabled and not status.ok:
                message = status.message or "no details recorded"
                remote.append(f"{HEALTH_SOURCE_LABELS[name]} degraded: {message}")
    remote.extend(str(problem) for problem in remote_problems if problem)

    problems = [*unwritable, *remote]
    if unwritable:
        status_text = "down"
        summary = "Data folder not writable: " + "; ".join(unwritable)
    elif remote:
        status_text = "degraded"
        summary = remote[0] if len(remote) == 1 else f"{remote[0]} (and {len(remote) - 1} more)"
    else:
        status_text = "ok"
        summary = HEALTH_SUMMARY_WAITING if snapshot is None else HEALTH_SUMMARY_ALL_OK
    return {
        "status": status_text,
        "summary": summary,
        "problems": problems,
        "dependency_status": dependency_status,
        "last_updated": last_updated,
        "sources": sources,
        "warnings": warnings,
        "cache_state": cache_state,
    }


def health_status_code(payload: dict[str, object]) -> int:
    """HTTP status for a health payload: 503 only when the container itself is down."""

    return 503 if payload.get("status") == "down" else 200

app = create_app()
