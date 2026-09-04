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


def _traceback_requested(record: logging.LogRecord) -> bool:
    return bool(getattr(record, INCLUDE_TRACEBACK_FIELD, False)) and bool(record.exc_info)


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
            payload["exception_class"] = record.exc_info[0].__name__
        if _traceback_requested(record):
            payload["traceback"] = logging.Formatter.formatException(self, record.exc_info)
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
        rendered = super().format(record)
        fields: dict[str, str | int | float | bool] = {}
        if self.service_name:
            fields["service"] = self.service_name
        for field_name in OBSERVABILITY_FIELDS:
            value = getattr(record, field_name, None)
            if isinstance(value, str | int | float | bool):
                fields[field_name] = value
        if record.exc_info and record.exc_info[0] is not None:
            fields["exception_class"] = record.exc_info[0].__name__
        if fields:
            suffix = " ".join(f"{key}={value}" for key, value in fields.items())
            rendered = f"{rendered} {suffix}"
        if _traceback_requested(record):
            traceback_text = logging.Formatter.formatException(self, record.exc_info)
            rendered = "\n".join((rendered, traceback_text))
        return rendered

    def formatException(self, ei) -> str:
        exception_type = ei[0]
        return exception_type.__name__ if exception_type is not None else "Exception"

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
