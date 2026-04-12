from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    refresh_interval_seconds: int = 30
    cache_ttl_seconds: int = 10
    log_level: str = "INFO"
    debug: bool = False
    verify_ssl: bool = True


class TrueNASConfig(BaseModel):
    host: str = "https://truenas.local"
    api_key: str = ""
    verify_ssl: bool = True
    timeout_seconds: int = 15
    enclosure_filter: str | None = None


class SSHConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 22
    user: str = ""
    key_path: str = "/run/ssh/id_truenas"
    sudo_password: str = ""
    known_hosts_path: str | None = None
    strict_host_key_checking: bool = False
    timeout_seconds: int = 15
    commands: list[str] = Field(
        default_factory=lambda: [
            "glabel status",
            "zpool status -gP",
            "gmultipath list",
            "camcontrol devlist -v",
            "sesutil map",
            "sesutil show",
        ]
    )


class LayoutConfig(BaseModel):
    slot_count: int = 60
    rows: int = 4
    columns: int = 15
    slot_number_base: int = 0
    api_slot_number_base: int = 1


class PathConfig(BaseModel):
    mapping_file: str = "/app/data/slot_mappings.json"
    log_file: str = "/app/logs/app.log"


class SystemConfig(BaseModel):
    id: str = "default"
    label: str | None = None
    truenas: TrueNASConfig = Field(default_factory=TrueNASConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    truenas: TrueNASConfig = Field(default_factory=TrueNASConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    systems: list[SystemConfig] = Field(default_factory=list)
    default_system_id: str | None = None
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    config_file: str = "/app/config/config.yaml"


ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "APP_HOST": ("app", "host"),
    "APP_PORT": ("app", "port"),
    "APP_REFRESH_INTERVAL": ("app", "refresh_interval_seconds"),
    "APP_CACHE_TTL": ("app", "cache_ttl_seconds"),
    "APP_LOG_LEVEL": ("app", "log_level"),
    "APP_DEBUG": ("app", "debug"),
    "APP_VERIFY_SSL": ("app", "verify_ssl"),
    "APP_CONFIG_PATH": ("config_file",),
    "TRUENAS_HOST": ("truenas", "host"),
    "TRUENAS_API_KEY": ("truenas", "api_key"),
    "TRUENAS_VERIFY_SSL": ("truenas", "verify_ssl"),
    "TRUENAS_TIMEOUT": ("truenas", "timeout_seconds"),
    "TRUENAS_ENCLOSURE_FILTER": ("truenas", "enclosure_filter"),
    "SSH_ENABLED": ("ssh", "enabled"),
    "SSH_HOST": ("ssh", "host"),
    "SSH_PORT": ("ssh", "port"),
    "SSH_USER": ("ssh", "user"),
    "SSH_KEY_PATH": ("ssh", "key_path"),
    "SSH_SUDO_PASSWORD": ("ssh", "sudo_password"),
    "SSH_KNOWN_HOSTS_PATH": ("ssh", "known_hosts_path"),
    "SSH_STRICT_HOST_KEY_CHECKING": ("ssh", "strict_host_key_checking"),
    "SSH_TIMEOUT": ("ssh", "timeout_seconds"),
    "SSH_COMMANDS_JSON": ("ssh", "commands"),
    "SYSTEM_DEFAULT_ID": ("default_system_id",),
    "LAYOUT_SLOT_COUNT": ("layout", "slot_count"),
    "LAYOUT_ROWS": ("layout", "rows"),
    "LAYOUT_COLUMNS": ("layout", "columns"),
    "LAYOUT_SLOT_NUMBER_BASE": ("layout", "slot_number_base"),
    "LAYOUT_API_SLOT_NUMBER_BASE": ("layout", "api_slot_number_base"),
    "PATH_MAPPING_FILE": ("paths", "mapping_file"),
    "PATH_LOG_FILE": ("paths", "log_file"),
}


def _parse_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null"}:
        return None

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _set_path_value(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file {config_path} must contain a YAML mapping.")
        return loaded


def _normalize_system_id(value: str | None, fallback_index: int) -> str:
    text = normalize_text(value)
    if not text:
        return f"system-{fallback_index}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip()).strip("-_").lower()
    return normalized or f"system-{fallback_index}"


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_systems(settings: Settings) -> Settings:
    systems = list(settings.systems)
    if not systems:
        systems = [
            SystemConfig(
                id="default",
                label="Primary",
                truenas=settings.truenas,
                ssh=settings.ssh,
            )
        ]

    normalized_systems: list[SystemConfig] = []
    seen_ids: set[str] = set()
    for index, system in enumerate(systems, start=1):
        system_id = _normalize_system_id(system.id, index)
        if system_id in seen_ids:
            suffix = 2
            while f"{system_id}-{suffix}" in seen_ids:
                suffix += 1
            system_id = f"{system_id}-{suffix}"
        seen_ids.add(system_id)
        normalized_systems.append(
            system.model_copy(
                update={
                    "id": system_id,
                    "label": normalize_text(system.label) or system_id.replace("-", " ").title(),
                }
            )
        )

    default_system_id = normalize_text(settings.default_system_id)
    if not default_system_id or default_system_id not in seen_ids:
        default_system_id = normalized_systems[0].id

    default_system = next(
        (system for system in normalized_systems if system.id == default_system_id),
        normalized_systems[0],
    )
    return settings.model_copy(
        update={
            "systems": normalized_systems,
            "default_system_id": default_system.id,
            # Keep top-level configs aligned with the active default system so older
            # code paths and templates continue to behave sensibly.
            "truenas": default_system.truenas,
            "ssh": default_system.ssh,
        }
    )


@lru_cache
def get_settings() -> Settings:
    defaults = Settings().model_dump()
    config_path = Path(os.getenv("APP_CONFIG_PATH", defaults["config_file"]))
    yaml_config = _load_yaml_config(config_path)
    merged = _deep_merge(defaults, yaml_config)

    for env_name, target_path in ENV_OVERRIDES.items():
        raw_value = os.getenv(env_name)
        if raw_value is None:
            continue
        _set_path_value(merged, target_path, _parse_scalar(raw_value))

    settings = _normalize_systems(Settings.model_validate(merged))
    Path(settings.paths.mapping_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.paths.log_file).parent.mkdir(parents=True, exist_ok=True)
    return settings
