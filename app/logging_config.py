from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import Settings

OBSERVABILITY_FIELDS = (
    "event",
    "component",
    "release",
    "request_id",
    "parent_request_id",
    "method",
    "route",
    "status_code",
    "duration_ms",
    "operation",
    "outcome",
    "exception_class",
)

# Formatters suppress stack traces by default so the bounded observability
# records stay free of them. A record that sets this attribute is opting in to
# full traceback rendering; only the unhandled-error diagnostic record does.
INCLUDE_TRACEBACK_FIELD = "include_traceback"
TRACEBACK_FILENAME_MAX_LENGTH = 160
TRACEBACK_FUNCTION_MAX_LENGTH = 120
TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH = 120
TRACEBACK_LINE_NUMBER_MAX_LENGTH = 20
TRACEBACK_MAX_EXCEPTION_DEPTH = 32
TRACEBACK_MAX_EXCEPTION_NODES = 64
TRACEBACK_MAX_FRAMES_PER_EXCEPTION = 32
TRACEBACK_MAX_TOTAL_FRAMES = 128
TRACEBACK_MAX_OUTPUT_LENGTH = 16_384


def _traceback_requested(record: logging.LogRecord) -> bool:
    return bool(getattr(record, INCLUDE_TRACEBACK_FIELD, False)) and bool(record.exc_info)


def _safe_traceback_metadata(value: Any, max_length: int, *, keep_tail: bool = False) -> str:
    escaped = json.dumps(str(value), ensure_ascii=True)[1:-1]
    if len(escaped) <= max_length:
        return escaped
    if keep_tail:
        return f"...{escaped[-(max_length - 3):]}"
    return f"{escaped[: max_length - 3]}..."


class _BoundedTracebackOutput:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.full = False
        self.markers: set[str] = set()

    def append(self, text: str) -> None:
        if self.full:
            return
        if self.length + len(text) <= TRACEBACK_MAX_OUTPUT_LENGTH:
            self.parts.append(text)
            self.length += len(text)
            return
        marker = "\n[traceback truncated: output limit]\n"
        prefix_length = TRACEBACK_MAX_OUTPUT_LENGTH - len(marker)
        self.parts = [("".join(self.parts) + text)[:prefix_length], marker]
        self.length = TRACEBACK_MAX_OUTPUT_LENGTH
        self.full = True

    def mark(self, reason: str) -> None:
        if reason in self.markers:
            return
        self.markers.add(reason)
        self.append(f"[traceback truncated: {reason}]\n")

    def mark_cycle(self) -> None:
        if "cycle" in self.markers:
            return
        self.markers.add("cycle")
        self.append("[traceback cycle omitted]\n")

    def render(self) -> str:
        return "".join(self.parts)


def _format_traceback_frames(traceback_object: Any, max_frames: int) -> tuple[list[str], int, bool]:
    rendered: list[str] = []
    current = traceback_object
    while current is not None and len(rendered) < max_frames:
        frame = current.tb_frame
        filename = _safe_traceback_metadata(
            Path(frame.f_code.co_filename).name or "<unknown>",
            TRACEBACK_FILENAME_MAX_LENGTH,
            keep_tail=True,
        )
        function_name = _safe_traceback_metadata(frame.f_code.co_name, TRACEBACK_FUNCTION_MAX_LENGTH)
        line_number = _safe_traceback_metadata(current.tb_lineno, TRACEBACK_LINE_NUMBER_MAX_LENGTH)
        rendered.append(
            f'  File "{filename}", line {line_number}, in {function_name}\n'
        )
        current = current.tb_next
    return rendered, len(rendered), current is not None


def _format_traceback_without_exception_value(exc_info: Any) -> str:
    exception_type, exception_value, traceback_object = exc_info
    output = _BoundedTracebackOutput()
    frames_rendered = 0

    if not isinstance(exception_value, BaseException):
        if traceback_object is not None:
            output.append("Traceback (most recent call last):\n")
            frames, frame_count, truncated = _format_traceback_frames(
                traceback_object,
                min(TRACEBACK_MAX_FRAMES_PER_EXCEPTION, TRACEBACK_MAX_TOTAL_FRAMES),
            )
            frames_rendered += frame_count
            for frame_text in frames:
                output.append(frame_text)
            if truncated:
                output.mark("frame limit")
        class_name = exception_type.__name__ if exception_type is not None else "Exception"
        output.append(_safe_traceback_metadata(class_name, TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH))
        return output.render()

    seen: set[int] = set()
    node_count = 0
    stack: list[tuple[str, Any, Any, int]] = [
        ("exception", exception_value, traceback_object, 0)
    ]
    while stack and not output.full:
        task, value, current_traceback, depth = stack.pop()
        if task == "text":
            output.append(value)
            continue
        if task == "body":
            if current_traceback is not None:
                output.append("Traceback (most recent call last):\n")
                available_frames = min(
                    TRACEBACK_MAX_FRAMES_PER_EXCEPTION,
                    TRACEBACK_MAX_TOTAL_FRAMES - frames_rendered,
                )
                frames, frame_count, truncated = _format_traceback_frames(
                    current_traceback,
                    available_frames,
                )
                frames_rendered += frame_count
                for frame_text in frames:
                    output.append(frame_text)
                if truncated:
                    output.mark("frame limit")
            output.append(
                _safe_traceback_metadata(
                    type(value).__name__,
                    TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH,
                )
            )
            if isinstance(value, BaseExceptionGroup):
                remaining_nodes = TRACEBACK_MAX_EXCEPTION_NODES - node_count
                members = value.exceptions[:remaining_nodes]
                if len(value.exceptions) > len(members):
                    output.append("\n")
                    output.mark("exception node limit")
                for member in reversed(members):
                    stack.append(("exception", member, member.__traceback__, depth + 1))
                    stack.append(("text", "\n", None, depth))
            continue

        if depth > TRACEBACK_MAX_EXCEPTION_DEPTH:
            output.mark("exception depth limit")
            continue
        if id(value) in seen:
            output.mark_cycle()
            continue
        if node_count >= TRACEBACK_MAX_EXCEPTION_NODES:
            output.mark("exception node limit")
            continue
        seen.add(id(value))
        node_count += 1
        stack.append(("body", value, current_traceback, depth))
        if value.__cause__ is not None:
            stack.append(
                (
                    "text",
                    "\nThe above exception was the direct cause of the following exception:\n\n",
                    None,
                    depth,
                )
            )
            stack.append(("exception", value.__cause__, value.__cause__.__traceback__, depth + 1))
        elif value.__context__ is not None and not value.__suppress_context__:
            stack.append(
                (
                    "text",
                    "\nDuring handling of the above exception, another exception occurred:\n\n",
                    None,
                    depth,
                )
            )
            stack.append(("exception", value.__context__, value.__context__.__traceback__, depth + 1))
    return output.render()


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service_name: str | None = None) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.service_name:
            payload["service"] = self.service_name
        for field_name in OBSERVABILITY_FIELDS:
            value = getattr(record, field_name, None)
            if isinstance(value, str | int | float | bool):
                payload[field_name] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_class"] = _safe_traceback_metadata(
                record.exc_info[0].__name__,
                TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH,
            )
        if _traceback_requested(record):
            payload["traceback"] = _format_traceback_without_exception_value(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        formatted = super().formatTime(record, datefmt or "%Y-%m-%dT%H:%M:%S")
        return f"{formatted}.{int(record.msecs):03d}Z"


def _normalize_log_format(value: str | None) -> str:
    return "json" if (value or "").strip().lower() == "json" else "text"


class SafeTextFormatter(logging.Formatter):
    def __init__(self, *, service_name: str | None = None) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        # Another handler may have cached an unsafe default rendering on the
        # shared record. Rebuild it through this formatter's redacted path.
        if record.exc_info:
            record.exc_text = None
        rendered = super().format(record)
        fields: dict[str, str | int | float | bool] = {}
        if self.service_name:
            fields["service"] = self.service_name
        for field_name in OBSERVABILITY_FIELDS:
            value = getattr(record, field_name, None)
            if isinstance(value, str | int | float | bool):
                fields[field_name] = value
        if record.exc_info and record.exc_info[0] is not None:
            fields["exception_class"] = _safe_traceback_metadata(
                record.exc_info[0].__name__,
                TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH,
            )
        if fields:
            suffix = " ".join(f"{key}={value}" for key, value in fields.items())
            rendered = f"{rendered} {suffix}"
        if _traceback_requested(record):
            traceback_text = _format_traceback_without_exception_value(record.exc_info)
            rendered = "\n".join((rendered, traceback_text))
        return rendered

    def formatException(self, ei) -> str:
        exception_type = ei[0]
        class_name = exception_type.__name__ if exception_type is not None else "Exception"
        return _safe_traceback_metadata(class_name, TRACEBACK_EXCEPTION_CLASS_MAX_LENGTH)

    def formatStack(self, stack_info: str) -> str:
        return ""


def _text_formatter(*, service_name: str | None = None) -> logging.Formatter:
    return SafeTextFormatter(service_name=service_name)


def _stream_formatter(*, log_format: str, service_name: str | None) -> logging.Formatter:
    if log_format == "json":
        return JsonFormatter(service_name=service_name)
    return _text_formatter(service_name=service_name)


def configure_service_logging(
    *,
    log_level: str,
    log_format: str = "text",
    service_name: str | None = None,
    log_file: str | None = None,
) -> None:
    root = logging.getLogger()
    normalized_format = _normalize_log_format(log_format)
    if getattr(configure_service_logging, "_configured", False):
        root.setLevel(log_level.upper())
        return

    root.setLevel(log_level.upper())

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(_stream_formatter(log_format=normalized_format, service_name=service_name))
    root.addHandler(stream_handler)

    if log_file:
        resolved_log_file = Path(log_file)
        try:
            resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                resolved_log_file,
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "File logging disabled for %s because the log file could not be opened: %s",
                resolved_log_file,
                exc,
            )
        else:
            # Keep the on-disk log human-readable even when stdout/syslog is JSON.
            file_handler.setFormatter(_text_formatter(service_name=service_name))
            root.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.disabled = False
        logger.setLevel(log_level.upper())

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    configure_service_logging._configured = True


def configure_logging(settings: Settings) -> None:
    configure_service_logging(
        log_level=settings.app.log_level,
        log_format=_normalize_log_format(os.getenv("LOG_FORMAT")),
        service_name="enclosure-ui",
        log_file=settings.paths.log_file,
    )
