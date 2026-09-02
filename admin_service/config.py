from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, model_validator

from app.secret_files import load_secret_environment_value


class AdminSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    app_name: str = "TrueNAS JBOD Admin Service"
    host: str = "0.0.0.0"
    port: int = 8002
    docker_socket_path: str = "/var/run/docker.sock"
    auto_stop_seconds: StrictInt = Field(default=0, ge=0)
    container_ui_name: str = "truenas-jbod-ui"
    container_history_name: str = "truenas-jbod-history"
    container_admin_name: str = "truenas-jbod-admin"
    container_control_timeout_seconds: int = 30
    container_ui_livez_url: str = "http://enclosure-ui:8000/livez"
    container_history_livez_url: str = "http://enclosure-history:8001/livez"
    container_admin_livez_url: str = "http://127.0.0.1:8002/livez"
    container_version_probe_timeout_seconds: float = 1.5
    public_origin: str | None = None
    auth_mode: Literal["network", "basic"] = "network"
    auth_username: str | None = None
    auth_password: SecretStr | None = None
    allow_plaintext_backup_export: bool = False
    clean_backup_targets: list[Literal["ui", "history"]] = Field(
        default_factory=lambda: ["ui", "history"]
    )
    host_prep_temp_dir: str = "/tmp/truenas-jbod-ui-host-prep"

    @model_validator(mode="after")
    def validate_authentication(self) -> "AdminSettings":
        if self.auth_mode != "basic":
            return self
        username = str(self.auth_username or "").strip()
        password = self.auth_password.get_secret_value() if self.auth_password else ""
        if not username or not password:
            raise ValueError(
                "ADMIN_AUTH_MODE=basic requires non-empty ADMIN_AUTH_USERNAME and ADMIN_AUTH_PASSWORD."
            )
        self.auth_username = username
        return self


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
    "ADMIN_AUTH_MODE": "auth_mode",
    "ADMIN_AUTH_USERNAME": "auth_username",
    "ADMIN_AUTH_PASSWORD": "auth_password",
    "ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT": "allow_plaintext_backup_export",
    "ADMIN_CLEAN_BACKUP_TARGETS_JSON": "clean_backup_targets",
    "ADMIN_HOST_PREP_TEMP_DIR": "host_prep_temp_dir",
}
FILE_SECRET_ENV_OVERRIDES = frozenset({"ADMIN_AUTH_PASSWORD"})


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
        raw_value = (
            load_secret_environment_value(env_name)
            if env_name in FILE_SECRET_ENV_OVERRIDES
            else os.getenv(env_name)
        )
        if raw_value is None:
            continue
        payload[field_name] = (
            raw_value
            if field_name in {"auth_username", "auth_password"}
            else _parse_scalar(raw_value)
        )

    settings = AdminSettings.model_validate(payload)
    Path("/tmp").mkdir(parents=True, exist_ok=True)
    Path(settings.host_prep_temp_dir).mkdir(parents=True, exist_ok=True)
    return settings
