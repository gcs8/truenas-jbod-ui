from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator, model_validator

from app.config_errors import ConfigurationError, describe_validation_error
from app.http_auth import parse_allowed_hosts
from app.secret_files import load_secret_environment_value


class ReadUiAuthSettings(BaseModel):
    """Operator credentials shared by UI and admin, without admin runtime state."""

    model_config = ConfigDict(hide_input_in_errors=True)

    public_origin: str | None = None
    auth_mode: Literal["network", "basic"] = "network"
    auth_username: str | None = None
    auth_password: SecretStr | None = None
    # Host names that may make changes when no public origin is set (#779).
    allowed_hosts: str = ""

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_allowed_hosts(cls, value: str) -> str:
        return ",".join(sorted(parse_allowed_hosts(value)))

    @property
    def allowed_host_names(self) -> frozenset[str]:
        return frozenset(name for name in self.allowed_hosts.split(",") if name)

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
    "ADMIN_ALLOWED_HOSTS": "allowed_hosts",
}
TEXT_AUTH_FIELDS = frozenset({"auth_username", "auth_password", "allowed_hosts"})


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
                if field_name in TEXT_AUTH_FIELDS
                else _parse_scalar(raw_value)
            )
    field_to_env = {field_name: env_name for env_name, field_name in AUTH_ENV_OVERRIDES.items()}
    try:
        return ReadUiAuthSettings.model_validate(payload)
    except ValidationError as exc:
        raise ConfigurationError(
            describe_validation_error(
                exc,
                resolve_location=lambda location: (
                    (field_to_env[str(location[0])], ".env") if str(location[0]) in field_to_env else None
                ),
                default_source=".env",
            )
        ) from None
