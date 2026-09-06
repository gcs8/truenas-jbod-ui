from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

ALLOWED_HISTORY_METRICS = (
    "temperature_c",
    "bytes_read",
    "bytes_written",
    "annualized_bytes_read",
    "annualized_bytes_written",
    "power_on_hours",
)
HISTORY_METRIC_MAX_LIMITS = {
    "temperature_c": 96,
    "bytes_read": 60,
    "bytes_written": 60,
    "annualized_bytes_read": 60,
    "annualized_bytes_written": 60,
    "power_on_hours": 60,
}
INTERNAL_HISTORY_METRIC_MAX_LIMITS = {**HISTORY_METRIC_MAX_LIMITS, "temperature": 96}
MAX_SCOPES = 32
MAX_TARGETS = 347
MAX_METRICS = len(ALLOWED_HISTORY_METRICS)
MAX_EVENT_ROWS = 4096
MAX_RETURNED_ROWS = 36000
MAX_HISTORY_HOURS = 8760
HISTORY_WINDOW_TRANSIT_TOLERANCE_SECONDS = 1
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 25165824
MAX_METRIC_LIMIT = 96
MAX_CONCURRENT_BULK_HISTORY_READS = 4
HISTORY_READ_BUSY_DETAIL = "History read capacity is temporarily busy; retry later."
HISTORY_READ_RETRY_AFTER_SECONDS = 1


class HistoryRequestShapeError(ValueError):
    """A history request has a malformed or ambiguous shape."""


class HistoryBudgetExceeded(ValueError):
    """A history request exceeds one named public server-owned budget."""

    def __init__(self, limit_name: str, value: int, limit: int) -> None:
        self.limit_name = limit_name
        self.value = value
        self.limit = limit
        super().__init__(f"History request exceeds {limit_name} limit ({limit}).")


class HistoryReadBusy(RuntimeError):
    """The shared anonymous bulk-history read boundary is full."""


class BulkHistoryReadAdmission:
    def __init__(self, *, max_concurrency: int = MAX_CONCURRENT_BULK_HISTORY_READS) -> None:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("Bulk history read concurrency must be a positive integer.")
        self.max_concurrency = max_concurrency
        self._state_lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self) -> bool:
        with self._state_lock:
            if self._in_flight >= self.max_concurrency:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._state_lock:
            if self._in_flight < 1:
                raise RuntimeError("Bulk history read admission release is unbalanced.")
            self._in_flight -= 1


@dataclass(frozen=True)
class HistoryScope:
    system_id: str
    enclosure_id: str | None
    slots: tuple[int, ...]


@dataclass(frozen=True)
class HistoryReadPlan:
    scopes: tuple[HistoryScope, ...]
    metrics: tuple[str, ...]
    since: str
    event_limit: int
    metric_limit: int
    window_hours: int
    scope_count: int
    target_count: int
    projected_event_rows: int
    projected_rows: int
    returned_row_count: int = 0
    response_bytes: int = 0

    @property
    def metric_limits(self) -> Mapping[str, int]:
        return MappingProxyType({
            metric: min(self.metric_limit, HISTORY_METRIC_MAX_LIMITS[metric])
            for metric in self.metrics
        })

    def budget_metadata(self) -> dict[str, int]:
        return {
            "scope_count": self.scope_count,
            "target_count": self.target_count,
            "projected_event_rows": self.projected_event_rows,
            "projected_returned_rows": self.projected_rows,
            "returned_row_count": self.returned_row_count,
            "response_bytes": self.response_bytes,
        }

    def with_result(self, *, returned_row_count: int, response_bytes: int) -> "HistoryReadPlan":
        return replace(self, returned_row_count=returned_row_count, response_bytes=response_bytes)


def _strict_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise HistoryRequestShapeError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _normalize_since(value: object, *, now: datetime) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise HistoryRequestShapeError("since is required for bulk history reads.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryRequestShapeError("since must be a valid timezone-aware timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryRequestShapeError("since must be timezone-aware.")
    current = now
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed_seconds = (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    maximum_elapsed_seconds = MAX_HISTORY_HOURS * 3600 + HISTORY_WINDOW_TRANSIT_TOLERANCE_SECONDS
    if elapsed_seconds < 3600 or elapsed_seconds > maximum_elapsed_seconds:
        raise HistoryRequestShapeError(f"since must bound history to between 1 and {MAX_HISTORY_HOURS} hours.")
    return parsed.astimezone(timezone.utc).isoformat(), min(int(elapsed_seconds // 3600), MAX_HISTORY_HOURS)


def _normalize_scope(raw_scope: object) -> tuple[HistoryScope, int]:
    if not isinstance(raw_scope, Mapping):
        raise HistoryRequestShapeError("Each history scope must be an object.")
    allowed_fields = {"system_id", "enclosure_id", "slots"}
    if set(raw_scope) - allowed_fields:
        raise HistoryRequestShapeError("History scope contains unsupported fields.")
    system_id = raw_scope.get("system_id")
    if not isinstance(system_id, str) or not system_id.strip():
        raise HistoryRequestShapeError("Each history scope requires system_id.")
    enclosure_id = raw_scope.get("enclosure_id")
    if enclosure_id is not None and not isinstance(enclosure_id, str):
        raise HistoryRequestShapeError("enclosure_id must be a string or null.")
    raw_slots = raw_scope.get("slots")
    if not isinstance(raw_slots, Sequence) or isinstance(raw_slots, (str, bytes)) or not raw_slots:
        raise HistoryRequestShapeError("Each history scope requires at least one slot.")
    if len(raw_slots) > MAX_TARGETS:
        raise HistoryBudgetExceeded("target_count", len(raw_slots), MAX_TARGETS)
    slots: list[int] = []
    for slot in raw_slots:
        if type(slot) is not int or slot < 0:
            raise HistoryRequestShapeError("History slot identifiers must be non-negative integers.")
        slots.append(slot)
    return (
        HistoryScope(
            system_id=system_id.strip(),
            enclosure_id=enclosure_id.strip() if isinstance(enclosure_id, str) and enclosure_id.strip() else None,
            slots=tuple(sorted(set(slots))),
        ),
        len(raw_slots),
    )


def build_history_read_plan(
    *,
    scopes: Iterable[object],
    metrics: Iterable[object],
    since: object,
    event_limit: object,
    metric_limit: object,
    now: datetime | None = None,
) -> HistoryReadPlan:
    raw_scopes = list(scopes)
    if not raw_scopes:
        raise HistoryRequestShapeError("At least one history scope is required.")
    if len(raw_scopes) > MAX_SCOPES:
        raise HistoryBudgetExceeded("scope_count", len(raw_scopes), MAX_SCOPES)

    normalized_scopes: list[HistoryScope] = []
    raw_target_count = 0
    identities: set[tuple[str, str | None]] = set()
    for raw_scope in raw_scopes:
        scope, raw_slot_count = _normalize_scope(raw_scope)
        raw_target_count += raw_slot_count
        if raw_target_count > MAX_TARGETS:
            raise HistoryBudgetExceeded("target_count", raw_target_count, MAX_TARGETS)
        identity = (scope.system_id, scope.enclosure_id)
        if identity in identities:
            raise HistoryRequestShapeError("Duplicate history scope identities are not allowed.")
        identities.add(identity)
        normalized_scopes.append(scope)

    raw_metrics = list(metrics)
    if not raw_metrics or len(raw_metrics) > MAX_METRICS:
        raise HistoryRequestShapeError(f"metrics must contain between 1 and {MAX_METRICS} entries.")
    normalized_metrics: list[str] = []
    for metric in raw_metrics:
        if not isinstance(metric, str) or metric not in ALLOWED_HISTORY_METRICS:
            raise HistoryRequestShapeError("metrics contains an unsupported history metric.")
        if metric not in normalized_metrics:
            normalized_metrics.append(metric)

    normalized_event_limit = _strict_int(event_limit, "event_limit", minimum=0, maximum=MAX_EVENT_ROWS)
    normalized_metric_limit = _strict_int(metric_limit, "metric_limit", minimum=1, maximum=MAX_METRIC_LIMIT)
    normalized_since, window_hours = _normalize_since(since, now=now or datetime.now(timezone.utc))
    target_count = sum(len(scope.slots) for scope in normalized_scopes)
    projected_event_rows = target_count * normalized_event_limit
    if projected_event_rows > MAX_EVENT_ROWS:
        raise HistoryBudgetExceeded("event_rows", projected_event_rows, MAX_EVENT_ROWS)
    projected_rows = target_count * (
        normalized_event_limit
        + sum(min(normalized_metric_limit, HISTORY_METRIC_MAX_LIMITS[metric]) for metric in normalized_metrics)
    )
    if projected_rows > MAX_RETURNED_ROWS:
        raise HistoryBudgetExceeded("projected_rows", projected_rows, MAX_RETURNED_ROWS)

    return HistoryReadPlan(
        scopes=tuple(normalized_scopes),
        metrics=tuple(normalized_metrics),
        since=normalized_since,
        event_limit=normalized_event_limit,
        metric_limit=normalized_metric_limit,
        window_hours=window_hours,
        scope_count=len(normalized_scopes),
        target_count=target_count,
        projected_event_rows=projected_event_rows,
        projected_rows=projected_rows,
    )


def validate_store_scope_request(
    *,
    slots: Sequence[object] | None,
    event_limit: object,
    metric_limits: Mapping[str, int] | None,
    since: object,
) -> None:
    raw_slots = list(slots or [])
    if not raw_slots:
        raise HistoryRequestShapeError("Bulk history storage reads require explicit slots.")
    if len(raw_slots) > MAX_TARGETS:
        raise HistoryBudgetExceeded("target_count", len(raw_slots), MAX_TARGETS)
    for slot in raw_slots:
        if type(slot) is not int or slot < 0:
            raise HistoryRequestShapeError("History slot identifiers must be non-negative integers.")
    normalized_slots = set(raw_slots)
    normalized_event_limit = _strict_int(event_limit, "event_limit", minimum=0, maximum=MAX_EVENT_ROWS)
    limits = dict(metric_limits or {})
    if not limits or len(limits) > MAX_METRICS:
        raise HistoryRequestShapeError("Bulk history storage reads require allowlisted metrics.")
    total_metric_rows = 0
    for metric, limit in limits.items():
        if not isinstance(metric, str) or metric not in INTERNAL_HISTORY_METRIC_MAX_LIMITS:
            raise HistoryRequestShapeError("Unsupported history metric.")
        normalized_limit = _strict_int(
            limit,
            f"{metric} limit",
            minimum=1,
            maximum=INTERNAL_HISTORY_METRIC_MAX_LIMITS[metric],
        )
        total_metric_rows += normalized_limit
    _normalize_since(since, now=datetime.now(timezone.utc))
    event_rows = len(normalized_slots) * normalized_event_limit
    if event_rows > MAX_EVENT_ROWS:
        raise HistoryBudgetExceeded("event_rows", event_rows, MAX_EVENT_ROWS)
    projected_rows = len(normalized_slots) * (normalized_event_limit + total_metric_rows)
    if projected_rows > MAX_RETURNED_ROWS:
        raise HistoryBudgetExceeded("projected_rows", projected_rows, MAX_RETURNED_ROWS)


def count_history_rows(histories: Iterable[Mapping[str, Any]]) -> int:
    total = 0
    for payload in histories:
        events = payload.get("events", [])
        if isinstance(events, list):
            total += len(events)
        metrics = payload.get("metrics", {})
        if isinstance(metrics, Mapping):
            total += sum(len(rows) for rows in metrics.values() if isinstance(rows, list))
    return total
