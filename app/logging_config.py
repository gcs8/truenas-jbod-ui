from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import Settings


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
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        formatted = super().formatTime(record, datefmt or "%Y-%m-%dT%H:%M:%S")
        return f"{formatted}.{int(record.msecs):03d}Z"


def _normalize_log_format(value: str | None) -> str:
    return "json" if (value or "").strip().lower() == "json" else "text"


def _text_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _stream_formatter(*, log_format: str, service_name: str | None) -> logging.Formatter:
    if log_format == "json":
        return JsonFormatter(service_name=service_name)
    return _text_formatter()


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
            file_handler.setFormatter(_text_formatter())
            root.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(log_level.upper())

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
