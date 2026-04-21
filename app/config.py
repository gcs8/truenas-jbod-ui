from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    refresh_interval_seconds: int = 30
    cache_ttl_seconds: int = 10
    startup_warm_cache_enabled: bool = False
    startup_warm_smart_enabled: bool = False
    smart_batch_max_concurrency: int = 12
    smart_prefetch_delay_ms: int = 120
    smart_prefetch_strategy: Literal["auto", "single", "chunked"] = "auto"
    smart_prefetch_single_threshold: int = 128
    smart_prefetch_chunk_size: int = 24
    smart_prefetch_batch_concurrency: int = 2
    export_history_concurrency: int = 12
    export_cache_ttl_seconds: int = 60
    export_cache_max_entries: int = 8
    log_level: str = "INFO"
    debug: bool = False
    verify_ssl: bool = True


class PerfConfig(BaseModel):
    enabled: bool = False
    log_all_requests: bool = False
    slow_request_ms: int = 1000
    slow_stage_ms: int = 250


class TrueNASConfig(BaseModel):
    host: str = "https://truenas.local"
    api_key: str = ""
    api_user: str = ""
    api_password: str = ""
    platform: Literal["core", "scale", "linux", "quantastor"] = "core"
    verify_ssl: bool = True
    tls_ca_bundle_path: str | None = None
    tls_server_name: str | None = None
    timeout_seconds: int = 15
    enclosure_filter: str | None = None


class HANodeConfig(BaseModel):
    system_id: str | None = None
    label: str | None = None
    host: str | None = None

    @field_validator("system_id", "label", "host", mode="before")
    @classmethod
    def _normalize_text_fields(cls, value: Any) -> str | None:
        normalized = normalize_text(str(value) if value is not None else None)
        return normalized or None


class SSHConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    extra_hosts: list[str] = Field(default_factory=list)
    ha_enabled: bool = False
    ha_nodes: list[HANodeConfig] = Field(default_factory=list)
    port: int = 22
    user: str = ""
    key_path: str = "/run/ssh/id_truenas"
    password: str = ""
    sudo_password: str = ""
    known_hosts_path: str | None = "/app/data/known_hosts"
    strict_host_key_checking: bool = True
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

    @field_validator("ha_nodes", mode="after")
    @classmethod
    def _normalize_ha_nodes(cls, value: list[HANodeConfig]) -> list[HANodeConfig]:
        normalized: list[HANodeConfig] = []
        seen: set[tuple[str | None, str | None]] = set()
        for node in value[:3]:
            if not (node.system_id or node.label or node.host):
                continue
            dedupe_key = (node.system_id, node.host)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            normalized.append(node)
        return normalized


class EnclosureProfileConfig(BaseModel):
    id: str
    label: str
    eyebrow: str | None = None
    summary: str | None = None
    panel_title: str | None = None
    edge_label: str | None = None
    face_style: str = "generic"
    latch_edge: str = "bottom"
    bay_size: str | None = None
    rows: int
    columns: int
    slot_layout: list[list[int | None]] | None = None
    row_groups: list[int] = Field(default_factory=list)
    slot_hints: dict[int, list[str]] = Field(default_factory=dict)

    @field_validator("bay_size", mode="before")
    @classmethod
    def _normalize_bay_size(cls, value: Any) -> Any:
        if value in {None, ""}:
            return None
        if isinstance(value, (int, float)):
            normalized = f"{float(value):.1f}"
        else:
            normalized = str(value).strip()
        if normalized not in {"3.5", "2.5"}:
            raise ValueError("bay_size must be 3.5 or 2.5")
        return normalized


StorageViewKind = Literal["ses_enclosure", "nvme_carrier", "boot_devices", "manual"]
StorageViewBindingMode = Literal["auto", "pool", "serial", "hybrid"]


class StorageViewRenderConfig(BaseModel):
    show_in_main_ui: bool = True
    show_in_admin_ui: bool = True
    default_collapsed: bool = False


class StorageViewBindingConfig(BaseModel):
    mode: StorageViewBindingMode = "auto"
    target_system_id: str | None = None
    enclosure_ids: list[str] = Field(default_factory=list)
    pool_names: list[str] = Field(default_factory=list)
    serials: list[str] = Field(default_factory=list)
    pcie_addresses: list[str] = Field(default_factory=list)
    device_names: list[str] = Field(default_factory=list)


class StorageViewLayoutOverridesConfig(BaseModel):
    slot_labels: dict[int, str] = Field(default_factory=dict)
    slot_sizes: dict[int, str] = Field(default_factory=dict)

    @field_validator("slot_labels", mode="before")
    @classmethod
    def _normalize_slot_labels(cls, value: Any) -> dict[int, str]:
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("slot_labels must be a mapping of slot numbers to labels")

        normalized: dict[int, str] = {}
        for raw_key, raw_label in value.items():
            try:
                slot_number = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise ValueError("slot label keys must be integers") from exc
            if slot_number < 0:
                continue
            label = normalize_text(str(raw_label) if raw_label is not None else None)
            if label:
                normalized[slot_number] = label[:128]
        return normalized

    @field_validator("slot_sizes", mode="before")
    @classmethod
    def _normalize_slot_sizes(cls, value: Any) -> dict[int, str]:
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("slot_sizes must be a mapping of slot numbers to M.2 lengths")

        allowed_sizes = {"2230", "2242", "2260", "2280", "22110"}
        normalized: dict[int, str] = {}
        for raw_key, raw_size in value.items():
            try:
                slot_number = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise ValueError("slot size keys must be integers") from exc
            if slot_number < 0:
                continue
            size_label = normalize_text(str(raw_size) if raw_size is not None else None)
            if size_label in allowed_sizes:
                normalized[slot_number] = size_label
        return normalized


class StorageViewConfig(BaseModel):
    id: str
    label: str
    kind: StorageViewKind
    template_id: str
    profile_id: str | None = None
    enabled: bool = True
    order: int = 10
    render: StorageViewRenderConfig = Field(default_factory=StorageViewRenderConfig)
    binding: StorageViewBindingConfig = Field(default_factory=StorageViewBindingConfig)
    layout_overrides: StorageViewLayoutOverridesConfig | None = None

    @field_validator("id", "label", "template_id", mode="before")
    @classmethod
    def _normalize_text_fields(cls, value: Any) -> str:
        normalized = normalize_text(str(value) if value is not None else None)
        return normalized or ""

    @field_validator("profile_id", mode="before")
    @classmethod
    def _normalize_optional_profile_id(cls, value: Any) -> str | None:
        return normalize_text(str(value) if value is not None else None)

    @field_validator("order", mode="before")
    @classmethod
    def _normalize_order(cls, value: Any) -> int:
        if value in {None, ""}:
            return 10
        try:
            return int(value)
        except (TypeError, ValueError):
            return 10

    @field_validator("binding", mode="after")
    @classmethod
    def _normalize_binding(cls, value: StorageViewBindingConfig) -> StorageViewBindingConfig:
        def _clean_list(values: list[str]) -> list[str]:
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in values or []:
                normalized = normalize_text(str(item) if item is not None else None)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    cleaned.append(normalized)
            return cleaned

        return value.model_copy(
            update={
                "target_system_id": normalize_text(value.target_system_id),
                "enclosure_ids": _clean_list(value.enclosure_ids),
                "pool_names": _clean_list(value.pool_names),
                "serials": _clean_list(value.serials),
                "pcie_addresses": _clean_list(value.pcie_addresses),
                "device_names": _clean_list(value.device_names),
            }
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
    profile_file: str = "/app/config/profiles.yaml"
    slot_detail_cache_file: str = "/app/data/slot_detail_cache.json"


class HistoryConfig(BaseModel):
    service_url: str = ""
    timeout_seconds: int = 10


class AdminSurfaceConfig(BaseModel):
    service_url: str = ""
    public_url: str | None = None
    port: int = 8082
    timeout_seconds: float = 0.75


class SystemConfig(BaseModel):
    id: str = "default"
    label: str | None = None
    truenas: TrueNASConfig = Field(default_factory=TrueNASConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    default_profile_id: str | None = None
    enclosure_profiles: dict[str, str] = Field(default_factory=dict)
    storage_views: list[StorageViewConfig] = Field(default_factory=list)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    perf: PerfConfig = Field(default_factory=PerfConfig)
    truenas: TrueNASConfig = Field(default_factory=TrueNASConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    admin: AdminSurfaceConfig = Field(default_factory=AdminSurfaceConfig)
    systems: list[SystemConfig] = Field(default_factory=list)
    default_system_id: str | None = None
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    paths: PathConfig = Field(default_factory=PathConfig)
    profiles: list[EnclosureProfileConfig] = Field(default_factory=list)
    config_file: str = "/app/config/config.yaml"


ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "APP_HOST": ("app", "host"),
    "APP_PORT": ("app", "port"),
    "APP_REFRESH_INTERVAL": ("app", "refresh_interval_seconds"),
    "APP_CACHE_TTL": ("app", "cache_ttl_seconds"),
    "APP_STARTUP_WARM_CACHE_ENABLED": ("app", "startup_warm_cache_enabled"),
    "APP_STARTUP_WARM_SMART_ENABLED": ("app", "startup_warm_smart_enabled"),
    "APP_SMART_BATCH_MAX_CONCURRENCY": ("app", "smart_batch_max_concurrency"),
    "APP_SMART_PREFETCH_DELAY_MS": ("app", "smart_prefetch_delay_ms"),
    "APP_SMART_PREFETCH_STRATEGY": ("app", "smart_prefetch_strategy"),
    "APP_SMART_PREFETCH_SINGLE_THRESHOLD": ("app", "smart_prefetch_single_threshold"),
    "APP_SMART_PREFETCH_CHUNK_SIZE": ("app", "smart_prefetch_chunk_size"),
    "APP_SMART_PREFETCH_BATCH_CONCURRENCY": ("app", "smart_prefetch_batch_concurrency"),
    "APP_EXPORT_HISTORY_CONCURRENCY": ("app", "export_history_concurrency"),
    "APP_EXPORT_CACHE_TTL_SECONDS": ("app", "export_cache_ttl_seconds"),
    "APP_EXPORT_CACHE_MAX_ENTRIES": ("app", "export_cache_max_entries"),
    "APP_LOG_LEVEL": ("app", "log_level"),
    "APP_DEBUG": ("app", "debug"),
    "APP_VERIFY_SSL": ("app", "verify_ssl"),
    "APP_CONFIG_PATH": ("config_file",),
    "PERF_TIMING_ENABLED": ("perf", "enabled"),
    "PERF_LOG_ALL_REQUESTS": ("perf", "log_all_requests"),
    "PERF_SLOW_REQUEST_MS": ("perf", "slow_request_ms"),
    "PERF_SLOW_STAGE_MS": ("perf", "slow_stage_ms"),
    "TRUENAS_HOST": ("truenas", "host"),
    "TRUENAS_API_KEY": ("truenas", "api_key"),
    "TRUENAS_API_USER": ("truenas", "api_user"),
    "TRUENAS_API_PASSWORD": ("truenas", "api_password"),
    "TRUENAS_PLATFORM": ("truenas", "platform"),
    "TRUENAS_VERIFY_SSL": ("truenas", "verify_ssl"),
    "TRUENAS_TLS_CA_BUNDLE_PATH": ("truenas", "tls_ca_bundle_path"),
    "TRUENAS_TLS_SERVER_NAME": ("truenas", "tls_server_name"),
    "TRUENAS_TIMEOUT": ("truenas", "timeout_seconds"),
    "TRUENAS_ENCLOSURE_FILTER": ("truenas", "enclosure_filter"),
    "SSH_ENABLED": ("ssh", "enabled"),
    "SSH_HOST": ("ssh", "host"),
    "SSH_EXTRA_HOSTS_JSON": ("ssh", "extra_hosts"),
    "SSH_PORT": ("ssh", "port"),
    "SSH_USER": ("ssh", "user"),
    "SSH_KEY_PATH": ("ssh", "key_path"),
    "SSH_PASSWORD": ("ssh", "password"),
    "SSH_SUDO_PASSWORD": ("ssh", "sudo_password"),
    "SSH_KNOWN_HOSTS_PATH": ("ssh", "known_hosts_path"),
    "SSH_STRICT_HOST_KEY_CHECKING": ("ssh", "strict_host_key_checking"),
    "SSH_TIMEOUT": ("ssh", "timeout_seconds"),
    "SSH_COMMANDS_JSON": ("ssh", "commands"),
    "HISTORY_BACKEND_URL": ("history", "service_url"),
    "HISTORY_BACKEND_TIMEOUT": ("history", "timeout_seconds"),
    "ADMIN_SERVICE_URL": ("admin", "service_url"),
    "ADMIN_PUBLIC_URL": ("admin", "public_url"),
    "ADMIN_PORT": ("admin", "port"),
    "ADMIN_TIMEOUT_SECONDS": ("admin", "timeout_seconds"),
    "SYSTEM_DEFAULT_ID": ("default_system_id",),
    "LAYOUT_SLOT_COUNT": ("layout", "slot_count"),
    "LAYOUT_ROWS": ("layout", "rows"),
    "LAYOUT_COLUMNS": ("layout", "columns"),
    "LAYOUT_SLOT_NUMBER_BASE": ("layout", "slot_number_base"),
    "LAYOUT_API_SLOT_NUMBER_BASE": ("layout", "api_slot_number_base"),
    "PATH_MAPPING_FILE": ("paths", "mapping_file"),
    "PATH_LOG_FILE": ("paths", "log_file"),
    "PATH_PROFILE_FILE": ("paths", "profile_file"),
    "PATH_SLOT_DETAIL_CACHE_FILE": ("paths", "slot_detail_cache_file"),
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


def _load_profile_yaml(profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return {}

    with profile_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    if isinstance(loaded, list):
        return {"profiles": loaded}
    if isinstance(loaded, dict):
        profiles = loaded.get("profiles", loaded)
        if isinstance(profiles, list):
            return {"profiles": profiles}

    raise ValueError(
        f"Profile file {profile_path} must contain a YAML list of profiles or a mapping with a 'profiles' list."
    )


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


def _normalize_storage_view_id(value: str | None, fallback_index: int) -> str:
    text = normalize_text(value)
    if not text:
        return f"storage-view-{fallback_index}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip()).strip("-_").lower()
    return normalized or f"storage-view-{fallback_index}"


def _normalize_storage_views(storage_views: list[StorageViewConfig] | None) -> list[StorageViewConfig]:
    normalized_views: list[StorageViewConfig] = []
    seen_ids: set[str] = set()
    for index, storage_view in enumerate(storage_views or [], start=1):
        storage_view_id = _normalize_storage_view_id(storage_view.id, index)
        if storage_view_id in seen_ids:
            suffix = 2
            while f"{storage_view_id}-{suffix}" in seen_ids:
                suffix += 1
            storage_view_id = f"{storage_view_id}-{suffix}"
        seen_ids.add(storage_view_id)
        normalized_views.append(
            storage_view.model_copy(
                update={
                    "id": storage_view_id,
                    "label": normalize_text(storage_view.label) or storage_view_id.replace("-", " ").title(),
                    "template_id": normalize_text(storage_view.template_id) or "manual-4",
                    "profile_id": normalize_text(storage_view.profile_id),
                    "order": storage_view.order if isinstance(storage_view.order, int) else index * 10,
                }
            )
        )
    return sorted(normalized_views, key=lambda view: (view.order, view.label.lower(), view.id))


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
                    "ssh": system.ssh.model_copy(
                        update={
                            "extra_hosts": [
                                host
                                for host in (
                                    normalize_text(value)
                                    for value in (system.ssh.extra_hosts or [])
                                )
                                if host
                            ],
                        }
                    ),
                    "default_profile_id": normalize_text(system.default_profile_id),
                    "enclosure_profiles": {
                        str(key): str(value)
                        for key, value in (system.enclosure_profiles or {}).items()
                        if normalize_text(str(key)) and normalize_text(str(value))
                    },
                    "storage_views": _normalize_storage_views(system.storage_views),
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

    profile_path = Path(merged.get("paths", {}).get("profile_file", defaults["paths"]["profile_file"]))
    if profile_path.exists():
        profile_config = _load_profile_yaml(profile_path)
        merged["profiles"] = [*(merged.get("profiles") or []), *(profile_config.get("profiles") or [])]

    settings = _normalize_systems(Settings.model_validate(merged))
    Path(settings.paths.mapping_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.paths.log_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.paths.profile_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.paths.slot_detail_cache_file).parent.mkdir(parents=True, exist_ok=True)
    return settings
