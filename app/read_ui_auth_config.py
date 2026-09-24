from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from app.secret_files import load_secret_environment_value


class ReadUiAuthSettings(BaseModel):
    """Operator credentials shared by UI and admin, without admin runtime state."""

    model_config = ConfigDict(hide_input_in_errors=True)

    public_origin: str | None = None
    auth_mode: Literal["network", "basic"] = "network"
    auth_username: str | None = None
    auth_password: SecretStr | None = None

    @model_validator(mode="after")
    def validate_authentication(self) -> "ReadUiAuthSettings":
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


AUTH_ENV_OVERRIDES = {
    "ADMIN_PUBLIC_ORIGIN": "public_origin",
    "ADMIN_AUTH_MODE": "auth_mode",
    "ADMIN_AUTH_USERNAME": "auth_username",
    "ADMIN_AUTH_PASSWORD": "auth_password",
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



def load_read_ui_auth_settings() -> ReadUiAuthSettings:
    """Read only shared auth inputs; never initialize admin directories or services."""
    payload = {}
    for env_name, field_name in AUTH_ENV_OVERRIDES.items():
        raw_value = (
            load_secret_environment_value(env_name)
            if field_name == "auth_password"
            else os.getenv(env_name)
        )
        if raw_value is not None:
            payload[field_name] = (
                raw_value
                if field_name in {"auth_username", "auth_password"}
                else _parse_scalar(raw_value)
            )
    return ReadUiAuthSettings.model_validate(payload)
