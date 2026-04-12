from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(configure_logging, "_configured", False):
        root.setLevel(settings.app.log_level.upper())
        return

    root.setLevel(settings.app.log_level.upper())
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    log_file = Path(settings.paths.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    configure_logging._configured = True

