from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from fastapi import FastAPI, Request
from starlette.responses import Response

from app.config import PerfConfig, Settings

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


def _log_trace(trace: PerfTrace, perf: PerfConfig, *, status_code: int | None, method: str, path: str) -> None:
    if not _should_log_trace(trace, perf):
        return
    level = logging.WARNING if trace.duration_ms >= perf.slow_request_ms or trace.has_slow_stage(perf.slow_stage_ms) else logging.INFO
    metadata = " ".join(f"{key}={value}" for key, value in sorted(trace.metadata.items()))
    metadata_suffix = f" {metadata}" if metadata else ""
    stage_summary = trace.stage_summary()
    stage_suffix = f" stages=[{stage_summary}]" if stage_summary else ""
    logger.log(
        level,
        "Perf request id=%s %s %s status=%s duration_ms=%.1f%s%s",
        trace.request_id,
        method,
        path,
        status_code if status_code is not None else "unknown",
        trace.duration_ms,
        metadata_suffix,
        stage_suffix,
    )


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
            request_id=uuid.uuid4().hex[:8],
            operation=f"{request.method} {request.url.path}",
        )
        trace.add_metadata(method=request.method, path=request.url.path)
        request.state.request_id = trace.request_id
        token = _CURRENT_TRACE.set(trace)
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = trace.request_id
            response.headers["Server-Timing"] = build_server_timing_header(trace)
            return response
        finally:
            _CURRENT_TRACE.reset(token)
            _log_trace(
                trace,
                settings.perf,
                status_code=response.status_code if response is not None else None,
                method=request.method,
                path=request.url.path,
            )
