from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class HistorySettings(BaseModel):
    app_name: str = "TrueNAS JBOD History Service"
    host: str = "0.0.0.0"
    port: int = 8001
    source_base_url: str = "http://enclosure-ui:8000"
    sqlite_path: str = "/app/history/history.db"
    backup_dir: str = "/app/history/backups"
    backup_retention_count: int = 28
    long_term_backup_dir: str | None = "/app/history/backups/long-term"
    weekly_backup_retention_count: int = 4
    monthly_backup_retention_count: int = 3
    poll_interval_seconds: int = 300
    fast_interval_seconds: int = 300
    slow_interval_seconds: int = 3600
    request_timeout_seconds: int = 20
    smart_batch_size: int = 24
    startup_grace_seconds: int = 20


ENV_OVERRIDES: dict[str, str] = {
    "HISTORY_HOST": "host",
    "HISTORY_PORT": "port",
    "HISTORY_SOURCE_BASE_URL": "source_base_url",
    "HISTORY_SQLITE_PATH": "sqlite_path",
    "HISTORY_BACKUP_DIR": "backup_dir",
    "HISTORY_BACKUP_RETENTION_COUNT": "backup_retention_count",
    "HISTORY_LONG_TERM_BACKUP_DIR": "long_term_backup_dir",
    "HISTORY_WEEKLY_BACKUP_RETENTION_COUNT": "weekly_backup_retention_count",
    "HISTORY_MONTHLY_BACKUP_RETENTION_COUNT": "monthly_backup_retention_count",
    "HISTORY_POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "HISTORY_FAST_INTERVAL_SECONDS": "fast_interval_seconds",
    "HISTORY_SLOW_INTERVAL_SECONDS": "slow_interval_seconds",
    "HISTORY_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "HISTORY_SMART_BATCH_SIZE": "smart_batch_size",
    "HISTORY_STARTUP_GRACE_SECONDS": "startup_grace_seconds",
}


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"

    try:
        return int(stripped)
    except ValueError:
        return stripped


@lru_cache
def get_history_settings() -> HistorySettings:
    payload = HistorySettings().model_dump()
    for env_name, field_name in ENV_OVERRIDES.items():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        payload[field_name] = _parse_scalar(raw_value)

    settings = HistorySettings.model_validate(payload)
    Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)
    if settings.long_term_backup_dir:
        Path(settings.long_term_backup_dir).mkdir(parents=True, exist_ok=True)
    return settings
