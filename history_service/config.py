from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


def _history_runtime_root() -> Path:
    return Path(__file__).resolve().parents[1] / "history"


def _default_history_sqlite_path() -> str:
    return str(_history_runtime_root() / "history.db")


def _default_history_backup_dir() -> str:
    return str(_history_runtime_root() / "backups")


def _default_history_long_term_backup_dir() -> str:
    return str(Path(_default_history_backup_dir()) / "long-term")


class HistorySettings(BaseModel):
    app_name: str = "TrueNAS JBOD History Service"
    host: str = "0.0.0.0"
    port: int = 8001
    source_base_url: str = "http://enclosure-ui:8000"
    sqlite_path: str = Field(default_factory=_default_history_sqlite_path)
    backup_dir: str = Field(default_factory=_default_history_backup_dir)
    backup_retention_count: int = 28
    long_term_backup_dir: str | None = Field(default_factory=_default_history_long_term_backup_dir)
    weekly_backup_retention_count: int = 4
    monthly_backup_retention_count: int = 3
    poll_interval_seconds: int = 300
    fast_interval_seconds: int = 300
    slow_interval_seconds: int = 3600
    request_timeout_seconds: int = 20
    smart_batch_size: int = 24
    startup_grace_seconds: int = 20

    @model_validator(mode="after")
    def align_backup_paths(self) -> "HistorySettings":
        default_sqlite_path = _default_history_sqlite_path()
        default_backup_dir = _default_history_backup_dir()
        default_long_term_backup_dir = _default_history_long_term_backup_dir()

        if self.sqlite_path != default_sqlite_path and self.backup_dir == default_backup_dir:
            self.backup_dir = str(Path(self.sqlite_path).parent / "backups")

        if self.long_term_backup_dir and (
            (self.backup_dir != default_backup_dir and self.long_term_backup_dir == default_long_term_backup_dir)
            or (self.sqlite_path != default_sqlite_path and self.long_term_backup_dir == default_long_term_backup_dir)
        ):
            self.long_term_backup_dir = str(Path(self.backup_dir) / "long-term")

        return self


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
