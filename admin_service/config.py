from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class AdminSettings(BaseModel):
    app_name: str = "TrueNAS JBOD Admin Service"
    host: str = "0.0.0.0"
    port: int = 8002
    docker_socket_path: str = "/var/run/docker.sock"
    auto_stop_seconds: int = 3600
    container_ui_name: str = "truenas-jbod-ui"
    container_history_name: str = "truenas-jbod-history"
    container_admin_name: str = "truenas-jbod-admin"
    container_control_timeout_seconds: int = 30
    container_ui_livez_url: str = "http://enclosure-ui:8000/livez"
    container_history_livez_url: str = "http://enclosure-history:8001/livez"
    container_admin_livez_url: str = "http://127.0.0.1:8002/livez"
    container_version_probe_timeout_seconds: float = 1.5
    public_origin: str | None = None
    clean_backup_targets: list[str] = Field(default_factory=lambda: ["ui", "history"])
    host_prep_temp_dir: str = "/tmp/truenas-jbod-ui-host-prep"


ENV_OVERRIDES: dict[str, str] = {
    "ADMIN_APP_NAME": "app_name",
    "ADMIN_HOST": "host",
    "ADMIN_PORT": "port",
    "ADMIN_DOCKER_SOCKET_PATH": "docker_socket_path",
    "ADMIN_AUTO_STOP_SECONDS": "auto_stop_seconds",
    "ADMIN_CONTAINER_UI_NAME": "container_ui_name",
    "ADMIN_CONTAINER_HISTORY_NAME": "container_history_name",
    "ADMIN_CONTAINER_ADMIN_NAME": "container_admin_name",
    "ADMIN_CONTAINER_CONTROL_TIMEOUT_SECONDS": "container_control_timeout_seconds",
    "ADMIN_CONTAINER_UI_LIVEZ_URL": "container_ui_livez_url",
    "ADMIN_CONTAINER_HISTORY_LIVEZ_URL": "container_history_livez_url",
    "ADMIN_CONTAINER_ADMIN_LIVEZ_URL": "container_admin_livez_url",
    "ADMIN_CONTAINER_VERSION_PROBE_TIMEOUT_SECONDS": "container_version_probe_timeout_seconds",
    "ADMIN_PUBLIC_ORIGIN": "public_origin",
    "ADMIN_CLEAN_BACKUP_TARGETS_JSON": "clean_backup_targets",
    "ADMIN_HOST_PREP_TEMP_DIR": "host_prep_temp_dir",
}


def _parse_scalar(value: str):
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


@lru_cache
def get_admin_settings() -> AdminSettings:
    payload = AdminSettings().model_dump()
    for env_name, field_name in ENV_OVERRIDES.items():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        payload[field_name] = _parse_scalar(raw_value)

    settings = AdminSettings.model_validate(payload)
    Path("/tmp").mkdir(parents=True, exist_ok=True)
    Path(settings.host_prep_temp_dir).mkdir(parents=True, exist_ok=True)
    return settings
