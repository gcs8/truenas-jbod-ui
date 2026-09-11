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
    _derive_runtime_layout_paths,
    _normalize_system_id,
    normalize_text,
)
from app.models.domain import SystemSetupRequest
from app.services.credential_authority import (
    api_credential_authority,
    bmc_credential_authority,
    same_credential_authorities,
    same_credential_authority,
    ssh_credential_authorities,
)
from app.services.sas_fabric import (
    CORE_DMIDECODE_SLOT_COMMAND,
    CORE_MPR_DMESG_EVENTS_COMMAND,
    CORE_MPR_SYSCTL_LOCATION_COMMAND,
    CORE_PCICONF_LV_COMMAND,
)


_CONFIG_WRITE_LOCK = threading.Lock()
PRESERVE_SECRET_SENTINEL = "__TRUENAS_JBOD_KEEP_EXISTING_VALUE__"
LINUX_NVME_LIST_SUBSYS_COMMAND = (
    "/usr/sbin/nvme list-subsys -o json 2>/dev/null || "
    "/usr/bin/nvme list-subsys -o json 2>/dev/null || true"
)


def resolve_preserved_secret(incoming: str | None, existing: str | None = None) -> str:
    if incoming == PRESERVE_SECRET_SENTINEL:
        if not existing:
            raise ValueError("The saved secret requested by this operation is unavailable.")
        return existing
    return incoming or ""


_PLATFORM_SETUP_REQUIREMENTS: dict[str, dict[str, object]] = {
    "core": {
        "summary": "Reads disks and pools through the TrueNAS API. Add SSH later for bay positions, bay lights, SMART details and SAS diagnostics.",
        "required": (
            "An API key from TrueNAS (Settings > API Keys).",
        ),
        "optional": (
            "An SSH login on the host for the extras above. This app can create one for you in step 3.",
        ),
        "guidance": "",
    },
    "scale": {
        "summary": "Reads disks and pools through the TrueNAS API. Add SSH for bay positions, bay lights and SMART details.",
        "required": (
            "An API key from TrueNAS (top-right user menu > API Keys).",
        ),
        "optional": (
            "An SSH login on the host for bay positions, bay lights and SMART details. This app can create one for you in step 3.",
            "nvme-cli on the host if you want NVMe details.",
        ),
        "guidance": "",
    },
    "linux": {
        "summary": "Reads disks over SSH. Bay positions come from a disk shelf (SES), a saved chassis layout, the management controller or a vendor tool, whichever this host has.",
        "required": (
            "An SSH login on the host. This app can create one for you in step 3.",
        ),
        "optional": (
            "A chassis layout or storage view so bays can be drawn.",
            "smartmontools for SMART details and sg3_utils for disk-shelf bay positions.",
        ),
        "guidance": "",
    },
    "quantastor": {
        "summary": "Reads storage systems and disks through the QuantaStor REST API. SSH to a node adds shared disk-shelf detail and SMART data.",
        "required": (
            "The QuantaStor web address plus an API user and password.",
        ),
        "optional": (
            "An SSH login on a node that can see the shared disk shelf, or on each HA node.",
        ),
        "guidance": "",
    },
    "esxi": {
        "summary": "VMware ESXi is host-managed and read-only here. The app reads inventory over SSH with ESXCLI and StorCLI; add BMC access for chassis and drive lights.",
        "required": (
            "An SSH login on the ESXi host (usually root).",
            "StorCLI or PercCLI installed on the host. Step 3 can install it for you.",
        ),
        "optional": (
            "Management controller (BMC) access for chassis and drive lights.",
        ),
        "guidance": "If StorCLI shows your controller as something other than /c0, change /c0 in the SSH commands (to /cN or /call) before saving.",
    },
    "ipmi": {
        "summary": "Uses only the server's management controller (BMC / IPMI). Pick a chassis layout so empty bays can be drawn.",
        "required": (
            "The management controller's address, user name and password.",
            "A chassis layout that matches the front of the server.",
        ),
        "optional": (
            "An SSH login on the host, added later, for SMART details and bay positions.",
        ),
        "guidance": "",
    },
}


def setup_requirements_for_platform(platform: str) -> dict[str, object]:
    normalized = normalize_text(platform) or "core"
    payload = _PLATFORM_SETUP_REQUIREMENTS.get(normalized, _PLATFORM_SETUP_REQUIREMENTS["core"])
    return {
        "summary": str(payload.get("summary") or ""),
        "required": list(payload.get("required") or ()),
        "optional": list(payload.get("optional") or ()),
        "guidance": str(payload.get("guidance") or ""),
    }


def default_ssh_commands_for_platform(platform: str) -> list[str]:
    normalized = normalize_text(platform) or "core"
    if normalized == "core":
        return [
            "/sbin/glabel status",
            "/usr/local/sbin/zpool status -gP",
            "gmultipath list",
            "sudo -n /sbin/camcontrol devlist -v",
            "sudo -n /usr/sbin/sesutil map",
            "sudo -n /usr/sbin/sesutil show",
            "sudo -n /usr/sbin/mprutil show adapters",
            "sudo -n /usr/sbin/mprutil show adapter",
            "sudo -n /usr/sbin/mprutil show devices",
            "sudo -n /usr/sbin/mprutil show enclosures",
            "sudo -n /usr/sbin/mprutil show expanders",
            "sudo -n /usr/sbin/mprutil show iocfacts",
            CORE_PCICONF_LV_COMMAND,
            CORE_MPR_SYSCTL_LOCATION_COMMAND,
            CORE_DMIDECODE_SLOT_COMMAND,
            CORE_MPR_DMESG_EVENTS_COMMAND,
        ]
    if normalized == "scale":
        return [
            "/usr/sbin/zpool status -gP",
            "/usr/bin/lsblk --json --bytes --output NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,HCTL,PKNAME,MOUNTPOINTS,FSTYPE,UUID,PARTUUID,LOG-SEC,PHY-SEC",
            "/usr/bin/lsscsi -g",
            "/usr/bin/lsscsi -g -t",
            LINUX_NVME_LIST_SUBSYS_COMMAND,
        ]
    if normalized == "linux":
        return [
            "/usr/bin/lsblk --json --bytes --output NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,HCTL,PKNAME,MOUNTPOINTS,FSTYPE,UUID,PARTUUID,LOG-SEC,PHY-SEC",
            "sudo -n /usr/sbin/mdadm --detail --scan",
            LINUX_NVME_LIST_SUBSYS_COMMAND,
            "/usr/bin/lsscsi -g -t",
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

            tls_ca_bundle_path = (
                payload.tls_ca_bundle_path
                if payload.tls_ca_bundle_path is not None
                else (existing_system.truenas.tls_ca_bundle_path if existing_system is not None else None)
            )
            tls_server_name = (
                payload.tls_server_name
                if payload.tls_server_name is not None
                else (existing_system.truenas.tls_server_name if existing_system is not None else None)
            )
            ssh_host = payload.ssh_host or payload.truenas_host
            ssh_extra_hosts = (
                list(existing_system.ssh.extra_hosts)
                if existing_system is not None
                else list(payload.ssh_extra_hosts)
            )
            ssh_ha_hosts = [node.host for node in payload.ha_nodes]

            preserving_api_secret = any(
                incoming == PRESERVE_SECRET_SENTINEL
                for incoming in (payload.api_key, payload.api_password)
            )
            if preserving_api_secret:
                saved_authority = (
                    api_credential_authority(
                        platform=existing_system.truenas.platform,
                        host=existing_system.truenas.host,
                        username=existing_system.truenas.api_user,
                        verify_tls=existing_system.truenas.verify_ssl,
                        tls_ca_bundle_path=existing_system.truenas.tls_ca_bundle_path,
                        tls_server_name=existing_system.truenas.tls_server_name,
                    )
                    if existing_system is not None
                    else None
                )
                requested_authority = api_credential_authority(
                    platform=payload.platform,
                    host=payload.truenas_host,
                    username=payload.api_user,
                    verify_tls=payload.verify_ssl,
                    tls_ca_bundle_path=tls_ca_bundle_path,
                    tls_server_name=tls_server_name,
                )
                if not same_credential_authority(requested_authority, saved_authority):
                    raise ValueError(
                        "A saved secret can only be reused with its saved connection settings."
                    )

            preserving_ssh_secret = any(
                incoming == PRESERVE_SECRET_SENTINEL
                for incoming in (payload.ssh_password, payload.ssh_sudo_password)
            )
            if preserving_ssh_secret:
                saved_authorities = (
                    ssh_credential_authorities(
                        platform=existing_system.truenas.platform,
                        hosts=[
                            existing_system.ssh.host,
                            *existing_system.ssh.extra_hosts,
                            *(node.host for node in existing_system.ssh.ha_nodes),
                        ],
                        port=existing_system.ssh.port,
                        username=existing_system.ssh.user,
                        strict_host_key_checking=existing_system.ssh.strict_host_key_checking,
                    )
                    if existing_system is not None
                    else frozenset()
                )
                requested_authorities = ssh_credential_authorities(
                    platform=payload.platform,
                    hosts=[ssh_host, *ssh_extra_hosts, *ssh_ha_hosts],
                    port=payload.ssh_port,
                    username=payload.ssh_user,
                    strict_host_key_checking=payload.ssh_strict_host_key_checking,
                )
                if not same_credential_authorities(requested_authorities, saved_authorities):
                    raise ValueError(
                        "A saved secret can only be reused with its saved connection settings."
                    )

            if payload.bmc_password == PRESERVE_SECRET_SENTINEL:
                saved_authority = (
                    bmc_credential_authority(
                        platform=existing_system.truenas.platform,
                        host=existing_system.bmc.host,
                        username=existing_system.bmc.username,
                        verify_tls=existing_system.bmc.verify_ssl,
                    )
                    if existing_system is not None
                    else None
                )
                requested_authority = bmc_credential_authority(
                    platform=payload.platform,
                    host=payload.bmc_host,
                    username=payload.bmc_username,
                    verify_tls=payload.bmc_verify_ssl,
                )
                if not same_credential_authority(requested_authority, saved_authority):
                    raise ValueError(
                        "A saved secret can only be reused with its saved connection settings."
                    )

            def resolve_secret(incoming: str | None, existing: str | None = None) -> str:
                return resolve_preserved_secret(incoming, existing)

            ssh_enabled = bool(payload.ssh_enabled)
            existing_ssh_commands = list(existing_system.ssh.commands) if existing_system else []
            if payload.ssh_commands_action == "preserve":
                if payload.ssh_commands:
                    raise ValueError("A preserved SSH command list cannot include replacement commands.")
                source_system_id = normalize_text(payload.ssh_commands_source_system_id)
                source_index = next(
                    (
                        index
                        for index, item in enumerate(raw_systems)
                        if isinstance(item, dict)
                        and source_system_id
                        and _normalize_system_id(item.get("id"), index + 1) == source_system_id
                    ),
                    None,
                )
                if source_index is None:
                    raise ValueError("The saved SSH command list is unavailable.")
                source_system = SystemConfig.model_validate(raw_systems[source_index])
                ssh_commands = list(source_system.ssh.commands)
            elif payload.ssh_commands_action == "replace":
                ssh_commands = list(payload.ssh_commands)
            else:
                ssh_commands = (
                    payload.ssh_commands
                    or existing_ssh_commands
                    or default_ssh_commands_for_platform(payload.platform)
                )
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
                    api_key=resolve_secret(
                        payload.api_key,
                        existing_system.truenas.api_key if existing_system is not None else None,
                    ),
                    api_user=payload.api_user or "",
                    api_password=resolve_secret(
                        payload.api_password,
                        existing_system.truenas.api_password if existing_system is not None else None,
                    ),
                    platform=payload.platform,
                    verify_ssl=payload.verify_ssl,
                    tls_ca_bundle_path=tls_ca_bundle_path,
                    tls_server_name=tls_server_name,
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
                    extra_hosts=ssh_extra_hosts,
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
                    password=resolve_secret(
                        payload.ssh_password,
                        existing_system.ssh.password if existing_system is not None else None,
                    ),
                    sudo_password=resolve_secret(
                        payload.ssh_sudo_password,
                        existing_system.ssh.sudo_password if existing_system is not None else None,
                    ),
                    known_hosts_path=_derive_runtime_layout_paths(self.config_path)["known_hosts_path"],
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
                    password=resolve_secret(
                        payload.bmc_password,
                        existing_system.bmc.password if existing_system is not None else None,
                    ),
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
