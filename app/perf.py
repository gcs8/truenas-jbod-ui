from __future__ import annotations

import logging
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from fastapi import FastAPI, Request
from starlette.responses import Response

from app import __version__
from app.config import PerfConfig, Settings
from app.request_context import (
    REQUEST_ID_HEADER,
    current_request_id,
    generate_request_id,
    validate_request_id,
)

logger = logging.getLogger("app.perf")


@dataclass(slots=True)
class PerfStageSample:
    label: str
    duration_ms: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PerfTrace:
    request_id: str
    operation: str
    started_at: float = field(default_factory=time.perf_counter)
    metadata: dict[str, Any] = field(default_factory=dict)
    stages: list[PerfStageSample] = field(default_factory=list)

    @contextmanager
    def stage(self, label: str, **detail: Any) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(
                PerfStageSample(
                    label=label,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    detail={key: value for key, value in detail.items() if value is not None},
                )
            )

    def add_metadata(self, **detail: Any) -> None:
        self.metadata.update({key: value for key, value in detail.items() if value is not None})

    @property
    def duration_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000

    def has_slow_stage(self, threshold_ms: int) -> bool:
        return any(stage.duration_ms >= threshold_ms for stage in self.stages)

    def stage_summary(self, *, limit: int = 8) -> str:
        grouped: OrderedDict[str, dict[str, float | int]] = OrderedDict()
        for stage in self.stages:
            bucket = grouped.setdefault(stage.label, {"total_ms": 0.0, "count": 0})
            bucket["total_ms"] = float(bucket["total_ms"]) + stage.duration_ms
            bucket["count"] = int(bucket["count"]) + 1
        ranked = sorted(grouped.items(), key=lambda item: float(item[1]["total_ms"]), reverse=True)
        parts: list[str] = []
        for label, payload in ranked[:limit]:
            total_ms = float(payload["total_ms"])
            count = int(payload["count"])
            suffix = f" x{count}" if count > 1 else ""
            parts.append(f"{label}={total_ms:.1f}ms{suffix}")
        remaining = len(ranked) - limit
        if remaining > 0:
            parts.append(f"+{remaining} more")
        return ", ".join(parts)

    def stage_rollups(self, *, limit: int = 8) -> list[tuple[str, float, int]]:
        grouped: OrderedDict[str, dict[str, float | int]] = OrderedDict()
        for stage in self.stages:
            bucket = grouped.setdefault(stage.label, {"total_ms": 0.0, "count": 0})
            bucket["total_ms"] = float(bucket["total_ms"]) + stage.duration_ms
            bucket["count"] = int(bucket["count"]) + 1
        ranked = sorted(grouped.items(), key=lambda item: float(item[1]["total_ms"]), reverse=True)
        return [
            (
                label,
                round(float(payload["total_ms"]), 1),
                int(payload["count"]),
            )
            for label, payload in ranked[:limit]
        ]


_CURRENT_TRACE: ContextVar[PerfTrace | None] = ContextVar("current_perf_trace", default=None)


def get_perf_trace() -> PerfTrace | None:
    return _CURRENT_TRACE.get()


def add_perf_metadata(**detail: Any) -> None:
    trace = get_perf_trace()
    if trace is not None:
        trace.add_metadata(**detail)


def perf_stage(label: str, **detail: Any):
    trace = get_perf_trace()
    if trace is None:
        return nullcontext()
    return trace.stage(label, **detail)


def _should_log_trace(trace: PerfTrace, perf: PerfConfig) -> bool:
    return perf.log_all_requests or trace.duration_ms >= perf.slow_request_ms or trace.has_slow_stage(perf.slow_stage_ms)


def _log_trace(trace: PerfTrace, perf: PerfConfig, *, status_code: int, method: str, route: str) -> None:
    if not _should_log_trace(trace, perf):
        return
    level = logging.WARNING if trace.duration_ms >= perf.slow_request_ms or trace.has_slow_stage(perf.slow_stage_ms) else logging.INFO
    logger.log(
        level,
        "http_performance",
        extra={
            "event": "http_performance",
            "component": "enclosure-ui",
            "release": __version__,
            "request_id": trace.request_id,
            "method": method,
            "route": route,
            "status_code": status_code,
            "duration_ms": round(trace.duration_ms, 1),
        },
    )


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if isinstance(route_path, str) and route_path else "unmatched"


def _quote_server_timing_desc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_server_timing_header(trace: PerfTrace, *, stage_limit: int = 8) -> str:
    metrics = [f'app;desc="total";dur={trace.duration_ms:.1f}']
    for index, (label, total_ms, count) in enumerate(trace.stage_rollups(limit=stage_limit), start=1):
        desc = label if count <= 1 else f"{label} x{count}"
        metrics.append(f'stage-{index};desc="{_quote_server_timing_desc(desc)}";dur={total_ms:.1f}')
    return ", ".join(metrics)


def install_perf_timing_middleware(app: FastAPI, settings: Settings) -> None:
    if not settings.perf.enabled:
        return

    @app.middleware("http")
    async def perf_timing_middleware(request: Request, call_next) -> Response:
        trace = PerfTrace(
            request_id=current_request_id() or generate_request_id(),
            operation=request.method,
        )
        token = _CURRENT_TRACE.set(trace)
        response: Response | None = None
        try:
            current_response = await call_next(request)
            response = current_response
            response_request_id = validate_request_id(current_response.headers.get(REQUEST_ID_HEADER))
            if response_request_id is not None:
                trace.request_id = response_request_id
            else:
                current_response.headers[REQUEST_ID_HEADER] = trace.request_id
            current_response.headers["Server-Timing"] = build_server_timing_header(trace)
            return current_response
        finally:
            _CURRENT_TRACE.reset(token)
            _log_trace(
                trace,
                settings.perf,
                status_code=response.status_code if response is not None else 500,
                method=request.method,
                route=_route_label(request),
            )
