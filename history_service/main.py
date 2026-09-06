from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.logging_config import configure_service_logging
from app.metrics import install_metrics
from app.script_json import register_script_json_filters
from app.services.history_status import project_public_collector_status
from app.services.release_status import ReleaseStatusService
from history_service.collector import HistoryCollectionAlreadyRunning, HistoryCollector
from history_service.config import HistorySettings, get_history_settings
from history_service.operation_bounds import (
    HISTORY_READ_BUSY_DETAIL,
    HISTORY_READ_RETRY_AFTER_SECONDS,
    MAX_CONCURRENT_BULK_HISTORY_READS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    BulkHistoryReadAdmission,
    HistoryBudgetExceeded,
    HistoryReadBusy,
    HistoryReadPlan,
    HistoryRequestShapeError,
    build_history_read_plan,
    count_history_rows,
)
from history_service.refresh_auth import (
    ManualRefreshAdmission,
    authorize_refresh_request,
    read_limited_request_body,
    read_refresh_document,
)
from history_service.store import HistoryStore

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
register_script_json_filters(templates.env)

configure_service_logging(
    log_level=os.getenv("APP_LOG_LEVEL", "INFO"),
    log_format=os.getenv("LOG_FORMAT", "text"),
    service_name="enclosure-history",
)


def build_history_store(settings: HistorySettings) -> HistoryStore:
    return HistoryStore(
        settings.sqlite_path,
        segment_catalog_path=settings.segment_catalog_path,
        permission_repair_enabled=settings.permission_repair_enabled,
        shared_dir_mode=settings.shared_dir_mode,
        shared_file_mode=settings.shared_file_mode,
    )


settings = get_history_settings()
store = build_history_store(settings)
collector = HistoryCollector(settings, store)
logger = logging.getLogger(__name__)
refresh_admission = ManualRefreshAdmission(
    cooldown_seconds=settings.full_refresh_cooldown_seconds,
)
bulk_history_read_admission = BulkHistoryReadAdmission(
    max_concurrency=MAX_CONCURRENT_BULK_HISTORY_READS,
)
bulk_history_read_operations: set[asyncio.Task[tuple[list[dict[str, object]], int]]] = set()
HISTORY_COLLECTOR_ERROR_DETAIL = "History collector error; see service logs."
SLOT_HISTORY_METRIC_LIMITS: dict[str, int] = {
    "temperature_c": 96,
    "bytes_read": 60,
    "bytes_written": 60,
    "annualized_bytes_read": 60,
    "annualized_bytes_written": 60,
    "power_on_hours": 60,
}


def _duplicate_key_rejector(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload


def _history_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, HistoryBudgetExceeded):
        return JSONResponse(
            {
                "detail": f"History request exceeds {exc.limit_name} limit.",
                "limit": exc.limit,
            },
            status_code=413,
        )
    return JSONResponse({"detail": "History request shape is invalid."}, status_code=422)


def _json_string_size(value: str) -> int:
    size = 2
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"} or character in "\b\f\n\r\t":
            size += 2
        elif codepoint <= 0x1F:
            size += 6
        else:
            size += len(character.encode("utf-8"))
    return size


def _json_key_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Out of range float values are not JSON compliant")
        return repr(value)
    raise TypeError("History response JSON object keys must be strings or scalar values.")


def _json_value_size(value: object) -> int:
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, str):
        return _json_string_size(value)
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Out of range float values are not JSON compliant")
        return len(repr(value))
    if isinstance(value, (list, tuple)):
        return 2 + max(0, len(value) - 1) + sum(_json_value_size(item) for item in value)
    if isinstance(value, dict):
        size = 2 + max(0, len(value) - 1)
        for key, item in value.items():
            size += _json_string_size(_json_key_text(key)) + 1 + _json_value_size(item)
        return size
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _set_exact_response_size(payload: dict[str, object]) -> int:
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        return _json_value_size(payload)
    budget["response_bytes"] = 0
    size_without_response_byte_digits = _json_value_size(payload) - 1
    digit_count = len(str(size_without_response_byte_digits + 1))
    response_bytes = size_without_response_byte_digits + digit_count
    if len(str(response_bytes)) != digit_count:
        response_bytes = size_without_response_byte_digits + len(str(response_bytes))
    budget["response_bytes"] = response_bytes
    return response_bytes


def bounded_history_json_response(
    payload: dict[str, object],
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> JSONResponse:
    response_bytes = _set_exact_response_size(payload)
    if response_bytes > max_bytes:
        return JSONResponse(
            {
                "detail": "History response exceeds serialized byte limit.",
                "limit": MAX_RESPONSE_BYTES,
            },
            status_code=413,
        )
    response = JSONResponse(payload)
    if len(response.body) != response_bytes:
        raise RuntimeError("History response byte accounting mismatch.")
    return response


async def _execute_history_plan(plan: HistoryReadPlan) -> tuple[list[dict[str, object]], int]:
    scope_payloads: list[dict[str, object]] = []
    returned_rows = 0
    for scope in plan.scopes:
        histories = await asyncio.to_thread(
            store.list_scope_history,
            scope.system_id,
            scope.enclosure_id,
            slots=list(scope.slots),
            event_limit=plan.event_limit,
            since=plan.since,
            metric_limits=dict(plan.metric_limits),
        )
        returned_rows += count_history_rows(histories.values())
        if returned_rows > plan.projected_rows:
            raise HistoryBudgetExceeded("returned_rows", returned_rows, plan.projected_rows)
        scope_payloads.append(
            {
                "system_id": scope.system_id,
                "enclosure_id": scope.enclosure_id,
                "histories": histories,
            }
        )
    return scope_payloads, returned_rows


async def _execute_admitted_history_plan(
    plan: HistoryReadPlan,
) -> tuple[list[dict[str, object]], int]:
    admission = bulk_history_read_admission
    if not admission.try_acquire():
        raise HistoryReadBusy(HISTORY_READ_BUSY_DETAIL)

    async def execute_and_release() -> tuple[list[dict[str, object]], int]:
        try:
            return await _execute_history_plan(plan)
        finally:
            admission.release()

    operation = asyncio.create_task(execute_and_release())
    bulk_history_read_operations.add(operation)
    operation.add_done_callback(_finish_bulk_history_operation)
    return await asyncio.shield(operation)


def _finish_bulk_history_operation(
    operation: asyncio.Task[tuple[list[dict[str, object]], int]],
) -> None:
    bulk_history_read_operations.discard(operation)
    if not operation.cancelled():
        operation.exception()


def _history_read_busy_response() -> JSONResponse:
    return JSONResponse(
        {"detail": HISTORY_READ_BUSY_DETAIL},
        status_code=503,
        headers={"Retry-After": str(HISTORY_READ_RETRY_AFTER_SECONDS)},
    )


def public_collector_status(
    status: object,
    *,
    last_error_detail: str = HISTORY_COLLECTOR_ERROR_DETAIL,
) -> dict[str, object]:
    return project_public_collector_status(
        status,
        last_error_detail=last_error_detail,
    )


def safe_http_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


@lru_cache
def get_release_status_service() -> ReleaseStatusService:
    return ReleaseStatusService(
        current_version=__version__,
        enabled=settings.release_check_enabled,
        repo_full_name=settings.release_check_repo,
        interval_seconds=settings.release_check_interval_seconds,
        timeout_seconds=settings.release_check_timeout_seconds,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    release_task = asyncio.create_task(get_release_status_service().run_periodic_refresh())
    await collector.start()
    try:
        yield
    finally:
        if release_task is not None and not release_task.done():
            release_task.cancel()
            try:
                await release_task
            except asyncio.CancelledError:
                pass
        await collector.stop()


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
install_metrics(app, service_name="enclosure-history", version=__version__)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, exact_counts: bool = Query(default=False)) -> HTMLResponse:
    status = public_collector_status(collector.status())
    counts = cast(
        dict[str, object],
        await asyncio.to_thread(store.counts if exact_counts else store.estimated_counts),
    )
    scopes = await asyncio.to_thread(store.list_scopes, include_activity_counts=exact_counts)
    database_size_bytes = await asyncio.to_thread(store.database_size_bytes)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        build_dashboard_context(
            request=request,
            status=status,
            counts=counts,
            scopes=scopes,
            app_version=__version__,
            release_status=get_release_status_service().snapshot(),
            database_size_bytes=database_size_bytes,
        ),
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    collector_status = public_collector_status(collector.status())
    payload = {
        "status": "ok" if not collector.last_error else "degraded",
        "collector": collector_status,
        "database_size_bytes": await asyncio.to_thread(store.database_size_bytes),
        **collector_status,
    }
    return JSONResponse(payload, status_code=200)


@app.get("/livez")
async def livez() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
        },
        status_code=200,
    )


@app.get("/api/history/overview")
async def overview(exact_counts: bool = Query(default=False)) -> dict[str, object]:
    counts = await asyncio.to_thread(store.counts if exact_counts else store.estimated_counts)
    return {
        "collector": public_collector_status(collector.status()),
        "counts": counts,
        "counts_exact": exact_counts or counts.get("estimated") is False,
        "database": {
            "size_bytes": await asyncio.to_thread(store.database_size_bytes),
        },
        "scopes": await asyncio.to_thread(store.list_scopes, include_activity_counts=exact_counts),
    }


@app.post("/api/history/refresh", response_model=None)
async def refresh_history(request: Request) -> dict[str, object] | JSONResponse:
    authorize_refresh_request(request, settings)
    normalized_mode = await read_refresh_document(request)
    if collector.collection_running:
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": "History collection already running.",
            },
            status_code=409,
        )
    admission = await refresh_admission.try_acquire(normalized_mode)
    if not admission.accepted:
        detail = (
            "History full refresh is cooling down."
            if admission.status_code == 429
            else "History refresh already running."
        )
        headers = (
            {"Retry-After": str(admission.retry_after)}
            if admission.retry_after is not None
            else None
        )
        return JSONResponse(
            {"ok": False, "mode": normalized_mode, "detail": detail},
            status_code=admission.status_code or 409,
            headers=headers,
        )
    try:
        await collector.run_once(
            force_fast=True,
            force_slow=normalized_mode == "full",
            include_due_intervals=False,
            cached_root_only=normalized_mode == "fast",
        )
    except HistoryCollectionAlreadyRunning:
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": "History collection already running.",
            },
            status_code=409,
        )
    except Exception:  # noqa: BLE001 - report manual collection failures as structured API errors.
        logger.exception("Manual history %s refresh failed", normalized_mode)
        failure_detail = f"History {normalized_mode} refresh failed; see service logs."
        collector.last_error = failure_detail
        try:
            payload = await overview(exact_counts=False)
            collector_payload = payload.get("collector")
            payload["collector"] = public_collector_status(
                collector_payload if isinstance(collector_payload, dict) else {},
                last_error_detail=failure_detail,
            )
        except Exception:  # noqa: BLE001 - keep the original refresh failure visible even if summary loading also fails.
            logger.exception("Manual history %s refresh failed while loading summary payload", normalized_mode)
            payload = {
                "collector": public_collector_status(collector.status(), last_error_detail=failure_detail),
                "counts": {},
                "counts_exact": False,
                "scopes": [],
            }
        return JSONResponse(
            {
                "ok": False,
                "mode": normalized_mode,
                "detail": failure_detail,
                **payload,
            },
            status_code=500,
        )
    finally:
        await refresh_admission.release()
    payload = await overview(exact_counts=False)
    return {
        "ok": True,
        "mode": normalized_mode,
        "detail": "History full refresh completed." if normalized_mode == "full" else "History fast refresh completed.",
        **payload,
    }


@app.get("/api/history/slots/{slot}/events")
async def slot_events(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    return {
        "events": await asyncio.to_thread(
            store.list_slot_events,
            system_id,
            enclosure_id,
            slot,
            limit=limit,
        ),
    }


@app.get("/api/history/slots/{slot}/metrics")
async def slot_metrics(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    return {
        "samples": await asyncio.to_thread(
            store.list_metric_samples,
            system_id,
            enclosure_id,
            slot,
            metric_name=metric_name,
            limit=limit,
            since=since,
        ),
    }


@app.get("/api/history/slots/{slot}/bundle")
async def slot_history_bundle(
    slot: int,
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    event_limit: int = Query(default=12, ge=1, le=1000),
) -> dict[str, object]:
    return await asyncio.to_thread(
        store.get_slot_history_bundle,
        system_id,
        enclosure_id,
        slot,
        event_limit=event_limit,
        metric_limits=SLOT_HISTORY_METRIC_LIMITS,
        since=since,
    )


@app.get("/api/history/scopes/slots")
async def scope_slot_history(
    system_id: str = Query(...),
    enclosure_id: str | None = Query(default=None),
    slots: list[int] | None = Query(default=None),
    metrics: list[str] | None = Query(default=None),
    since: str | None = Query(default=None),
    event_limit: int = Query(default=12),
    metric_limit: int = 60,
) -> JSONResponse:
    try:
        plan = build_history_read_plan(
            scopes=[{"system_id": system_id, "enclosure_id": enclosure_id, "slots": slots or []}],
            metrics=metrics or SLOT_HISTORY_METRIC_LIMITS.keys(),
            since=since,
            event_limit=event_limit,
            metric_limit=metric_limit,
        )
        scope_payloads, returned_rows = await _execute_admitted_history_plan(plan)
    except HistoryReadBusy:
        return _history_read_busy_response()
    except (HistoryRequestShapeError, HistoryBudgetExceeded) as exc:
        raise HTTPException(
            status_code=413 if isinstance(exc, HistoryBudgetExceeded) else 422,
            detail=(
                f"History request exceeds {exc.limit_name} limit ({exc.limit})."
                if isinstance(exc, HistoryBudgetExceeded)
                else "History request shape is invalid."
            ),
        ) from exc
    histories = cast(dict[int, dict[str, object]], scope_payloads[0]["histories"])
    budget = plan.with_result(returned_row_count=returned_rows, response_bytes=0).budget_metadata()
    return bounded_history_json_response(
        {
            "histories": {
                str(slot): {
                    "slot": slot,
                    "system_id": system_id,
                    "enclosure_id": enclosure_id,
                    "events": payload.get("events", []),
                    "metrics": payload.get("metrics", {}),
                    "sample_counts": payload.get("sample_counts", {}),
                    "latest_values": payload.get("latest_values", {}),
                }
                for slot, payload in histories.items()
            },
            "budget": budget,
        }
    )


@app.post("/api/history/scopes/bundle")
async def scopes_history_bundle(request: Request) -> JSONResponse:
    try:
        body = await read_limited_request_body(
            request,
            limit=MAX_REQUEST_BYTES,
            detail=f"History request exceeds request_bytes limit ({MAX_REQUEST_BYTES}).",
        )
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail, "limit": MAX_REQUEST_BYTES}, status_code=exc.status_code)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _history_error_response(HistoryRequestShapeError("invalid content type"))
    try:
        document = json.loads(body, object_pairs_hook=_duplicate_key_rejector)
        if not isinstance(document, dict) or set(document) != {
            "scopes", "metrics", "since", "event_limit", "metric_limit"
        }:
            raise HistoryRequestShapeError("invalid document fields")
        plan = build_history_read_plan(
            scopes=document["scopes"],
            metrics=document["metrics"],
            since=document["since"],
            event_limit=document["event_limit"],
            metric_limit=document["metric_limit"],
        )
        scope_payloads, returned_rows = await _execute_admitted_history_plan(plan)
    except HistoryReadBusy:
        return _history_read_busy_response()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, HistoryBudgetExceeded):
            return _history_error_response(exc)
        return _history_error_response(
            exc if isinstance(exc, HistoryRequestShapeError) else HistoryRequestShapeError("invalid document")
        )
    budget = plan.with_result(returned_row_count=returned_rows, response_bytes=0).budget_metadata()
    return bounded_history_json_response({"scopes": scope_payloads, "budget": budget})


def format_count(value: object, *, estimated: bool = False) -> str:
    if value is None:
        return "deferred"
    prefix = "~" if estimated else ""
    return f"{prefix}{value}"


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.1f} {unit}"


def _duration_seconds(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def format_duration(value: object) -> str:
    seconds = _duration_seconds(value)
    minutes = int(seconds // 60)
    remainder = int(seconds % 60)
    if minutes <= 0:
        return f"{remainder}s"
    return f"{minutes}m {remainder}s"


def dashboard_activity_labels(status: dict[str, object]) -> tuple[str, str]:
    current_collection = "not running"
    if status.get("collection_running"):
        collection_kind = str(status.get("collection_kind") or "background")
        collection_duration = format_duration(status.get("collection_elapsed_seconds"))
        collection_activity = str(status.get("collection_activity") or "working")
        current_collection = (
            f"{collection_kind} for {collection_duration}: {collection_activity}"
        )
        return (
            current_collection,
            f"History {collection_kind} collection running for "
            f"{collection_duration}: {collection_activity}.",
        )

    backoff_seconds = status.get("background_backoff_seconds_remaining")
    if _duration_seconds(backoff_seconds) > 0:
        return (
            current_collection,
            "History background collection is backed off for "
            f"{format_duration(backoff_seconds)} after repeated failures.",
        )
    return current_collection, ""


def collection_duration_label(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.1f}s"
    return "not recorded"


def collection_inventory_label(value: object) -> str:
    if value is True:
        return "forced"
    if value is False:
        return "cached"
    return "not recorded"


def build_dashboard_context(
    *,
    request: Request,
    status: dict[str, object],
    counts: dict[str, object],
    scopes: list[dict[str, object]],
    app_version: str,
    release_status: dict[str, object] | None = None,
    database_size_bytes: int = 0,
) -> dict[str, object]:
    counts_are_estimated = bool(counts.get("estimated"))
    release_payload = release_status or {}
    backoff_seconds = int(status.get("background_backoff_seconds_remaining") or 0)
    current_collection_label, collector_banner_text = dashboard_activity_labels(status)
    return {
        "request": request,
        "app_name": settings.app_name,
        "app_version": app_version,
        "status": status,
        "counts": counts,
        "scopes": scopes,
        "counts_are_estimated": counts_are_estimated,
        "database_size_label": format_bytes(database_size_bytes),
        "release_summary": str(release_payload.get("summary") or "Checking releases..."),
        "latest_url": safe_http_url(release_payload.get("latest_url")),
        "backoff_label": f"{backoff_seconds}s remaining" if backoff_seconds > 0 else "inactive",
        "current_collection_label": current_collection_label,
        "collector_banner_text": collector_banner_text,
        "direct_refresh_enabled": settings.refresh_auth_mode == "network",
        "last_collection_duration_label": collection_duration_label(
            status.get("last_collection_duration_seconds")
        ),
        "last_background_overrun_label": collection_duration_label(
            status.get("last_background_overrun_seconds")
        ),
        "last_retention_duration_label": collection_duration_label(
            status.get("last_retention_duration_seconds")
        ),
        "last_inventory_mode": collection_inventory_label(
            status.get("last_collection_inventory_forced")
        ),
        "format_count": format_count,
        "status_json": json.dumps(status),
    }
