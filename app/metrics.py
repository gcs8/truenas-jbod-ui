from __future__ import annotations

import os
import platform
import time
from datetime import datetime

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, Info, generate_latest
from starlette.responses import Response

METRICS_NAMESPACE = "truenas_jbod_ui"
DEFAULT_METRICS_PATH = "/metrics"
HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)
INVENTORY_DURATION_BUCKETS = HTTP_DURATION_BUCKETS + (120.0,)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Completed HTTP requests.",
    labelnames=("service", "method", "route", "status_code"),
    namespace=METRICS_NAMESPACE,
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("service", "method", "route"),
    namespace=METRICS_NAMESPACE,
    buckets=HTTP_DURATION_BUCKETS,
)
HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently in progress.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
BUILD_INFO = Info(
    "build",
    "Static build metadata for the running service.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
SERVICE_UP = Gauge(
    "service_up",
    "Whether the service process is initialized.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_COLLECTION_RUNS_TOTAL = Counter(
    "history_collection_runs_total",
    "History collector passes.",
    labelnames=("service", "result"),
    namespace=METRICS_NAMESPACE,
)
HISTORY_COLLECTION_DURATION_SECONDS = Histogram(
    "history_collection_duration_seconds",
    "History collector pass duration in seconds.",
    labelnames=("service", "result"),
    namespace=METRICS_NAMESPACE,
    buckets=HTTP_DURATION_BUCKETS,
)
HISTORY_COLLECTOR_RUNNING = Gauge(
    "history_collector_running",
    "Whether the history collector background loop is running.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_SCOPE_COUNT = Gauge(
    "history_last_scope_count",
    "Number of inventory scopes seen in the latest collector pass.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_TRACKED_SLOTS = Gauge(
    "history_tracked_slots",
    "Tracked slot rows currently stored by the history service.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_EVENT_COUNT = Gauge(
    "history_event_count",
    "Slot event rows currently stored by the history service.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_METRIC_SAMPLE_COUNT = Gauge(
    "history_metric_sample_count",
    "Metric sample rows currently stored by the history service.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_ERROR = Gauge(
    "history_last_error",
    "Whether the latest collector pass ended in an error.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_INVENTORY_TIMESTAMP = Gauge(
    "history_last_inventory_timestamp_seconds",
    "Unix timestamp of the last inventory snapshot seen by the collector.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_FAST_TIMESTAMP = Gauge(
    "history_last_fast_metrics_timestamp_seconds",
    "Unix timestamp of the last fast-metrics collection pass.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_SLOW_TIMESTAMP = Gauge(
    "history_last_slow_metrics_timestamp_seconds",
    "Unix timestamp of the last slow-metrics collection pass.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_SUCCESS_TIMESTAMP = Gauge(
    "history_last_success_timestamp_seconds",
    "Unix timestamp of the last successful history collector pass.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
HISTORY_LAST_BACKUP_TIMESTAMP = Gauge(
    "history_last_backup_timestamp_seconds",
    "Unix timestamp of the last history backup snapshot.",
    labelnames=("service",),
    namespace=METRICS_NAMESPACE,
)
INVENTORY_SNAPSHOT_REQUESTS_TOTAL = Counter(
    "inventory_snapshot_requests_total",
    "Inventory snapshot requests by cache outcome.",
    labelnames=("service", "system_id", "platform", "cache_state"),
    namespace=METRICS_NAMESPACE,
)
INVENTORY_SNAPSHOT_BUILD_DURATION_SECONDS = Histogram(
    "inventory_snapshot_build_duration_seconds",
    "Inventory snapshot rebuild duration in seconds.",
    labelnames=("service", "system_id", "platform", "trigger", "topology"),
    namespace=METRICS_NAMESPACE,
    buckets=INVENTORY_DURATION_BUCKETS,
)
INVENTORY_SOURCE_BUNDLE_REQUESTS_TOTAL = Counter(
    "inventory_source_bundle_requests_total",
    "Inventory source-bundle requests by cache outcome.",
    labelnames=("service", "system_id", "platform", "cache_state"),
    namespace=METRICS_NAMESPACE,
)
INVENTORY_SOURCE_BUNDLE_BUILD_DURATION_SECONDS = Histogram(
    "inventory_source_bundle_build_duration_seconds",
    "Inventory source-bundle rebuild duration in seconds.",
    labelnames=("service", "system_id", "platform", "trigger"),
    namespace=METRICS_NAMESPACE,
    buckets=INVENTORY_DURATION_BUCKETS,
)
INVENTORY_SNAPSHOT_CACHE_ENTRIES = Gauge(
    "inventory_snapshot_cache_entries",
    "Current in-memory snapshot cache entries for this system service.",
    labelnames=("service", "system_id", "platform"),
    namespace=METRICS_NAMESPACE,
)
SMART_SUMMARY_CACHE_ENTRIES = Gauge(
    "smart_summary_cache_entries",
    "Current in-memory SMART summary cache entries for this system service.",
    labelnames=("service", "system_id", "platform"),
    namespace=METRICS_NAMESPACE,
)
SMART_SUMMARY_REQUESTS_TOTAL = Counter(
    "smart_summary_requests_total",
    "SMART summary requests by cache/source outcome.",
    labelnames=("service", "system_id", "platform", "cache_state"),
    namespace=METRICS_NAMESPACE,
)


def metrics_enabled() -> bool:
    raw_value = str(os.getenv("METRICS_ENABLED", "true")).strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def metrics_path() -> str:
    raw_path = str(os.getenv("METRICS_PATH", DEFAULT_METRICS_PATH)).strip() or DEFAULT_METRICS_PATH
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    return raw_path.rstrip("/") or DEFAULT_METRICS_PATH


def install_metrics(app: FastAPI, *, service_name: str, version: str) -> None:
    if getattr(app, "_metrics_installed", False):
        return
    setattr(app, "_metrics_installed", True)
    _set_build_info(service_name, version)
    if not metrics_enabled():
        return

    metrics_mount_path = metrics_path()

    @app.get(metrics_mount_path, include_in_schema=False)
    async def prometheus_metrics_endpoint() -> Response:
        return Response(
            content=generate_latest(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    @app.middleware("http")
    async def prometheus_metrics_middleware(request: Request, call_next) -> Response:
        if request.url.path == metrics_mount_path:
            return await call_next(request)

        labels = {"service": service_name}
        HTTP_REQUESTS_IN_PROGRESS.labels(**labels).inc()
        started = time.perf_counter()
        response: Response | None = None
        status_code = "500"
        try:
            response = await call_next(request)
            status_code = str(response.status_code)
            return response
        finally:
            route_label = _route_label(request)
            elapsed = max(0.0, time.perf_counter() - started)
            HTTP_REQUESTS_TOTAL.labels(
                service=service_name,
                method=request.method,
                route=route_label,
                status_code=status_code,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                service=service_name,
                method=request.method,
                route=route_label,
            ).observe(elapsed)
            HTTP_REQUESTS_IN_PROGRESS.labels(**labels).dec()


def set_history_collector_running(service_name: str, running: bool) -> None:
    if not metrics_enabled():
        return
    HISTORY_COLLECTOR_RUNNING.labels(service=service_name).set(1 if running else 0)


def observe_history_collection_run(
    *,
    service_name: str,
    result: str,
    duration_seconds: float,
    status: dict[str, object],
    counts: dict[str, int] | None = None,
) -> None:
    if not metrics_enabled():
        return

    normalized_result = result if result in {"success", "error"} else "error"
    HISTORY_COLLECTION_RUNS_TOTAL.labels(service=service_name, result=normalized_result).inc()
    HISTORY_COLLECTION_DURATION_SECONDS.labels(service=service_name, result=normalized_result).observe(
        max(0.0, duration_seconds)
    )
    HISTORY_LAST_SCOPE_COUNT.labels(service=service_name).set(int(status.get("last_scope_count") or 0))
    HISTORY_LAST_ERROR.labels(service=service_name).set(1 if status.get("last_error") else 0)
    _set_timestamp_metric(
        HISTORY_LAST_INVENTORY_TIMESTAMP,
        service_name,
        status.get("last_inventory_at"),
    )
    _set_timestamp_metric(
        HISTORY_LAST_FAST_TIMESTAMP,
        service_name,
        status.get("last_fast_metrics_at"),
    )
    _set_timestamp_metric(
        HISTORY_LAST_SLOW_TIMESTAMP,
        service_name,
        status.get("last_slow_metrics_at"),
    )
    _set_timestamp_metric(
        HISTORY_LAST_SUCCESS_TIMESTAMP,
        service_name,
        status.get("last_success_at"),
    )
    _set_timestamp_metric(
        HISTORY_LAST_BACKUP_TIMESTAMP,
        service_name,
        status.get("last_backup_at"),
    )
    if counts is not None:
        HISTORY_TRACKED_SLOTS.labels(service=service_name).set(int(counts.get("tracked_slots") or 0))
        HISTORY_EVENT_COUNT.labels(service=service_name).set(int(counts.get("event_count") or 0))
        HISTORY_METRIC_SAMPLE_COUNT.labels(service=service_name).set(int(counts.get("metric_sample_count") or 0))


def observe_inventory_snapshot_request(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    cache_state: str,
) -> None:
    if not metrics_enabled():
        return
    INVENTORY_SNAPSHOT_REQUESTS_TOTAL.labels(
        service=service_name,
        system_id=_normalize_metric_label(system_id),
        platform=_normalize_metric_label(platform),
        cache_state=_normalize_metric_label(cache_state),
    ).inc()


def observe_inventory_snapshot_build(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    trigger: str,
    topology: str,
    duration_seconds: float,
) -> None:
    if not metrics_enabled():
        return
    INVENTORY_SNAPSHOT_BUILD_DURATION_SECONDS.labels(
        service=service_name,
        system_id=_normalize_metric_label(system_id),
        platform=_normalize_metric_label(platform),
        trigger=_normalize_metric_label(trigger),
        topology=_normalize_metric_label(topology),
    ).observe(max(0.0, duration_seconds))


def observe_inventory_source_bundle_request(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    cache_state: str,
) -> None:
    if not metrics_enabled():
        return
    INVENTORY_SOURCE_BUNDLE_REQUESTS_TOTAL.labels(
        service=service_name,
        system_id=_normalize_metric_label(system_id),
        platform=_normalize_metric_label(platform),
        cache_state=_normalize_metric_label(cache_state),
    ).inc()


def observe_inventory_source_bundle_build(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    trigger: str,
    duration_seconds: float,
) -> None:
    if not metrics_enabled():
        return
    INVENTORY_SOURCE_BUNDLE_BUILD_DURATION_SECONDS.labels(
        service=service_name,
        system_id=_normalize_metric_label(system_id),
        platform=_normalize_metric_label(platform),
        trigger=_normalize_metric_label(trigger),
    ).observe(max(0.0, duration_seconds))


def observe_inventory_cache_sizes(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    snapshot_entries: int,
    smart_entries: int,
) -> None:
    if not metrics_enabled():
        return
    labels = {
        "service": service_name,
        "system_id": _normalize_metric_label(system_id),
        "platform": _normalize_metric_label(platform),
    }
    INVENTORY_SNAPSHOT_CACHE_ENTRIES.labels(**labels).set(max(0, int(snapshot_entries)))
    SMART_SUMMARY_CACHE_ENTRIES.labels(**labels).set(max(0, int(smart_entries)))


def observe_smart_summary_request(
    *,
    service_name: str,
    system_id: str | None,
    platform: str | None,
    cache_state: str,
) -> None:
    if not metrics_enabled():
        return
    SMART_SUMMARY_REQUESTS_TOTAL.labels(
        service=service_name,
        system_id=_normalize_metric_label(system_id),
        platform=_normalize_metric_label(platform),
        cache_state=_normalize_metric_label(cache_state),
    ).inc()


def _set_build_info(service_name: str, version: str) -> None:
    SERVICE_UP.labels(service=service_name).set(1)
    if metrics_enabled():
        BUILD_INFO.labels(service=service_name).info(
            {
                "version": version,
                "python_version": platform.python_version(),
            }
        )


def _set_timestamp_metric(metric: Gauge, service_name: str, raw_timestamp: object) -> None:
    parsed = _parse_timestamp(raw_timestamp)
    if parsed is None:
        return
    metric.labels(service=service_name).set(parsed)


def _parse_timestamp(raw_timestamp: object) -> float | None:
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        return None
    try:
        return datetime.fromisoformat(raw_timestamp).timestamp()
    except ValueError:
        return None


def _normalize_metric_label(raw_value: str | None, *, default: str = "unknown") -> str:
    value = str(raw_value or "").strip()
    return value or default


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return "unmatched"
