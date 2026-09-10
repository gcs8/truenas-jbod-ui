"""The four admin-owned settings the main UI needs to gate its write controls.

The main UI container receives the whole ``.env`` file, but only the sign-in
mode, the credentials, and the admin public origin matter to it. Loading the
full admin model here would make a typo in an admin-only variable, or an
unwritable admin staging folder, take the main page down as well.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, model_validator

from app.config_errors import ConfigurationError, describe_validation_error
from app.secret_files import load_secret_environment_value


class ReadUiAuthSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    auth_mode: Literal["network", "basic"] = "network"
    auth_username: str | None = None
    auth_password: SecretStr | None = None
    public_origin: str | None = None

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


ENV_OVERRIDES: dict[str, str] = {
    "ADMIN_AUTH_MODE": "auth_mode",
    "ADMIN_AUTH_USERNAME": "auth_username",
    "ADMIN_AUTH_PASSWORD": "auth_password",
    "ADMIN_PUBLIC_ORIGIN": "public_origin",
}
_EXACT_TEXT_FIELDS = frozenset({"auth_username", "auth_password"})


@lru_cache
def get_read_ui_auth_settings() -> ReadUiAuthSettings:
    payload = ReadUiAuthSettings().model_dump()
    for env_name, field_name in ENV_OVERRIDES.items():
        raw_value = (
            load_secret_environment_value(env_name)
            if env_name == "ADMIN_AUTH_PASSWORD"
            else os.getenv(env_name)
        )
        if raw_value is None:
            continue
        if field_name in _EXACT_TEXT_FIELDS:
            payload[field_name] = raw_value
        elif raw_value.strip():
            payload[field_name] = raw_value.strip()

    field_to_env = {field_name: env_name for env_name, field_name in ENV_OVERRIDES.items()}
    try:
        return ReadUiAuthSettings.model_validate(payload)
    except ValidationError as exc:
        problems = describe_validation_error(
            exc,
            resolve_location=lambda location: (
                (field_to_env[str(location[0])], ".env") if str(location[0]) in field_to_env else None
            ),
            default_source=".env",
        )
        raise ConfigurationError(problems) from None
