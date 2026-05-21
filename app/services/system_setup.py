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
from app.services.sas_fabric import (
    CORE_DMIDECODE_SLOT_COMMAND,
    CORE_MPR_DMESG_EVENTS_COMMAND,
    CORE_MPR_SYSCTL_LOCATION_COMMAND,
    CORE_PCICONF_LV_COMMAND,
)


_CONFIG_WRITE_LOCK = threading.Lock()
LINUX_NVME_LIST_SUBSYS_COMMAND = (
    "/usr/sbin/nvme list-subsys -o json 2>/dev/null || "
    "/usr/bin/nvme list-subsys -o json 2>/dev/null || true"
)


_PLATFORM_SETUP_REQUIREMENTS: dict[str, dict[str, object]] = {
    "core": {
        "summary": "TrueNAS CORE uses middleware API inventory, with optional FreeBSD SSH enrichment for physical slots, SMART detail, identify LEDs, and SAS Fabric diagnostics.",
        "required": (
            "TrueNAS CORE API key for base disks, pools, and enclosure rows.",
            "Saved enclosure/profile selection when more than one live enclosure or view is present.",
        ),
        "optional": (
            "CORE SSH service account for sesutil, camcontrol, smartctl, and read-only mprutil diagnostics.",
            "Narrow pciconf, dmidecode -t slot, and /var/log/messages tail permissions for HBA slot labels and timestamped SAS Fabric evidence.",
        ),
        "unsupported": (
            "Linux lsscsi/sg_ses discovery is not used on CORE.",
            "ESXi host-prep and Linux sudoers bootstrap flows are not CORE runtime paths.",
        ),
        "guidance": "Use the CORE midclt permission preview for command-limited sudo instead of adding broad shell access.",
    },
    "scale": {
        "summary": "TrueNAS SCALE combines middleware API inventory with Linux-side SSH enrichment for SES slot mapping, SMART detail, and optional identify LEDs.",
        "required": (
            "TrueNAS SCALE API key for disks, pools, and base inventory.",
            "SSH commands /usr/bin/lsblk --json and /usr/bin/lsscsi -g -t when Linux evidence must provide block and SCSI transport detail.",
        ),
        "optional": (
            "sg3_utils sg_ses AES/EC and --join --filter reads for each discovered /dev/sgN enclosure device.",
            "smartmontools smartctl -x for SMART detail and history enrichment.",
            "nvme-cli list-subsys JSON output for NVMe controller and PCIe-path context when NVMe devices are present.",
            "sg_ses identify rules only after the SG device and slot mapping are verified.",
        ),
        "unsupported": (
            "TrueNAS CORE/BSD tools sesutil, mprutil, and camcontrol.",
            "CORE-only SAS Fabric topology and mprutil diagnostics.",
        ),
        "guidance": "Let lsscsi -g -t name the live /dev/sgN devices and transport addresses, then prefer exact sg_ses -p aes/ec plus --join --filter rules for those devices; the wildcard bootstrap rules are a convenience, not a requirement to ask for BSD tools.",
    },
    "linux": {
        "summary": "Generic Linux is SSH-first: inventory starts with lsblk, then profile, SES, BMC, mdadm, NVMe, or vendor sources determine how physical the view can be.",
        "required": (
            "SSH host, user/key, and stable-column /usr/bin/lsblk --json output for base disk inventory.",
            "A selected profile, storage view, SES source, BMC source, or vendor source when physical slot rendering is expected.",
        ),
        "optional": (
            "mdadm for software RAID context and nvme-cli for NVMe subsystem context.",
            "smartmontools smartctl -x for SMART detail and history enrichment.",
            "lsscsi -g -t and sg3_utils sg_ses AES/EC/join reads for SES-backed chassis after lsscsi shows enclosure SG devices.",
            "BMC/IPMI or vendor commands where the platform has proven slot metadata.",
        ),
        "unsupported": (
            "TrueNAS API-only setup.",
            "CORE SAS Fabric mprutil diagnostics and ESXi host-prep package installation.",
        ),
        "guidance": "For Linux SES, grant sg_ses AES/EC/join reads for the exact SG devices discovered by lsscsi -g -t and only enable identify writes after slot mapping is proven.",
    },
    "quantastor": {
        "summary": "Quantastor is REST-first, with optional HA-node SSH enrichment for shared SES faces, qs CLI details, and smartctl.",
        "required": (
            "Quantastor REST endpoint plus API user/password.",
            "A selected storage system or enclosure view from the REST inventory.",
        ),
        "optional": (
            "One or more HA-node SSH hosts when internal views or shared SES access need node-specific evidence.",
            "qs CLI, sg_ses, and smartctl on the node that can see the shared enclosure.",
            "Extra HA node hosts for redundant visibility and failover context.",
        ),
        "unsupported": (
            "TrueNAS CORE SAS Fabric topology.",
            "ESXi host-prep and ESXi storage CLI install flow.",
        ),
        "guidance": "Treat Quantastor as a cluster plus visible HA nodes: REST owns the system view, and SSH should name the node that can actually see SES or internal media.",
    },
    "esxi": {
        "summary": "VMware ESXi stays host-managed and read-only here; SSH, ESXCLI, and StorCLI provide inventory while optional BMC access can add out-of-band chassis context.",
        "required": (
            "ESXi SSH access for the saved host.",
            "ESXCLI storage commands for adapters, devices, paths, filesystems, and VMFS extents.",
            "Vendor storage CLI such as StorCLI or PercCLI for physical RAID-member detail.",
        ),
        "optional": (
            "Operator-supplied ESXi offline bundle or VIB for the host-prep upload/install flow.",
            "BMC/IPMI access for out-of-band chassis or drive-locate context where supported.",
        ),
        "unsupported": (
            "Linux sudoers/bootstrap and saved sudo-password flows.",
            "TrueNAS API setup and CORE SAS Fabric diagnostics.",
            "Slot identify writes from the ESXi storage path.",
        ),
        "guidance": "If the controller is not c0, edit the recommended StorCLI commands to the observed /cN or /call target before saving.",
    },
    "ipmi": {
        "summary": "IPMI / BMC Only systems use out-of-band controller access as the primary path, usually paired with a saved profile so empty slots can render.",
        "required": (
            "BMC host, username, and password.",
            "A profile that matches the chassis face when host-side inventory is not available.",
        ),
        "optional": (
            "Host SSH can be added later for SMART, SES, or storage-topology enrichment when a safe host path exists.",
            "TLS certificate trust if the BMC exposes HTTPS with a private CA.",
        ),
        "unsupported": (
            "SMART detail, history, SES, and storage topology without a host-side source.",
            "TrueNAS API, Linux sudoers bootstrap, and ESXi host-prep as primary BMC-only setup paths.",
        ),
        "guidance": "Use BMC-only entries for chassis/locator visibility first; add host-side SSH later only when you need disk health or topology data.",
    },
}


def setup_requirements_for_platform(platform: str) -> dict[str, object]:
    normalized = normalize_text(platform) or "core"
    payload = _PLATFORM_SETUP_REQUIREMENTS.get(normalized, _PLATFORM_SETUP_REQUIREMENTS["core"])
    return {
        "summary": str(payload.get("summary") or ""),
        "required": list(payload.get("required") or ()),
        "optional": list(payload.get("optional") or ()),
        "unsupported": list(payload.get("unsupported") or ()),
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
