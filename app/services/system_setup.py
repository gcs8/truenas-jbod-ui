from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from app.config import (
    BMCConfig,
    HANodeConfig,
    SSHConfig,
    StorageViewBindingConfig,
    StorageViewConfig,
    StorageViewLayoutOverridesConfig,
    StorageViewRenderConfig,
    SystemConfig,
    TrueNASConfig,
    _normalize_system_id,
    normalize_text,
)
from app.models.domain import SystemSetupRequest


_CONFIG_WRITE_LOCK = threading.Lock()


def default_ssh_commands_for_platform(platform: str) -> list[str]:
    normalized = normalize_text(platform) or "core"
    if normalized == "scale":
        return [
            "/usr/sbin/zpool status -gP",
            "/usr/bin/lsblk -o NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,HCTL",
            "/usr/bin/lsscsi -g",
        ]
    if normalized == "linux":
        return [
            "/usr/bin/lsblk -OJ",
            "sudo -n /usr/sbin/mdadm --detail --scan",
            "/usr/sbin/nvme list-subsys -o json",
        ]
    if normalized == "quantastor":
        return []
    if normalized == "esxi":
        return [
            "vmware -v",
            "esxcli system version get",
            "esxcli software vib list",
            "esxcli storage core adapter list",
            "esxcli storage core device list",
            "esxcli storage core path list",
            "esxcli storage filesystem list",
            "esxcli storage vmfs extent list",
            "esxcli storage san sas list",
            "/opt/lsi/storcli64/storcli64 /c0 show all J",
            "/opt/lsi/storcli64/storcli64 /c0/vall show all J",
            "/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J",
        ]
    if normalized == "ipmi":
        return []
    return list(SSHConfig().commands)


class SystemSetupService:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def create_system(self, payload: SystemSetupRequest) -> SystemConfig:
        system, _ = self.save_system(payload)
        return system

    def delete_system(self, system_id: str) -> tuple[str, str | None]:
        normalized_system_id = _normalize_system_id(system_id, 1)
        with _CONFIG_WRITE_LOCK:
            config = self._load_config()
            raw_systems = list(config.get("systems") or [])
            existing_index = next(
                (
                    index
                    for index, item in enumerate(raw_systems)
                    if isinstance(item, dict)
                    and _normalize_system_id(item.get("id"), index + 1) == normalized_system_id
                ),
                None,
            )
            if existing_index is None:
                raise ValueError(f"System id '{normalized_system_id}' does not exist in the current config.")

            removed_system = SystemConfig.model_validate(raw_systems.pop(existing_index))
            config["systems"] = raw_systems

            current_default = normalize_text(str(config.get("default_system_id") or ""))
            next_default_id: str | None = current_default or None
            if current_default == normalized_system_id:
                if raw_systems:
                    fallback_ids = [
                        _normalize_system_id(item.get("id"), index + 1)
                        for index, item in enumerate(raw_systems)
                        if isinstance(item, dict)
                    ]
                    next_default_id = fallback_ids[0] if fallback_ids else None
                    config["default_system_id"] = next_default_id
                else:
                    config.pop("default_system_id", None)
                    next_default_id = None

            self._write_config(config)
            return removed_system.label or removed_system.id, next_default_id

    def save_system(self, payload: SystemSetupRequest) -> tuple[SystemConfig, bool]:
        with _CONFIG_WRITE_LOCK:
            config = self._load_config()
            raw_systems = list(config.get("systems") or [])
            next_index = len(raw_systems) + 1
            system_id = _normalize_system_id(payload.system_id or payload.label, next_index)

            existing_index = next(
                (
                    index
                    for index, item in enumerate(raw_systems)
                    if isinstance(item, dict)
                    and _normalize_system_id(item.get("id"), index + 1) == system_id
                ),
                None,
            )
            if existing_index is not None and not payload.replace_existing:
                raise ValueError(f"System id '{system_id}' already exists in the current config.")

            existing_system = None
            if existing_index is not None:
                existing_system = SystemConfig.model_validate(raw_systems[existing_index])

            ssh_enabled = bool(payload.ssh_enabled)
            existing_ssh_commands = list(existing_system.ssh.commands) if existing_system else []
            ssh_commands = (
                payload.ssh_commands
                or existing_ssh_commands
                or default_ssh_commands_for_platform(payload.platform)
            )
            ssh_host = payload.ssh_host or payload.truenas_host
            if payload.storage_views is None and existing_system is not None:
                storage_views = list(existing_system.storage_views)
            else:
                storage_views = [
                    StorageViewConfig(
                        id=storage_view.id or storage_view.label,
                        label=storage_view.label,
                        kind=storage_view.kind,
                        template_id=storage_view.template_id,
                        profile_id=storage_view.profile_id,
                        enabled=storage_view.enabled,
                        order=storage_view.order,
                        render=StorageViewRenderConfig(
                            show_in_main_ui=storage_view.render.show_in_main_ui,
                            show_in_admin_ui=storage_view.render.show_in_admin_ui,
                            default_collapsed=storage_view.render.default_collapsed,
                        ),
                        binding=StorageViewBindingConfig(
                            mode=storage_view.binding.mode,
                            target_system_id=storage_view.binding.target_system_id,
                            enclosure_ids=list(storage_view.binding.enclosure_ids),
                            pool_names=list(storage_view.binding.pool_names),
                            serials=list(storage_view.binding.serials),
                            pcie_addresses=list(storage_view.binding.pcie_addresses),
                            device_names=list(storage_view.binding.device_names),
                        ),
                        layout_overrides=(
                            StorageViewLayoutOverridesConfig(
                                slot_labels=dict(storage_view.layout_overrides.slot_labels),
                                slot_sizes=dict(storage_view.layout_overrides.slot_sizes),
                            )
                            if storage_view.layout_overrides is not None
                            else None
                        ),
                    )
                    for storage_view in (payload.storage_views or [])
                ]
            system = SystemConfig(
                id=system_id,
                label=payload.label,
                default_profile_id=payload.default_profile_id,
                enclosure_profiles=dict(existing_system.enclosure_profiles) if existing_system else {},
                storage_views=storage_views,
                truenas=TrueNASConfig(
                    host=payload.truenas_host,
                    api_key=payload.api_key or "",
                    api_user=payload.api_user or "",
                    api_password=payload.api_password or "",
                    platform=payload.platform,
                    verify_ssl=payload.verify_ssl,
                    tls_ca_bundle_path=(
                        payload.tls_ca_bundle_path
                        if payload.tls_ca_bundle_path is not None
                        else (existing_system.truenas.tls_ca_bundle_path if existing_system is not None else None)
                    ),
                    tls_server_name=(
                        payload.tls_server_name
                        if payload.tls_server_name is not None
                        else (existing_system.truenas.tls_server_name if existing_system is not None else None)
                    ),
                    timeout_seconds=(
                        existing_system.truenas.timeout_seconds
                        if existing_system is not None
                        else payload.timeout_seconds
                    ),
                    enclosure_filter=payload.enclosure_filter,
                ),
                ssh=SSHConfig(
                    enabled=ssh_enabled,
                    host=ssh_host or "",
                    extra_hosts=(
                        list(existing_system.ssh.extra_hosts)
                        if existing_system is not None
                        else list(payload.ssh_extra_hosts)
                    ),
                    ha_enabled=bool(payload.ha_enabled),
                    ha_nodes=[
                        HANodeConfig(
                            system_id=node.system_id,
                            label=node.label,
                            host=node.host or "",
                        )
                        for node in payload.ha_nodes
                    ],
                    port=payload.ssh_port,
                    user=payload.ssh_user or "",
                    key_path=payload.ssh_key_path or "",
                    password=payload.ssh_password or "",
                    sudo_password=payload.ssh_sudo_password or "",
                    known_hosts_path=payload.ssh_known_hosts_path,
                    strict_host_key_checking=payload.ssh_strict_host_key_checking,
                    timeout_seconds=(
                        existing_system.ssh.timeout_seconds
                        if existing_system is not None
                        else payload.ssh_timeout_seconds
                    ),
                    commands=list(ssh_commands),
                ),
                bmc=BMCConfig(
                    enabled=bool(payload.bmc_enabled),
                    host=payload.bmc_host or "",
                    username=payload.bmc_username or "",
                    password=payload.bmc_password or "",
                    verify_ssl=payload.bmc_verify_ssl,
                    timeout_seconds=payload.bmc_timeout_seconds,
                ),
            )

            if existing_index is None:
                raw_systems.append(system.model_dump(mode="python", exclude_none=True))
            else:
                raw_systems[existing_index] = system.model_dump(mode="python", exclude_none=True)
            config["systems"] = raw_systems
            if payload.make_default or not normalize_text(str(config.get("default_system_id") or "")):
                config["default_system_id"] = system_id

            self._write_config(config)
            return system, existing_index is not None

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}

        with self.config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file {self.config_path} must contain a YAML mapping.")
        return loaded

    def _write_config(self, payload: dict[str, Any]) -> None:
        temp_path = self.config_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(
                payload,
                handle,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=False,
            )
        temp_path.replace(self.config_path)
