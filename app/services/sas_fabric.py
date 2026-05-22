from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import SystemConfig
from app.models.domain import (
    InventorySnapshot,
    SasFabricAlias,
    SasFabricLink,
    SasFabricNode,
    SasFabricSnapshot,
    SasFabricTrace,
    SourceStatus,
    SlotView,
)
from app.services.sas_diagnostics import (
    finalize_mpr_event_summary,
    make_decoded_event_record,
    new_mpr_event_summary,
    record_mpr_event_summary,
)
from app.services.parsers import canonicalize_ssh_command, normalize_text


CORE_MPRUTIL_UNIT_SUBCOMMANDS = ("adapter", "devices", "enclosures", "expanders", "iocfacts")
CORE_MESSAGES_TAIL_COMMAND = "tail -n 4000 /var/log/messages"
CORE_MESSAGES_TAIL_SUDO_COMMAND = "sudo -n /usr/bin/tail -n 4000 /var/log/messages"
CORE_MPR_DMESG_EVENTS_COMMAND = (
    "messages=$({ "
    f"{CORE_MESSAGES_TAIL_COMMAND} 2>/dev/null || "
    f"{CORE_MESSAGES_TAIL_SUDO_COMMAND} 2>/dev/null || true; "
    "} | egrep '(mpr[0-9]+:|\\(da[0-9]+:mpr[0-9]+:)' || true); "
    "if [ -n \"$messages\" ]; then printf '%s\\n' \"$messages\" | tail -n 400; "
    "else dmesg -a | egrep '(mpr[0-9]+:|\\(da[0-9]+:mpr[0-9]+:)' | tail -n 400; fi"
)
CORE_PCICONF_LV_COMMAND = "/usr/sbin/pciconf -lv"
CORE_PCICONF_LV_OPTIONAL_COMMAND = f"{CORE_PCICONF_LV_COMMAND} 2>/dev/null || true"
CORE_DMIDECODE_SLOT_COMMAND = "sudo -n /usr/local/sbin/dmidecode -t slot"
CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND = f"{CORE_DMIDECODE_SLOT_COMMAND} 2>/dev/null || true"
CORE_MPR_SYSCTL_LOCATION_COMMAND = (
    "sysctl -a 2>/dev/null | egrep '^dev\\.mpr\\.[0-9]+\\.%(location|parent):' || true"
)

def parse_mpr_adapter_summary(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip().startswith("/dev/"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        rows.append(
            {
                "device": parts[0],
                "unit": _mpr_unit_from_text(parts[0]),
                "chip": parts[1],
                "board": " ".join(parts[2:-1]),
                "firmware": parts[-1],
            }
        )
    return rows


def parse_mpr_adapter_detail(text: str) -> dict[str, Any]:
    detail: dict[str, Any] = {"phy_rows": []}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("Adapter:"):
            detail["name"] = stripped.removesuffix(" Adapter:")
            continue
        if ":" in stripped and not re.match(r"^\d+\s+", stripped):
            key, value = stripped.split(":", 1)
            detail[re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")] = value.strip()
            continue
        if re.match(r"^\d+\s+", stripped):
            parts = stripped.split()
            if len(parts) >= 8:
                detail["phy_rows"].append(
                    {
                        "phy": parts[0],
                        "controller_handle": parts[1],
                        "device_handle": parts[2],
                        "disabled": parts[3],
                        "speed": parts[4],
                        "min": parts[5],
                        "max": parts[6],
                        "device": " ".join(parts[7:]),
                    }
                )
    detail["phy_count"] = len(detail.get("phy_rows") or [])
    detail["linked_phy_count"] = sum(
        1
        for row in detail.get("phy_rows") or []
        if row.get("device") and row.get("device") != "No Device"
    )
    return detail


def parse_mpr_devices(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.search(r"\b(SAS|SATA|SMP|SEP)\s+Target\b", stripped):
            continue
        parts = stripped.split()
        sas_index = next((index for index, part in enumerate(parts) if re.fullmatch(r"[0-9a-fA-F]{16}", part)), None)
        if sas_index is None or len(parts) <= sas_index + 6:
            continue
        bus = parts[0] if sas_index >= 2 and re.fullmatch(r"\d+", parts[0]) else None
        target = parts[1] if sas_index >= 2 and re.fullmatch(r"\d+", parts[1]) else None
        device_words: list[str] = []
        index = sas_index + 3
        while index < len(parts) and not re.fullmatch(r"\d+(?:\.\d+)?", parts[index]):
            device_words.append(parts[index])
            index += 1
        if index + 2 >= len(parts):
            continue
        rows.append(
            {
                "sas_address": parts[sas_index],
                "handle": parts[sas_index + 1],
                "parent": parts[sas_index + 2],
                "device": " ".join(device_words),
                "speed": parts[index],
                "enclosure_handle": parts[index + 1],
                "slot": parts[index + 2],
                "bus": bus,
                "target": target,
            }
        )
    return rows


def parse_mpr_enclosures(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r"^\d+\s+[0-9a-fA-F]{8,}", stripped):
            continue
        parts = stripped.split()
        if len(parts) < 5:
            continue
        rows.append(
            {
                "slots": parts[0],
                "logical_id": parts[1],
                "sep_handle": parts[2],
                "enc_handle": parts[3],
                "type": " ".join(parts[4:]),
            }
        )
    return rows


def parse_mpr_expanders(text: str) -> list[dict[str, Any]]:
    expanders: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    header_pattern = re.compile(
        r"^\s*(\d+)\s+([0-9a-fA-F]{16})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+(\d+)\s*$"
    )
    phy_pattern = re.compile(
        r"^\s*(\d+)\s+(?:(\d+)\s+([0-9a-fA-F]{4})\s+([\d.]+)|)\s*([\d.]+|\?\?\?)\s+([\d.]+|\?\?\?)\s+(.+?)\s*$"
    )
    for line in text.splitlines():
        header = header_pattern.match(line)
        if header:
            current = {
                "num_phys": header.group(1),
                "sas_address": header.group(2),
                "dev_handle": header.group(3),
                "parent": header.group(4),
                "enc_handle": header.group(5),
                "sas_level": header.group(6),
                "phys": [],
            }
            expanders.append(current)
            continue
        if current is None:
            continue
        phy = phy_pattern.match(line)
        if not phy:
            continue
        device = phy.group(7).strip()
        if device.lower().startswith("phy "):
            continue
        current["phys"].append(
            {
                "phy": phy.group(1),
                "remote_phy": phy.group(2),
                "dev_handle": phy.group(3),
                "speed": phy.group(4),
                "min": phy.group(5),
                "max": phy.group(6),
                "device": device,
            }
        )
    for expander in expanders:
        counts = Counter(str(row.get("device") or "unknown").strip() for row in expander.get("phys") or [])
        expander["device_counts"] = dict(counts)
        expander["linked_phys"] = sum(1 for row in expander.get("phys") or [] if row.get("device") != "No Device")
    return expanders


def parse_mpr_iocfacts(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
        if normalized_key and value.strip():
            facts[normalized_key] = value.strip()
    return facts


def parse_pciconf_sas_controllers(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    header_pattern = re.compile(
        r"^(?P<driver>[A-Za-z0-9_.-]+)@pci(?P<domain>\d+):(?P<bus>\d+):(?P<slot>\d+):"
        r"(?P<function>\d+):\s*(?P<attrs>.*)$"
    )
    for line in text.splitlines():
        header = header_pattern.match(line.strip())
        if header:
            driver = header.group("driver")
            if not re.match(r"^(?:mpr|mps)\d+$", driver, flags=re.IGNORECASE):
                current = None
                continue
            current = {
                "controller": driver,
                "unit": _mpr_unit_from_text(driver) or "",
                "pci_location": (
                    f"pci{header.group('domain')}:{header.group('bus')}:"
                    f"{header.group('slot')}:{header.group('function')}"
                ),
                "pci_address": _pciconf_bus_address(
                    header.group("domain"),
                    header.group("bus"),
                    header.group("slot"),
                    header.group("function"),
                ),
            }
            for key, value in re.findall(r"([A-Za-z0-9_]+)=([^\s]+)", header.group("attrs")):
                normalized_key = key.lower()
                if normalized_key == "class":
                    current["class_code"] = value
                elif normalized_key == "rev":
                    current["revision"] = value
                elif normalized_key == "vendor":
                    current["vendor_id"] = value
                elif normalized_key == "device":
                    current["device_id"] = value
                elif normalized_key in {"subvendor", "subdevice"}:
                    current[f"{normalized_key}_id"] = value
                else:
                    current[normalized_key] = value
            rows.append(current)
            continue

        if current is None:
            continue
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        normalized_key = key.strip().lower().replace(" ", "_")
        cleaned_value = value.strip().strip("'\"")
        if normalized_key == "vendor":
            current["vendor_name"] = cleaned_value
        elif normalized_key == "device":
            current["device_name"] = cleaned_value
        elif normalized_key == "class":
            current["class_name"] = cleaned_value
        elif normalized_key == "subclass":
            current["subclass_name"] = cleaned_value
        else:
            current[normalized_key] = cleaned_value
    return rows


def parse_dmidecode_slots(text: str) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    collecting_characteristics = False

    def flush_current() -> None:
        nonlocal current
        if current and (current.get("designation") or current.get("bus_address")):
            slots.append(current)
        current = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Handle "):
            flush_current()
            collecting_characteristics = False
            continue
        if stripped == "System Slot Information":
            current = {}
            collecting_characteristics = False
            continue
        if current is None:
            continue
        if not stripped:
            flush_current()
            collecting_characteristics = False
            continue
        if stripped == "Characteristics:":
            current["characteristics"] = []
            collecting_characteristics = True
            continue
        if collecting_characteristics and ":" not in stripped:
            current.setdefault("characteristics", []).append(stripped)
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
        cleaned_value = value.strip()
        if not normalized_key or not cleaned_value:
            continue
        if normalized_key == "id":
            normalized_key = "slot_id"
        elif normalized_key == "bus_address":
            cleaned_value = cleaned_value.lower()
        current[normalized_key] = cleaned_value
        collecting_characteristics = False

    flush_current()
    return slots


def parse_mpr_sysctl_locations(text: str) -> dict[str, dict[str, str]]:
    controllers: dict[str, dict[str, str]] = {}
    location_pattern = re.compile(r"^dev\.mpr\.(?P<unit>\d+)\.%location:\s*(?P<value>.+)$")
    parent_pattern = re.compile(r"^dev\.mpr\.(?P<unit>\d+)\.%parent:\s*(?P<value>.+)$")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        location_match = location_pattern.match(stripped)
        if location_match:
            controller = f"mpr{location_match.group('unit')}"
            row = controllers.setdefault(controller, {"controller": controller, "unit": location_match.group("unit")})
            for key, value in re.findall(r"([a-zA-Z_]+)=([^\s]+)", location_match.group("value")):
                normalized_key = key.lower()
                if normalized_key == "dbsf":
                    row["pci_location"] = value
                    row["pci_address"] = _freebsd_pci_location_to_address(value)
                elif normalized_key == "handle":
                    row["acpi_handle"] = value
                else:
                    row[f"pci_{normalized_key}"] = value
            row["raw_location"] = location_match.group("value").strip()
            continue
        parent_match = parent_pattern.match(stripped)
        if parent_match:
            controller = f"mpr{parent_match.group('unit')}"
            row = controllers.setdefault(controller, {"controller": controller, "unit": parent_match.group("unit")})
            row["pci_parent"] = parent_match.group("value").strip()
    return controllers


def parse_mpr_dmesg_events(text: str) -> dict[str, Any]:
    controller_pattern = re.compile(
        r"^(?P<controller>mpr\d+):\s+(?P<message>.+?)"
        r"(?:\s+tgt\s+(?P<target>\d+)\s+SMID\s+(?P<smid>\d+)\s+loginfo\s+(?P<loginfo>[0-9a-fA-F]+))?$"
    )
    disk_pattern = re.compile(
        r"^\((?P<device>da\d+):(?P<controller>mpr\d+):(?P<bus>\d+):"
        r"(?P<target>\d+):(?P<lun>\d+)\):\s+(?P<message>.+)$"
    )
    summaries = {
        "by_controller": defaultdict(new_mpr_event_summary),
        "by_device": defaultdict(new_mpr_event_summary),
        "by_controller_target": defaultdict(new_mpr_event_summary),
    }
    recent_events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        source_timestamp, source_line = _split_mpr_dmesg_timestamp(stripped)
        event: dict[str, Any] | None = None
        disk_match = disk_pattern.match(source_line)
        if disk_match:
            message = disk_match.group("message").strip()
            event = {
                "source": "cam",
                "controller": disk_match.group("controller"),
                "device": disk_match.group("device"),
                "bus": disk_match.group("bus"),
                "target": disk_match.group("target"),
                "lun": disk_match.group("lun"),
                "message": message,
                "event_type": _mpr_dmesg_event_type(message),
                "severity": _mpr_dmesg_severity(message),
                "line": stripped,
            }
            if source_timestamp:
                event["timestamp_raw"] = source_timestamp
            sense = _parse_mpr_sense_message(message)
            if sense:
                event.update(sense)
        else:
            controller_match = controller_pattern.match(source_line)
            if controller_match:
                message = controller_match.group("message").strip()
                event = {
                    "source": "controller",
                    "controller": controller_match.group("controller"),
                    "target": controller_match.group("target"),
                    "smid": controller_match.group("smid"),
                    "loginfo": controller_match.group("loginfo"),
                    "message": message,
                    "event_type": _mpr_dmesg_event_type(message),
                    "severity": "error",
                    "line": stripped,
                }
                if source_timestamp:
                    event["timestamp_raw"] = source_timestamp
        if not event:
            continue
        event_id = f"mpr-dmesg-{len(recent_events) + 1:04d}"
        event["event_id"] = event_id
        decoded_record = make_decoded_event_record(event, event_id=event_id, sequence=len(recent_events))
        recent_events.append(event)
        record_mpr_event_summary(summaries["by_controller"][event["controller"]], event, decoded_record)
        if event.get("device"):
            record_mpr_event_summary(summaries["by_device"][event["device"]], event, decoded_record)
        if event.get("target"):
            key = f"{event['controller']}:{event['target']}"
            record_mpr_event_summary(summaries["by_controller_target"][key], event, decoded_record)

    return {
        "event_count": len(recent_events),
        "recent_events": recent_events[-40:],
        "by_controller": {
            key: finalize_mpr_event_summary(summary)
            for key, summary in sorted(summaries["by_controller"].items())
        },
        "by_device": {
            key: finalize_mpr_event_summary(summary)
            for key, summary in sorted(summaries["by_device"].items())
        },
        "by_controller_target": {
            key: finalize_mpr_event_summary(summary)
            for key, summary in sorted(summaries["by_controller_target"].items())
        },
    }


def _split_mpr_dmesg_timestamp(line: str) -> tuple[str | None, str]:
    bracketed_match = re.match(r"^\[(?P<timestamp>\s*\d+(?:\.\d+)?)\]\s+(?P<rest>.+)$", line)
    if bracketed_match:
        return f"[{bracketed_match.group('timestamp').strip()}]", bracketed_match.group("rest").strip()

    iso_match = re.match(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<rest>.+)$",
        line,
    )
    if iso_match:
        return iso_match.group("timestamp"), _strip_syslog_kernel_prefix(iso_match.group("rest").strip())

    syslog_match = re.match(
        r"^(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<rest>.+)$",
        line,
    )
    if syslog_match:
        return syslog_match.group("timestamp"), _strip_syslog_kernel_prefix(syslog_match.group("rest").strip())

    return None, line


def _strip_syslog_kernel_prefix(line: str) -> str:
    kernel_match = re.match(r"^(?:\S+\s+)?kernel:\s+(?P<rest>.+)$", line)
    if kernel_match:
        return kernel_match.group("rest").strip()
    host_prefixed_match = re.match(
        r"^\S+\s+(?P<rest>(?:mpr\d+:|\(da\d+:mpr\d+:).+)$",
        line,
    )
    return host_prefixed_match.group("rest").strip() if host_prefixed_match else line


def _mpr_dmesg_event_type(message: str) -> str:
    lowered = message.lower()
    if "controller reported" in lowered or "ioc terminated" in lowered:
        return "ioc_terminated"
    if lowered.startswith("cam status:"):
        return "cam_status"
    if re.match(r"error\s+\d+\s*,", lowered):
        return "cam_error"
    if lowered.startswith("scsi sense:"):
        return "scsi_sense"
    if lowered.startswith("scsi status:"):
        return "scsi_status"
    if lowered.startswith("retrying"):
        return "retry"
    if ". cdb:" in lowered or " cdb:" in lowered:
        return "cdb"
    return "message"


def _mpr_dmesg_severity(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("error", "aborted", "nak", "timeout", "connection lost", "terminated")):
        return "error"
    if lowered.startswith("scsi status:") and any(
        token in lowered
        for token in (
            "check condition",
            "busy",
            "reservation conflict",
            "task set full",
            "aca active",
        )
    ):
        return "warning"
    if lowered.startswith("retrying"):
        return "warning"
    return "info"


def _parse_mpr_sense_message(message: str) -> dict[str, str]:
    match = re.search(r"SCSI sense:\s*(?P<sense>.+?)\s+asc:(?P<asc>[0-9a-fA-F]+,[0-9a-fA-F]+)\s+\((?P<reason>.+)\)", message)
    if not match:
        return {}
    return {
        "sense": match.group("sense").strip(),
        "sense_key": match.group("sense").strip().upper(),
        "asc": match.group("asc").lower(),
        "reason": match.group("reason").strip(),
    }


def discover_mpr_units_from_adapter_summary(text: str) -> list[int]:
    units: set[int] = set()
    for row in parse_mpr_adapter_summary(text):
        unit = row.get("unit")
        if unit is not None and str(unit).isdigit():
            units.add(int(unit))
    return sorted(units)


def build_core_mprutil_unit_commands(adapter_summary_output: str, seen_commands: set[str] | None = None) -> list[str]:
    seen = seen_commands or set()
    commands: list[str] = []
    for unit in discover_mpr_units_from_adapter_summary(adapter_summary_output):
        for subcommand in CORE_MPRUTIL_UNIT_SUBCOMMANDS:
            command = f"sudo -n /usr/sbin/mprutil -u {unit} show {subcommand}"
            if canonicalize_ssh_command(command) in seen:
                continue
            commands.append(command)
    return commands


def _select_sas_fabric_builder_key(system: SystemConfig, snapshot: InventorySnapshot) -> str:
    platform = system.truenas.platform
    if platform in {"scale", "linux"} and _snapshot_has_linux_ses_evidence(snapshot):
        return "linux_ses"
    if platform != "core":
        return "platform_storage"
    return "core_mpr"


@dataclass(frozen=True)
class SasFabricBuildContext:
    system: SystemConfig
    snapshot: InventorySnapshot
    normalized_outputs: dict[str, str]
    sources: dict[str, SourceStatus] | None
    warnings: list[str]
    command_failures: list[dict[str, Any]] | None
    aliases: list[SasFabricAlias] | None
    alias_map: dict[str, SasFabricAlias]


class SasFabricBuilder(Protocol):
    key: str

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        ...


class CoreMprFabricBuilder:
    key = "core_mpr"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_core_mpr_fabric_snapshot(
            system=context.system,
            snapshot=context.snapshot,
            normalized_outputs=context.normalized_outputs,
            sources=context.sources,
            warnings=context.warnings,
            command_failures=context.command_failures,
            aliases=context.aliases,
            alias_map=context.alias_map,
        )


class LinuxSesFabricBuilder:
    key = "linux_ses"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_linux_ses_fabric_snapshot(
            system=context.system,
            snapshot=context.snapshot,
            sources=context.sources,
            warnings=context.warnings,
            command_failures=context.command_failures,
            aliases=context.aliases,
            alias_map=context.alias_map,
        )


class PlatformStorageFabricBuilder:
    key = "platform_storage"

    def build(self, context: SasFabricBuildContext) -> SasFabricSnapshot:
        return _build_platform_storage_fabric_snapshot(
            system=context.system,
            snapshot=context.snapshot,
            sources=context.sources,
            warnings=context.warnings,
            command_failures=context.command_failures,
            aliases=context.aliases,
            alias_map=context.alias_map,
        )


_SAS_FABRIC_BUILDERS: tuple[SasFabricBuilder, ...] = (
    CoreMprFabricBuilder(),
    LinuxSesFabricBuilder(),
    PlatformStorageFabricBuilder(),
)
_SAS_FABRIC_BUILDER_REGISTRY: dict[str, SasFabricBuilder] = {
    builder.key: builder for builder in _SAS_FABRIC_BUILDERS
}


def _build_sas_fabric_context(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    ssh_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
) -> SasFabricBuildContext:
    return SasFabricBuildContext(
        system=system,
        snapshot=snapshot,
        normalized_outputs=_canonical_outputs(ssh_outputs),
        sources=sources,
        warnings=list(warnings or []),
        command_failures=command_failures,
        aliases=aliases,
        alias_map=_sas_fabric_alias_map(aliases or []),
    )


def _select_sas_fabric_builder(context: SasFabricBuildContext) -> SasFabricBuilder:
    builder_key = _select_sas_fabric_builder_key(context.system, context.snapshot)
    try:
        return _SAS_FABRIC_BUILDER_REGISTRY[builder_key]
    except KeyError as exc:
        raise RuntimeError(f"No SAS fabric builder registered for {builder_key!r}") from exc


def build_sas_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    ssh_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
) -> SasFabricSnapshot:
    context = _build_sas_fabric_context(
        system=system,
        snapshot=snapshot,
        ssh_outputs=ssh_outputs,
        sources=sources,
        warnings=warnings,
        command_failures=command_failures,
        aliases=aliases,
    )
    return _select_sas_fabric_builder(context).build(context)


def _build_core_mpr_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    normalized_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    alias_map: dict[str, SasFabricAlias] | None = None,
) -> SasFabricSnapshot:
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    selected_enclosure_keys = _selected_enclosure_keys(snapshot)
    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    mpr_kernel_diagnostics = parse_mpr_dmesg_events(normalized_outputs.get("dmesg mpr events", ""))
    add_node(
        nodes,
        SasFabricNode(
            id="host",
            kind="host",
            label=system.label or system.id,
            raw_id=system.id,
            metrics={"slot_count": len(snapshot.slots)},
        ),
    )

    adapter_summary = parse_mpr_adapter_summary(normalized_outputs.get("mprutil show adapters", ""))
    unit_data = _parse_unit_data(normalized_outputs)
    pci_inventory = _pci_inventory_by_controller(
        normalized_outputs.get("pciconf -lv", ""),
        normalized_outputs.get("dmidecode slot", ""),
        normalized_outputs.get("mpr sysctl pci locations", ""),
    )
    path_counts, path_slots = _path_counts_from_slots(snapshot.slots)
    controller_names = sorted(
        {
            f"mpr{row['unit']}"
            for row in adapter_summary
            if row.get("unit") is not None
        }
        | set(path_counts)
        | {f"mpr{unit}" for unit in unit_data}
    )

    controllers: list[dict[str, Any]] = []
    for controller_name in controller_names:
        unit = _mpr_unit_from_text(controller_name)
        summary_row = next((row for row in adapter_summary if row.get("unit") == unit), None)
        parsed_unit = unit_data.get(int(unit)) if unit is not None and unit.isdigit() else None
        detail = parsed_unit.get("adapter_detail", {}) if parsed_unit else {}
        iocfacts = parsed_unit.get("iocfacts", {}) if parsed_unit else {}
        pci_info = pci_inventory.get(controller_name, {})
        pcie_slot = pci_info.get("pcie_slot")
        controller_diagnostics = mpr_kernel_diagnostics.get("by_controller", {}).get(controller_name, {})
        counts = dict(path_counts.get(controller_name, {}))
        related_slots = _dedupe_ints(slot for slots in path_slots.get(controller_name, {}).values() for slot in slots)
        controller_evidence = [f"mprutil -u {unit} show adapter"] if unit is not None else ["mprutil show adapters"]
        if pci_info.get("pci_address"):
            controller_evidence.append("pciconf -lv")
        if pci_info.get("sysctl_pci_location"):
            controller_evidence.append("sysctl dev.mpr.%location")
        if pcie_slot:
            controller_evidence.append("dmidecode -t slot")
        controller = {
            "id": f"controller:{controller_name}",
            "name": controller_name,
            "unit": int(unit) if unit is not None and unit.isdigit() else None,
            "device": summary_row.get("device") if summary_row else f"/dev/{controller_name}",
            "board": detail.get("board_name") or (summary_row or {}).get("board"),
            "chip": (summary_row or {}).get("chip"),
            "firmware": detail.get("firmware_revision") or (summary_row or {}).get("firmware"),
            "temperature": detail.get("temperature"),
            "pci_address": pci_info.get("pci_address"),
            "pci_location": pci_info.get("pci_location"),
            "pci_parent": pci_info.get("pci_parent"),
            "acpi_handle": pci_info.get("acpi_handle"),
            "pcie_slot": pcie_slot,
            "pcie_slot_type": pci_info.get("pcie_slot_type"),
            "pcie_slot_usage": pci_info.get("pcie_slot_usage"),
            "pci_vendor": pci_info.get("vendor_name"),
            "pci_device": pci_info.get("device_name"),
            "pci": pci_info or None,
            "path_counts": counts,
            "related_slots": related_slots,
            "iocfacts": iocfacts,
            "kernel_diagnostics": controller_diagnostics,
        }
        controllers.append(controller)
        add_node(
            nodes,
            SasFabricNode(
                id=controller["id"],
                kind="controller",
                label=controller_name,
                raw_id=controller.get("device"),
                status=_controller_status(counts),
                related_slots=related_slots,
                metrics={
                    "path_counts": counts,
                    "firmware": controller.get("firmware"),
                    "temperature": controller.get("temperature"),
                    "pcie_slot": controller.get("pcie_slot"),
                    "pci_address": controller.get("pci_address"),
                    "phy_count": detail.get("phy_count"),
                    "linked_phy_count": detail.get("linked_phy_count"),
                    "kernel_diagnostics": _diagnostic_metric_summary(controller_diagnostics),
                },
                evidence=_dedupe_strings(controller_evidence),
                raw={key: value for key, value in controller.items() if key not in {"id", "related_slots"}},
            ),
        )
        add_link(links, "host", controller["id"], "host-controller", related_slots=related_slots)

    paths: list[dict[str, Any]] = []
    for controller_name, state_counts in sorted(path_counts.items()):
        controller_id = f"controller:{controller_name}"
        for state, count in sorted(state_counts.items(), key=lambda item: (_path_state_sort_key(item[0]), item[0])):
            slots = sorted(path_slots[controller_name][state])
            path_id = f"path:{controller_name}:{state}"
            paths.append(
                {
                    "id": path_id,
                    "controller": controller_name,
                    "state": state,
                    "count": count,
                    "slots": slots,
                }
            )
            add_node(
                nodes,
                SasFabricNode(
                    id=path_id,
                    kind="path",
                    label=f"{controller_name} {state}",
                    status=state,
                    controller_id=controller_id,
                    related_slots=slots,
                    metrics={"count": count},
                    evidence=["slot.multipath.members"],
                ),
            )
            add_link(links, controller_id, path_id, "controller-path", status=state, related_slots=slots)
            traces[path_id] = SasFabricTrace(
                id=path_id,
                label=f"{controller_name} {state}",
                kind="path",
                node_ids=["host", controller_id, path_id],
                link_ids=[_link_id("host", controller_id, "host-controller"), _link_id(controller_id, path_id, "controller-path")],
                slots=slots,
                metrics={"count": count, "state": state},
                evidence=["slot.multipath.members"],
            )

    ses_devices_by_slot = _ses_devices_by_slot(snapshot.slots)
    _add_mpr_infrastructure(
        nodes=nodes,
        links=links,
        controllers=controllers,
        unit_data=unit_data,
        selected_enclosure_keys=selected_enclosure_keys,
    )
    mpr_trace_index = _build_mpr_trace_index(
        unit_data=unit_data,
        controllers=controllers,
        diagnostics=mpr_kernel_diagnostics,
    )
    backplane_zones = _backplane_zones_for_slots(snapshot.slots)
    for zone in backplane_zones.values():
        add_node(
            nodes,
            SasFabricNode(
                id=zone["id"],
                kind="backplane",
                label=zone["label"],
                related_slots=zone["slots"],
                metrics={
                    "zone": zone["index"] + 1,
                    "slot_range": zone["range"],
                    "slot_count": len(zone["slots"]),
                },
                evidence=["profile slot layout"],
                raw=zone,
            ),
        )

    for slot in snapshot.slots:
        bay_id = f"bay:{slot.slot}"
        bay_related_nodes = ["host", bay_id]
        bay_related_links: list[str] = []
        mpr_device_metrics: list[dict[str, Any]] = []
        backplane_zone = backplane_zones.get(slot.slot)
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {slot.slot:02d}",
                slot=slot.slot,
                status=slot.state.value if hasattr(slot.state, "value") else str(slot.state),
                related_slots=[slot.slot],
                metrics={
                    "present": slot.present,
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                },
                evidence=["inventory snapshot"],
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=["profile slot layout"],
            )
            bay_related_nodes.append(backplane_zone["id"])
            bay_related_links.append(backplane_link.id)
        for member in (slot.multipath.members if slot.multipath else []):
            controller_name = normalize_text(member.controller_label)
            if not controller_name:
                continue
            state = normalize_path_state(member.state)
            controller_id = f"controller:{controller_name}"
            path_id = f"path:{controller_name}:{state}"
            link = add_link(links, path_id, bay_id, "path-bay", status=state, slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.extend([controller_id, path_id])
            bay_related_links.extend(
                [
                    _link_id("host", controller_id, "host-controller"),
                    _link_id(controller_id, path_id, "controller-path"),
                ]
            )
            bay_related_links.append(link.id)
            mpr_context = _add_mpr_member_trace_context(
                nodes=nodes,
                links=links,
                trace_index=mpr_trace_index,
                controller_name=controller_name,
                path_id=path_id,
                bay_id=bay_id,
                slot_number=slot.slot,
                path_state=state,
                member_device_name=member.device_name,
                slot_enclosure_ids=_slot_enclosure_candidates(slot),
                slot_location_numbers=_slot_location_number_candidates(slot),
                diagnostics=mpr_kernel_diagnostics,
            )
            if mpr_context:
                bay_related_nodes.extend(mpr_context["node_ids"])
                bay_related_links.extend(mpr_context["bay_link_ids"])
                mpr_device_metrics.append(mpr_context["metric"])
                path_trace = traces.get(path_id)
                if path_trace:
                    path_trace.node_ids = _dedupe_strings([*path_trace.node_ids, *mpr_context["node_ids"]])
                    path_trace.link_ids = _dedupe_strings([*path_trace.link_ids, *mpr_context["path_link_ids"]])
                    path_trace.evidence = _dedupe_strings([*path_trace.evidence, *mpr_context["evidence"]])
                    path_trace.metrics["mpr_device_count"] = int(path_trace.metrics.get("mpr_device_count") or 0) + 1
                    path_trace.metrics["mpr_enclosure_count"] = len(
                        {
                            node_id
                            for node_id in path_trace.node_ids
                            if node_id.startswith("mpr-enclosure:")
                        }
                    )
                    path_trace.metrics["expander_count"] = len(
                        {
                            node_id
                            for node_id in path_trace.node_ids
                            if node_id.startswith("expander:")
                        }
                    )
        for ses_device in ses_devices_by_slot.get(slot.slot, []):
            ses_id = f"ses:{ses_device}"
            add_node(
                nodes,
                SasFabricNode(
                    id=ses_id,
                    kind="ses-enclosure",
                    label=ses_device,
                    raw_id=ses_device,
                    related_slots=[slot.slot],
                    evidence=["slot.ssh_ses_device"],
                    raw={"ses_device": ses_device},
                ),
            )
            link = add_link(links, ses_id, bay_id, "ses-bay", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(ses_id)
            bay_related_links.append(link.id)
        if slot.pool_name:
            pool_id = f"pool:{slot.pool_name}"
            add_node(nodes, SasFabricNode(id=pool_id, kind="pool", label=slot.pool_name, related_slots=[slot.slot]))
            link = add_link(links, bay_id, pool_id, "bay-pool", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(pool_id)
            bay_related_links.append(link.id)
        if slot.vdev_name:
            vdev_id = f"vdev:{slot.vdev_name}"
            add_node(nodes, SasFabricNode(id=vdev_id, kind="vdev", label=slot.vdev_name, related_slots=[slot.slot]))
            target_id = f"pool:{slot.pool_name}" if slot.pool_name else bay_id
            link = add_link(links, target_id, vdev_id, "pool-vdev", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(vdev_id)
            bay_related_links.append(link.id)
        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {slot.slot:02d}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=[slot.slot],
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "path_states": [
                    {
                        "controller": member.controller_label,
                        "state": normalize_path_state(member.state),
                        "device_name": member.device_name,
                    }
                    for member in (slot.multipath.members if slot.multipath else [])
                ],
                "mpr_devices": mpr_device_metrics,
            },
            evidence=["inventory snapshot", "slot.multipath.members"],
        )

    _scope_mpr_infrastructure(
        nodes=nodes,
        links=links,
        traces=traces,
        snapshot=snapshot,
    )
    for controller in controllers:
        controller_node = nodes.get(str(controller.get("id") or ""))
        if controller_node:
            controller["related_slots"] = list(controller_node.related_slots)

    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )

    expanders = [_node_raw_for_payload(node) for node in nodes.values() if node.kind == "expander"]
    enclosures = [
        _node_raw_for_payload(node)
        for node in nodes.values()
        if node.kind in {"mpr-enclosure", "ses-enclosure"}
    ]

    if not controller_names and not paths:
        fabric_warnings.append("No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions.")

    return SasFabricSnapshot(
        available=bool(controller_names or paths or expanders or enclosures),
        system_id=system.id,
        system_label=system.label,
        platform=system.truenas.platform,
        selected_enclosure_id=snapshot.selected_enclosure_id,
        selected_enclosure_label=snapshot.selected_enclosure_label,
        nodes=sorted(nodes.values(), key=lambda node: (node.kind, node.id)),
        links=sorted(links.values(), key=lambda link: link.id),
        traces=sorted(traces.values(), key=lambda trace: trace.id),
        controllers=controllers,
        expanders=sorted(expanders, key=lambda item: str(item.get("id") or "")),
        enclosures=sorted(enclosures, key=lambda item: str(item.get("id") or "")),
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources or {},
        raw={
            "commands": sorted(normalized_outputs),
            "path_counts": {controller: dict(counts) for controller, counts in sorted(path_counts.items())},
            "path_slots": {
                controller: {state: sorted(slots) for state, slots in sorted(states.items())}
                for controller, states in sorted(path_slots.items())
            },
            "selected_enclosure_keys": sorted(selected_enclosure_keys),
            "selected_bay_slots": _snapshot_slot_numbers(snapshot.slots),
            "selected_disk_slots": _snapshot_disk_slot_numbers(snapshot.slots),
            "pci_controllers": parse_pciconf_sas_controllers(normalized_outputs.get("pciconf -lv", "")),
            "pcie_slots": parse_dmidecode_slots(normalized_outputs.get("dmidecode slot", "")),
            "mpr_sysctl_locations": parse_mpr_sysctl_locations(
                normalized_outputs.get("mpr sysctl pci locations", "")
            ),
            "mpr_kernel_events": {
                "event_count": mpr_kernel_diagnostics.get("event_count", 0),
                "controllers": sorted((mpr_kernel_diagnostics.get("by_controller") or {}).keys()),
                "devices": sorted((mpr_kernel_diagnostics.get("by_device") or {}).keys()),
            },
            "command_failures": list(command_failures or []),
        },
    )


def _slot_storage_identity(slot: SlotView) -> dict[str, Any]:
    values: dict[str, Any] = {
        "serial": slot.serial,
        "model": slot.model,
        "size_bytes": slot.size_bytes,
        "size_human": slot.size_human,
        "gptid": slot.gptid,
        "persistent_id_label": slot.persistent_id_label,
        "vdev_class": slot.vdev_class,
        "topology_label": slot.topology_label,
        "logical_block_size": slot.logical_block_size,
        "physical_block_size": slot.physical_block_size,
        "logical_unit_id": slot.logical_unit_id,
        "sas_address": slot.sas_address,
        "enclosure_id": slot.enclosure_id,
        "enclosure_label": slot.enclosure_label,
        "enclosure_name": slot.enclosure_name,
        "enclosure_identifier": slot.enclosure_identifier,
        "smart_device_type": slot.smart_device_type,
    }
    if slot.smart_device_names:
        values["smart_device_names"] = list(slot.smart_device_names)
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    for key in (
        "sas_device_type",
        "transport_address",
        "linux_hctl",
        "linux_transport",
        "ses_slot_number",
        "ses_disabled",
        "ses_do_not_remove",
        "ses_fault_requested",
        "ses_fault_sensed",
        "ses_predicted_failure",
    ):
        if key in raw_status:
            values[key] = raw_status.get(key)
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def _build_linux_ses_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    alias_map: dict[str, SasFabricAlias] | None = None,
) -> SasFabricSnapshot:
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    slots_with_ses = [
        slot
        for slot in snapshot.slots
        if _slot_ses_devices(slot)
    ]
    platform_label = "TrueNAS SCALE" if system.truenas.platform == "scale" else "Linux"
    if not slots_with_ses:
        fabric_warnings.append(
            f"No {_linux_ses_platform_phrase(platform_label)} slot evidence is available for this selection. "
            "Run stable lsblk --json, lsscsi -g -t, and sg_ses AES/EC/join probes before rendering a SCALE/Linux Storage Fabric map."
        )
        return SasFabricSnapshot(
            available=False,
            system_id=system.id,
            system_label=system.label,
            platform=system.truenas.platform,
            selected_enclosure_id=snapshot.selected_enclosure_id,
            selected_enclosure_label=snapshot.selected_enclosure_label,
            warnings=fabric_warnings,
            sources=sources or {},
            aliases=list(aliases or []),
            raw={
                "fabric_domain": "storage_fabric",
                "fabric_kind": "linux_ses",
                "command_failures": list(command_failures or []),
            },
        )

    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    selected_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    ses_slots: dict[str, list[SlotView]] = defaultdict(list)
    for slot in slots_with_ses:
        primary_ses = _slot_primary_ses_device(slot)
        if primary_ses:
            ses_slots[primary_ses].append(slot)

    controller_id = "controller:linux-ses"
    controller_name = "linux-ses"
    ses_device_count = len(ses_slots)
    add_node(
        nodes,
        SasFabricNode(
            id="host",
            kind="host",
            label=system.label or system.id,
            raw_id=system.id,
            metrics={
                "slot_count": len(snapshot.slots),
                "selected_disk_count": len(selected_disk_slots),
                "fabric_domain": "storage_fabric",
                "fabric_kind": "linux_ses",
            },
            evidence=["inventory snapshot"],
            raw={"platform": system.truenas.platform, "fabric_domain": "storage_fabric", "fabric_kind": "linux_ses"},
        ),
    )
    add_node(
        nodes,
        SasFabricNode(
            id=controller_id,
            kind="controller",
            label="Linux SES",
            raw_id="lsscsi -g -t / sg_ses",
            status="online",
            related_slots=selected_slots,
            metrics={
                "path_counts": {"mapped": len(slots_with_ses)},
                "ses_device_count": ses_device_count,
                "selected_bay_count": len(selected_slots),
                "selected_disk_count": len(selected_disk_slots),
                "fabric_kind": "linux_ses",
            },
            evidence=["lsscsi -g", "lsscsi -g -t", "sg_ses AES/EC", "sg_ses --join --filter"],
            raw={
                "device": "lsscsi -g -t",
                "board": f"{platform_label} Linux SES",
                "path_counts": {"mapped": len(slots_with_ses)},
                "source": "linux_ses",
                "fabric_domain": "storage_fabric",
                "fabric_kind": "linux_ses",
            },
        ),
    )
    add_link(
        links,
        "host",
        controller_id,
        "host-controller",
        related_slots=selected_slots,
        evidence=["inventory snapshot"],
    )

    paths: list[dict[str, Any]] = []
    enclosures_by_ses: dict[str, str] = {}
    for ses_device, slots in sorted(ses_slots.items(), key=lambda item: _linux_ses_sort_key(item[0])):
        ses_token = _object_id_token(ses_device)
        ses_slot_numbers = _dedupe_ints(slot.slot for slot in slots)
        sample_slot = slots[0]
        path_id = f"path:{controller_name}:{ses_token}"
        ses_id = f"ses:{ses_token}"
        enclosures_by_ses[ses_device] = ses_id
        path = {
            "id": path_id,
            "controller": controller_name,
            "state": "mapped",
            "count": len(ses_slot_numbers),
            "slots": ses_slot_numbers,
            "label": f"{ses_device} mapped",
            "ses_device": ses_device,
            "source": "linux_ses",
            "fabric_kind": "linux_ses",
        }
        paths.append(path)
        add_node(
            nodes,
            SasFabricNode(
                id=path_id,
                kind="path",
                label=f"{ses_device} mapped",
                status="mapped",
                controller_id=controller_id,
                related_slots=ses_slot_numbers,
                metrics={"count": len(ses_slot_numbers), "ses_device": ses_device},
                evidence=["lsscsi -g", "lsscsi -g -t", "sg_ses AES/EC", "sg_ses --join --filter"],
                raw={"ses_device": ses_device, "source": "linux_ses", "fabric_domain": "storage_fabric", "fabric_kind": "linux_ses"},
            ),
        )
        add_node(
            nodes,
            SasFabricNode(
                id=ses_id,
                kind="ses-enclosure",
                label=snapshot.selected_enclosure_label or sample_slot.enclosure_label or ses_device,
                raw_id=ses_device,
                controller_id=controller_id,
                related_slots=ses_slot_numbers,
                metrics={
                    "slot_count": len(ses_slot_numbers),
                    "ses_device": ses_device,
                    "selected_enclosure": True,
                    "selected_disk_count": len([slot for slot in slots if _slot_has_disk(slot)]),
                },
                evidence=["sg_ses AES/EC", "sg_ses --join --filter"],
                raw={
                    "id": ses_id,
                    "ses_device": ses_device,
                    "enclosure_id": sample_slot.enclosure_id or snapshot.selected_enclosure_id,
                    "enclosure_label": sample_slot.enclosure_label or snapshot.selected_enclosure_label,
                    "enclosure_name": sample_slot.enclosure_name or snapshot.selected_enclosure_name,
                    "source": "linux_ses",
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                },
            ),
        )
        add_link(
            links,
            controller_id,
            path_id,
            "controller-path",
            status="mapped",
            related_slots=ses_slot_numbers,
            evidence=["lsscsi -g", "lsscsi -g -t"],
        )
        add_link(
            links,
            path_id,
            ses_id,
            "path-ses-enclosure",
            status="mapped",
            related_slots=ses_slot_numbers,
            evidence=["sg_ses AES/EC", "sg_ses --join --filter"],
        )
        traces[path_id] = SasFabricTrace(
            id=path_id,
            label=f"{ses_device} mapped",
            kind="path",
            node_ids=["host", controller_id, path_id, ses_id],
            link_ids=[
                _link_id("host", controller_id, "host-controller"),
                _link_id(controller_id, path_id, "controller-path"),
                _link_id(path_id, ses_id, "path-ses-enclosure"),
            ],
            slots=ses_slot_numbers,
            metrics={"count": len(ses_slot_numbers), "state": "mapped", "ses_device": ses_device},
            evidence=["lsscsi -g", "lsscsi -g -t", "sg_ses AES/EC", "sg_ses --join --filter"],
        )

    backplane_zones = _backplane_zones_for_slots(snapshot.slots)
    for zone in backplane_zones.values():
        add_node(
            nodes,
            SasFabricNode(
                id=zone["id"],
                kind="backplane",
                label=zone["label"],
                related_slots=zone["slots"],
                metrics={
                    "zone": zone["index"] + 1,
                    "slot_range": zone["range"],
                    "slot_count": len(zone["slots"]),
                },
                evidence=["profile slot layout"],
                raw=zone,
            ),
        )

    for slot in snapshot.slots:
        slot_identity = _slot_storage_identity(slot)
        bay_id = f"bay:{slot.slot}"
        ses_device = _slot_primary_ses_device(slot)
        ses_id = enclosures_by_ses.get(ses_device or "")
        path_id = f"path:{controller_name}:{_object_id_token(ses_device)}" if ses_device else None
        bay_related_nodes = _dedupe_strings(["host", controller_id, *( [path_id] if path_id else [] ), *( [ses_id] if ses_id else [] ), bay_id])
        bay_related_links = [
            _link_id("host", controller_id, "host-controller"),
            *([_link_id(controller_id, path_id, "controller-path")] if path_id else []),
            *([_link_id(path_id, ses_id, "path-ses-enclosure")] if path_id and ses_id else []),
        ]
        backplane_zone = backplane_zones.get(slot.slot)
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {slot.slot:02d}",
                slot=slot.slot,
                status=slot.state.value if hasattr(slot.state, "value") else str(slot.state),
                related_slots=[slot.slot],
                metrics={
                    "present": slot.present,
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                    "sas_address": slot.sas_address,
                    "attached_sas_address": getattr(slot, "attached_sas_address", None),
                    "transport_protocol": getattr(slot, "transport_protocol", None),
                    "sg_device": getattr(slot, "sg_device", None),
                    "scsi_hctl": getattr(slot, "scsi_hctl", None),
                    "phy_identifier": getattr(slot, "phy_identifier", None),
                    "target_port_protocol": getattr(slot, "target_port_protocol", None),
                    "ses_device": ses_device,
                    "ses_element_id": slot.ssh_ses_element_id,
                    **slot_identity,
                },
                evidence=["inventory snapshot", "lsblk --json", "lsscsi -g -t", "sg_ses AES/EC", "sg_ses --join --filter"],
                raw={
                    **slot_identity,
                    "ses_device": ses_device,
                    "ses_element_id": slot.ssh_ses_element_id,
                    "ses_targets": list(slot.ssh_ses_targets),
                    "sg_device": getattr(slot, "sg_device", None),
                    "scsi_hctl": getattr(slot, "scsi_hctl", None),
                    "transport_protocol": getattr(slot, "transport_protocol", None),
                    "attached_sas_address": getattr(slot, "attached_sas_address", None),
                    "phy_identifier": getattr(slot, "phy_identifier", None),
                    "target_port_protocol": getattr(slot, "target_port_protocol", None),
                    "linux_blockdevice": slot.raw_status.get("linux_blockdevice") if isinstance(slot.raw_status, dict) else None,
                    "linux_scsi_device": slot.raw_status.get("linux_scsi_device") if isinstance(slot.raw_status, dict) else None,
                    "mapping_source": slot.mapping_source,
                    "source": "linux_ses",
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                },
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=["profile slot layout"],
            )
            bay_related_nodes.append(backplane_zone["id"])
            bay_related_links.append(backplane_link.id)
        if ses_id:
            ses_link = add_link(
                links,
                ses_id,
                bay_id,
                "ses-bay",
                slot=slot.slot,
                related_slots=[slot.slot],
                evidence=["sg_ses AES/EC"],
            )
            bay_related_links.append(ses_link.id)
        if slot.pool_name:
            pool_id = f"pool:{slot.pool_name}"
            add_node(nodes, SasFabricNode(id=pool_id, kind="pool", label=slot.pool_name, related_slots=[slot.slot]))
            link = add_link(links, bay_id, pool_id, "bay-pool", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(pool_id)
            bay_related_links.append(link.id)
        if slot.vdev_name:
            vdev_id = f"vdev:{slot.vdev_name}"
            add_node(nodes, SasFabricNode(id=vdev_id, kind="vdev", label=slot.vdev_name, related_slots=[slot.slot]))
            target_id = f"pool:{slot.pool_name}" if slot.pool_name else bay_id
            link = add_link(links, target_id, vdev_id, "pool-vdev", slot=slot.slot, related_slots=[slot.slot])
            bay_related_nodes.append(vdev_id)
            bay_related_links.append(link.id)
        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {slot.slot:02d}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=[slot.slot],
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "fabric_kind": "linux_ses",
                **slot_identity,
                "path_states": [
                    {
                        "controller": controller_name,
                        "state": "mapped" if ses_device else "unmapped",
                        "device_name": slot.device_name,
                        "pool_name": slot.pool_name,
                        "vdev_name": slot.vdev_name,
                        "ses_device": ses_device,
                        "sg_device": getattr(slot, "sg_device", None),
                        "scsi_hctl": getattr(slot, "scsi_hctl", None),
                        "transport_protocol": getattr(slot, "transport_protocol", None),
                        "target_port_protocol": getattr(slot, "target_port_protocol", None),
                        "attached_sas_address": getattr(slot, "attached_sas_address", None),
                        "phy_identifier": getattr(slot, "phy_identifier", None),
                        "path_id": path_id,
                        **slot_identity,
                    }
                ],
                "mpr_devices": [],
                "ses_device": ses_device,
                "ses_element_id": slot.ssh_ses_element_id,
            },
            evidence=["inventory snapshot", "lsblk --json", "lsscsi -g -t", "sg_ses AES/EC", "sg_ses --join --filter"],
        )

    controllers = [
        {
            "id": controller_id,
            "name": controller_name,
            "device": "lsscsi -g -t",
            "board": f"{platform_label} Linux SES",
            "path_counts": {"mapped": len(slots_with_ses)},
            "related_slots": selected_slots,
            "source": "linux_ses",
            "fabric_kind": "linux_ses",
        }
    ]
    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )
    enclosures = [
        _node_raw_for_payload(node)
        for node in nodes.values()
        if node.kind == "ses-enclosure"
    ]
    fabric_warnings.append(
        f"{platform_label} Storage Fabric map is built from Linux block, SCSI transport, and SES slot evidence. "
        "HBA and expander hop detail is not exposed by this Linux SES evidence."
    )
    return SasFabricSnapshot(
        available=True,
        system_id=system.id,
        system_label=system.label,
        platform=system.truenas.platform,
        selected_enclosure_id=snapshot.selected_enclosure_id,
        selected_enclosure_label=snapshot.selected_enclosure_label,
        nodes=sorted(nodes.values(), key=lambda node: (node.kind, node.id)),
        links=sorted(links.values(), key=lambda link: link.id),
        traces=sorted(traces.values(), key=lambda trace: trace.id),
        controllers=controllers,
        expanders=[],
        enclosures=sorted(enclosures, key=lambda item: str(item.get("id") or "")),
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources or {},
        raw={
            "fabric_domain": "storage_fabric",
            "fabric_kind": "linux_ses",
            "ses_devices": sorted(ses_slots),
            "selected_bay_slots": selected_slots,
            "selected_disk_slots": selected_disk_slots,
            "command_failures": list(command_failures or []),
        },
    )


def _build_platform_storage_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    command_failures: list[dict[str, Any]] | None = None,
    aliases: list[SasFabricAlias] | None = None,
    alias_map: dict[str, SasFabricAlias] | None = None,
) -> SasFabricSnapshot:
    fabric_warnings = list(warnings or [])
    aliases_by_id = alias_map or _sas_fabric_alias_map(aliases or [])
    platform = normalize_text(system.truenas.platform).lower()
    platform_label = _platform_label(platform)
    fabric_kind = _storage_fabric_kind(platform)
    evidence_slots = [slot for slot in snapshot.slots if _slot_has_storage_fabric_evidence(slot)]

    if not evidence_slots:
        fabric_warnings.append(
            f"No Storage Fabric evidence is available for {platform_label}. "
            "This selection needs platform inventory, slot, block-device, controller, or BMC evidence before a graph can render."
        )
        return SasFabricSnapshot(
            available=False,
            system_id=system.id,
            system_label=system.label,
            platform=system.truenas.platform,
            selected_enclosure_id=snapshot.selected_enclosure_id,
            selected_enclosure_label=snapshot.selected_enclosure_label,
            warnings=fabric_warnings,
            sources=sources or {},
            aliases=list(aliases or []),
            raw={
                "fabric_domain": "storage_fabric",
                "fabric_kind": fabric_kind,
                "command_failures": list(command_failures or []),
            },
        )

    nodes: dict[str, SasFabricNode] = {}
    links: dict[str, SasFabricLink] = {}
    traces: dict[str, SasFabricTrace] = {}
    selected_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    add_node(
        nodes,
        SasFabricNode(
            id="host",
            kind="host",
            label=system.label or system.id,
            raw_id=system.id,
            metrics={
                "slot_count": len(snapshot.slots),
                "selected_disk_count": len(selected_disk_slots),
                "fabric_domain": "storage_fabric",
                "fabric_kind": fabric_kind,
            },
            evidence=["inventory snapshot"],
            raw={"platform": system.truenas.platform, "fabric_kind": fabric_kind},
        ),
    )

    controllers_by_name: dict[str, dict[str, Any]] = {}
    paths_by_id: dict[str, dict[str, Any]] = {}
    enclosure_nodes: dict[str, SasFabricNode] = {}
    backplane_zones = _backplane_zones_for_slots(snapshot.slots)
    for zone in backplane_zones.values():
        add_node(
            nodes,
            SasFabricNode(
                id=zone["id"],
                kind="backplane",
                label=zone["label"],
                related_slots=zone["slots"],
                metrics={
                    "zone": zone["index"] + 1,
                    "slot_range": zone["range"],
                    "slot_count": len(zone["slots"]),
                },
                evidence=["profile slot layout"],
                raw=zone,
            ),
        )

    for slot in evidence_slots:
        raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
        route = _storage_fabric_route_for_slot(
            system=system,
            snapshot=snapshot,
            slot=slot,
            fabric_kind=fabric_kind,
            platform_label=platform_label,
        )
        controller_id = route["controller_id"]
        controller_name = route["controller_name"]
        path_id = route["path_id"]
        enclosure_id = route["enclosure_id"]
        slot_numbers = [slot.slot]
        controller_slots = controllers_by_name.setdefault(
            controller_name,
            {
                "id": controller_id,
                "name": controller_name,
                "device": route.get("controller_device"),
                "board": route.get("controller_label"),
                "path_counts": Counter(),
                "related_slots": [],
                "source": route.get("source"),
                "fabric_kind": fabric_kind,
            },
        )
        controller_slots["path_counts"][route["path_state"]] += 1
        controller_slots["related_slots"] = _dedupe_ints([*controller_slots["related_slots"], slot.slot])
        add_node(
            nodes,
            SasFabricNode(
                id=controller_id,
                kind="controller",
                label=route["controller_label"],
                raw_id=route.get("controller_device"),
                status=route["controller_status"],
                related_slots=slot_numbers,
                metrics={
                    "path_counts": dict(controller_slots["path_counts"]),
                    "fabric_kind": fabric_kind,
                    "source": route.get("source"),
                },
                evidence=route["controller_evidence"],
                raw={
                    "device": route.get("controller_device"),
                    "board": route["controller_label"],
                    "source": route.get("source"),
                    "fabric_kind": fabric_kind,
                    **route.get("controller_raw", {}),
                },
            ),
        )
        add_link(
            links,
            "host",
            controller_id,
            "host-controller",
            related_slots=slot_numbers,
            evidence=["inventory snapshot"],
        )

        path = paths_by_id.setdefault(
            path_id,
            {
                "id": path_id,
                "controller": controller_name,
                "state": route["path_state"],
                "count": 0,
                "slots": [],
                "label": route["path_label"],
                "source": route.get("source"),
                "fabric_kind": fabric_kind,
                "path_type": route.get("path_type"),
            },
        )
        path["count"] += 1
        path["slots"] = _dedupe_ints([*path["slots"], slot.slot])
        add_node(
            nodes,
            SasFabricNode(
                id=path_id,
                kind="path",
                label=route["path_label"],
                status=route["path_state"],
                controller_id=controller_id,
                related_slots=slot_numbers,
                metrics={
                    "count": path["count"],
                    "path_type": route.get("path_type"),
                    "fabric_kind": fabric_kind,
                },
                evidence=route["path_evidence"],
                raw=route["path_raw"],
            ),
        )
        add_link(
            links,
            controller_id,
            path_id,
            "controller-path",
            status=route["path_state"],
            related_slots=slot_numbers,
            evidence=route["path_evidence"],
        )

        enclosure_node = add_node(
            nodes,
            SasFabricNode(
                id=enclosure_id,
                kind=route["enclosure_kind"],
                label=route["enclosure_label"],
                raw_id=route.get("enclosure_raw_id"),
                controller_id=controller_id,
                related_slots=slot_numbers,
                metrics={
                    "slot_count": 1,
                    "selected_enclosure": True,
                    "fabric_kind": fabric_kind,
                    "source": route.get("source"),
                },
                evidence=route["enclosure_evidence"],
                raw=route["enclosure_raw"],
            ),
        )
        enclosure_node.metrics["slot_count"] = len(enclosure_node.related_slots)
        enclosure_nodes[enclosure_id] = enclosure_node
        add_link(
            links,
            path_id,
            enclosure_id,
            "path-storage-enclosure",
            status=route["path_state"],
            related_slots=slot_numbers,
            evidence=route["enclosure_evidence"],
        )

        bay_id = f"bay:{slot.slot}"
        backplane_zone = backplane_zones.get(slot.slot)
        bay_related_nodes = _dedupe_strings(
            [
                "host",
                controller_id,
                path_id,
                enclosure_id,
                *( [backplane_zone["id"]] if backplane_zone else [] ),
                bay_id,
            ]
        )
        bay_related_links = [
            _link_id("host", controller_id, "host-controller"),
            _link_id(controller_id, path_id, "controller-path"),
            _link_id(path_id, enclosure_id, "path-storage-enclosure"),
        ]
        add_node(
            nodes,
            SasFabricNode(
                id=bay_id,
                kind="bay",
                label=f"Bay {slot.slot:02d}",
                slot=slot.slot,
                status=_slot_state_text(slot),
                related_slots=slot_numbers,
                metrics={
                    "present": slot.present,
                    "pool_name": slot.pool_name,
                    "vdev_name": slot.vdev_name,
                    "device_name": slot.device_name,
                    "health": slot.health,
                    "transport_protocol": raw_status.get("transport_protocol") or raw_status.get("esxi_transport"),
                    "fabric_kind": fabric_kind,
                },
                evidence=["inventory snapshot"],
                raw={
                    "source": route.get("source"),
                    "fabric_kind": fabric_kind,
                    "operator_context": dict(slot.operator_context or {}),
                    **(slot.raw_status if isinstance(slot.raw_status, dict) else {}),
                },
            ),
        )
        if backplane_zone:
            backplane_link = add_link(
                links,
                backplane_zone["id"],
                bay_id,
                "backplane-bay",
                slot=slot.slot,
                related_slots=slot_numbers,
                evidence=["profile slot layout"],
            )
            bay_related_links.append(backplane_link.id)
        enclosure_link = add_link(
            links,
            enclosure_id,
            bay_id,
            "storage-enclosure-bay",
            slot=slot.slot,
            related_slots=slot_numbers,
            evidence=route["enclosure_evidence"],
        )
        bay_related_links.append(enclosure_link.id)

        if slot.pool_name:
            pool_id = f"pool:{_object_id_token(slot.pool_name)}"
            add_node(nodes, SasFabricNode(id=pool_id, kind="pool", label=slot.pool_name, related_slots=slot_numbers))
            link = add_link(links, bay_id, pool_id, "bay-pool", slot=slot.slot, related_slots=slot_numbers)
            bay_related_nodes.append(pool_id)
            bay_related_links.append(link.id)
        if slot.vdev_name:
            vdev_id = f"vdev:{_object_id_token(slot.vdev_name)}"
            add_node(nodes, SasFabricNode(id=vdev_id, kind="vdev", label=slot.vdev_name, related_slots=slot_numbers))
            target_id = f"pool:{_object_id_token(slot.pool_name)}" if slot.pool_name else bay_id
            link = add_link(links, target_id, vdev_id, "pool-vdev", slot=slot.slot, related_slots=slot_numbers)
            bay_related_nodes.append(vdev_id)
            bay_related_links.append(link.id)

        traces[bay_id] = SasFabricTrace(
            id=bay_id,
            label=f"Bay {slot.slot:02d}",
            kind="bay",
            node_ids=_dedupe_strings(bay_related_nodes),
            link_ids=_dedupe_strings(bay_related_links),
            slots=slot_numbers,
            metrics={
                "device_name": slot.device_name,
                "pool_name": slot.pool_name,
                "vdev_name": slot.vdev_name,
                "path_states": [
                    {
                        "controller": controller_name,
                        "state": route["path_state"],
                        "device_name": slot.device_name,
                        "path_id": path_id,
                        "source": route.get("source"),
                        "path_type": route.get("path_type"),
                    }
                ],
                "mpr_devices": [],
                "fabric_kind": fabric_kind,
            },
            evidence=route["path_evidence"],
        )

    for path in paths_by_id.values():
        traces[path["id"]] = SasFabricTrace(
            id=path["id"],
            label=path["label"],
            kind="path",
            node_ids=_dedupe_strings(["host", f"controller:{path['controller']}", path["id"]]),
            link_ids=[
                _link_id("host", f"controller:{path['controller']}", "host-controller"),
                _link_id(f"controller:{path['controller']}", path["id"], "controller-path"),
            ],
            slots=path["slots"],
            metrics={
                "count": path["count"],
                "state": path["state"],
                "source": path.get("source"),
                "fabric_kind": fabric_kind,
            },
            evidence=["inventory snapshot"],
        )

    controllers = []
    for controller in controllers_by_name.values():
        controller["path_counts"] = dict(controller["path_counts"])
        controllers.append(controller)
    paths = sorted(paths_by_id.values(), key=lambda item: (str(item.get("controller") or ""), str(item.get("label") or "")))
    _apply_sas_fabric_aliases(
        nodes=nodes,
        traces=traces,
        controllers=controllers,
        paths=paths,
        aliases=aliases_by_id,
    )
    enclosures = [_node_raw_for_payload(node) for node in enclosure_nodes.values()]
    fabric_warnings.append(_storage_fabric_scope_warning(platform_label, fabric_kind))
    return SasFabricSnapshot(
        available=True,
        system_id=system.id,
        system_label=system.label,
        platform=system.truenas.platform,
        selected_enclosure_id=snapshot.selected_enclosure_id,
        selected_enclosure_label=snapshot.selected_enclosure_label,
        nodes=sorted(nodes.values(), key=lambda node: (node.kind, node.id)),
        links=sorted(links.values(), key=lambda link: link.id),
        traces=sorted(traces.values(), key=lambda trace: trace.id),
        controllers=sorted(controllers, key=lambda item: str(item.get("name") or "")),
        expanders=[],
        enclosures=sorted(enclosures, key=lambda item: str(item.get("id") or "")),
        paths=paths,
        aliases=list(aliases_by_id.values()),
        warnings=fabric_warnings,
        sources=sources or {},
        raw={
            "fabric_domain": "storage_fabric",
            "fabric_kind": fabric_kind,
            "selected_bay_slots": selected_slots,
            "selected_disk_slots": selected_disk_slots,
            "command_failures": list(command_failures or []),
        },
    )


def normalize_path_state(value: str | None) -> str:
    state = (value or "unknown").strip().lower()
    if "active" in state:
        return "active"
    if "passive" in state:
        return "passive"
    if "fail" in state:
        return "fail"
    if "missing" in state:
        return "missing"
    return state or "unknown"


def add_node(nodes: dict[str, SasFabricNode], node: SasFabricNode) -> SasFabricNode:
    existing = nodes.get(node.id)
    if existing is None:
        nodes[node.id] = node
        return node
    existing.related_slots = _dedupe_ints([*existing.related_slots, *node.related_slots])
    existing.evidence = _dedupe_strings([*existing.evidence, *node.evidence])
    existing.metrics.update({key: value for key, value in node.metrics.items() if value is not None})
    existing.raw.update({key: value for key, value in node.raw.items() if value is not None})
    if existing.status is None:
        existing.status = node.status
    return existing


def add_link(
    links: dict[str, SasFabricLink],
    source: str,
    target: str,
    kind: str,
    *,
    status: str | None = None,
    slot: int | None = None,
    related_slots: list[int] | None = None,
    evidence: list[str] | None = None,
) -> SasFabricLink:
    link_id = _link_id(source, target, kind, slot)
    existing = links.get(link_id)
    if existing is None:
        existing = SasFabricLink(
            id=link_id,
            source=source,
            target=target,
            kind=kind,
            status=status,
            slot=slot,
            related_slots=_dedupe_ints(related_slots or []),
            evidence=evidence or [],
        )
        links[link_id] = existing
        return existing
    existing.related_slots = _dedupe_ints([*existing.related_slots, *(related_slots or [])])
    existing.evidence = _dedupe_strings([*existing.evidence, *(evidence or [])])
    if existing.status is None:
        existing.status = status
    return existing


def _canonical_outputs(ssh_outputs: dict[str, str]) -> dict[str, str]:
    return {
        canonicalize_ssh_command(command): output
        for command, output in ssh_outputs.items()
        if output is not None
    }


def _pciconf_bus_address(domain: str, bus: str, slot: str, function: str) -> str:
    try:
        return f"{int(domain, 10):04x}:{int(bus, 10):02x}:{int(slot, 10):02x}.{int(function, 10)}"
    except ValueError:
        return f"{domain}:{bus}:{slot}.{function}".lower()


def _freebsd_pci_location_to_address(location: str) -> str:
    match = re.match(
        r"^pci(?P<domain>\d+):(?P<bus>\d+):(?P<slot>\d+):(?P<function>\d+)$",
        normalize_text(location),
    )
    if not match:
        return normalize_text(location)
    return _pciconf_bus_address(
        match.group("domain"),
        match.group("bus"),
        match.group("slot"),
        match.group("function"),
    )


def _pci_inventory_by_controller(
    pciconf_text: str,
    dmidecode_text: str,
    sysctl_text: str = "",
) -> dict[str, dict[str, Any]]:
    slots_by_address = {
        str(slot.get("bus_address") or "").lower(): slot
        for slot in parse_dmidecode_slots(dmidecode_text)
        if slot.get("bus_address")
    }
    inventory: dict[str, dict[str, Any]] = {}
    sysctl_rows = parse_mpr_sysctl_locations(sysctl_text)
    for row in parse_pciconf_sas_controllers(pciconf_text):
        controller = normalize_text(row.get("controller"))
        if not controller:
            continue
        sysctl_row = sysctl_rows.get(controller, {})
        pci_address = str(row.get("pci_address") or sysctl_row.get("pci_address") or "").lower()
        slot = slots_by_address.get(pci_address, {})
        inventory[controller] = {
            **sysctl_row,
            **row,
            "pci_address": pci_address or row.get("pci_address"),
            "pcie_slot": slot.get("designation"),
            "pcie_slot_type": slot.get("type"),
            "pcie_slot_usage": slot.get("current_usage"),
            "pcie_slot_length": slot.get("length"),
            "pcie_slot_id": slot.get("slot_id"),
            "pcie_slot_characteristics": slot.get("characteristics") or [],
            "dmidecode_slot": slot or None,
        }
        if sysctl_row.get("pci_location"):
            inventory[controller]["sysctl_pci_location"] = sysctl_row.get("pci_location")
            inventory[controller]["sysctl_pci_address"] = sysctl_row.get("pci_address")
    for controller, sysctl_row in sysctl_rows.items():
        if controller in inventory:
            continue
        pci_address = str(sysctl_row.get("pci_address") or "").lower()
        slot = slots_by_address.get(pci_address, {})
        inventory[controller] = {
            **sysctl_row,
            "sysctl_pci_location": sysctl_row.get("pci_location"),
            "sysctl_pci_address": pci_address or None,
            "pci_address": pci_address or sysctl_row.get("pci_address"),
            "pcie_slot": slot.get("designation"),
            "pcie_slot_type": slot.get("type"),
            "pcie_slot_usage": slot.get("current_usage"),
            "pcie_slot_length": slot.get("length"),
            "pcie_slot_id": slot.get("slot_id"),
            "pcie_slot_characteristics": slot.get("characteristics") or [],
            "dmidecode_slot": slot or None,
        }
    return inventory


def _parse_unit_data(outputs: dict[str, str]) -> dict[int, dict[str, Any]]:
    units: set[int] = set(discover_mpr_units_from_adapter_summary(outputs.get("mprutil show adapters", "")))
    for command in outputs:
        match = re.match(r"mprutil -u (?P<unit>\d+) show (?P<subcommand>[a-z]+)$", command)
        if match:
            units.add(int(match.group("unit")))

    parsed: dict[int, dict[str, Any]] = {}
    for unit in sorted(units):
        parsed[unit] = {
            "adapter_detail": parse_mpr_adapter_detail(outputs.get(f"mprutil -u {unit} show adapter", "")),
            "devices": parse_mpr_devices(outputs.get(f"mprutil -u {unit} show devices", "")),
            "enclosures": parse_mpr_enclosures(outputs.get(f"mprutil -u {unit} show enclosures", "")),
            "expanders": parse_mpr_expanders(outputs.get(f"mprutil -u {unit} show expanders", "")),
            "iocfacts": parse_mpr_iocfacts(outputs.get(f"mprutil -u {unit} show iocfacts", "")),
        }
    if not parsed:
        parsed[-1] = {
            "adapter_detail": parse_mpr_adapter_detail(outputs.get("mprutil show adapter", "")),
            "devices": parse_mpr_devices(outputs.get("mprutil show devices", "")),
            "enclosures": parse_mpr_enclosures(outputs.get("mprutil show enclosures", "")),
            "expanders": parse_mpr_expanders(outputs.get("mprutil show expanders", "")),
            "iocfacts": parse_mpr_iocfacts(outputs.get("mprutil show iocfacts", "")),
        }
    return parsed


def _path_counts_from_slots(slots: list[SlotView]) -> tuple[dict[str, Counter[str]], dict[str, dict[str, list[int]]]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    path_slots: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for slot in slots:
        for member in (slot.multipath.members if slot.multipath else []):
            controller = normalize_text(member.controller_label)
            if not controller:
                continue
            state = normalize_path_state(member.state)
            counts[controller][state] += 1
            path_slots[controller][state].append(slot.slot)
    return dict(counts), {controller: dict(states) for controller, states in path_slots.items()}


def _ses_devices_by_slot(slots: list[SlotView]) -> dict[int, list[str]]:
    devices: dict[int, list[str]] = {}
    for slot in slots:
        candidates = _slot_ses_devices(slot)
        if candidates:
            devices[slot.slot] = candidates
    return devices


def _slot_ses_devices(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    candidates: list[str] = []
    if slot.ssh_ses_device:
        candidates.append(slot.ssh_ses_device)
    for target in slot.ssh_ses_targets:
        ses_device = target.get("ses_device") if isinstance(target, dict) else None
        if isinstance(ses_device, str) and ses_device:
            candidates.append(ses_device)
    for key in ("ses_device", "ssh_ses_device"):
        value = raw_status.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
    return _dedupe_strings(candidates)


def _slot_primary_ses_device(slot: SlotView) -> str | None:
    devices = _slot_ses_devices(slot)
    return devices[0] if devices else None


def _snapshot_has_linux_ses_evidence(snapshot: InventorySnapshot) -> bool:
    return any(_slot_ses_devices(slot) for slot in snapshot.slots)


def _linux_ses_platform_phrase(platform_label: str) -> str:
    return "Linux SES" if platform_label.strip().lower() == "linux" else f"{platform_label} Linux SES"


def _platform_label(platform: str | None) -> str:
    normalized = normalize_text(platform).lower()
    return {
        "core": "TrueNAS CORE",
        "scale": "TrueNAS SCALE",
        "quantastor": "Quantastor",
        "linux": "Linux",
        "esxi": "ESXi",
        "ipmi": "BMC / IPMI",
    }.get(normalized, normalized.upper() if normalized else "This platform")


def _storage_fabric_kind(platform: str | None) -> str:
    normalized = normalize_text(platform).lower()
    return {
        "scale": "storage_scale",
        "linux": "storage_linux",
        "quantastor": "storage_quantastor",
        "esxi": "storage_esxi",
        "ipmi": "storage_bmc",
    }.get(normalized, "storage_generic")


def _storage_fabric_scope_warning(platform_label: str, fabric_kind: str) -> str:
    if fabric_kind == "storage_quantastor":
        return (
            f"{platform_label} Storage Fabric is built from Quantastor storage-system, "
            "HA-node, pool, disk, and optional SES/qs evidence. Low-level controller, path, or expander hops are shown only when those sources prove them."
        )
    if fabric_kind == "storage_esxi":
        return (
            f"{platform_label} Storage Fabric is built from ESXCLI and vendor controller evidence. "
            "It shows host/controller/member relationships without enabling RAID-management actions."
        )
    if fabric_kind in {"storage_linux", "storage_scale"}:
        return (
            f"{platform_label} Storage Fabric is built from Linux block, pool, profile, SMART, and optional SES evidence. "
            "Unproven physical HBA or expander hops are kept out of the map."
        )
    if fabric_kind == "storage_bmc":
        return (
            f"{platform_label} Storage Fabric is limited to BMC slot/chassis evidence for this selection. "
            "Host storage paths need OS or vendor storage data."
        )
    return (
        f"{platform_label} Storage Fabric is built from the platform evidence available in this snapshot. "
        "Unproven physical hops are labeled by omission rather than inferred."
    )


def _slot_has_storage_fabric_evidence(slot: SlotView) -> bool:
    if _slot_has_disk(slot) or slot.present or slot.smart_device_names:
        return True
    if isinstance(slot.operator_context, dict) and any(_truthy_storage_value(value) for value in slot.operator_context.values()):
        return True
    if isinstance(slot.raw_status, dict) and any(_truthy_storage_value(value) for value in slot.raw_status.values()):
        return True
    return False


def _truthy_storage_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if value == "" or value == [] or value == {}:
        return False
    return True


def _slot_state_text(slot: SlotView) -> str:
    if slot.health:
        return normalize_text(slot.health).lower() or "unknown"
    if hasattr(slot.state, "value"):
        return normalize_text(slot.state.value).lower() or "unknown"
    return normalize_text(str(slot.state)).lower() or "unknown"


def _slot_device_candidates(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    raw_device_names = raw_status.get("device_names") if isinstance(raw_status.get("device_names"), list) else []
    return _dedupe_strings(
        [
            slot.device_name,
            *slot.smart_device_names,
            *raw_device_names,
            raw_status.get("device_hint"),
            slot.logical_unit_id,
            slot.sas_address,
            raw_status.get("esxi_device_id"),
            raw_status.get("esxi_runtime_name"),
            raw_status.get("storcli_slot"),
        ]
    )


def _pool_vdev_label(slot: SlotView, fallback: str = "storage path") -> str:
    if slot.topology_label:
        return slot.topology_label
    if slot.pool_name and slot.vdev_name:
        return f"{slot.pool_name} / {slot.vdev_name}"
    if slot.pool_name:
        return slot.pool_name
    if slot.vdev_name:
        return slot.vdev_name
    devices = _slot_device_candidates(slot)
    return devices[0] if devices else fallback


@dataclass(frozen=True)
class StorageFabricRouteContext:
    system: SystemConfig
    snapshot: InventorySnapshot
    slot: SlotView
    raw_status: dict[str, Any]
    operator_context: dict[str, Any]
    fabric_kind: str
    platform_label: str
    platform: str


class StorageFabricRouteProvider(Protocol):
    key: str

    def matches(self, context: StorageFabricRouteContext) -> bool:
        ...

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        ...


class QuantastorStorageRouteProvider:
    key = "quantastor"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "quantastor"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _quantastor_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.operator_context,
            context.fabric_kind,
        )


class EsxiStorageRouteProvider:
    key = "esxi"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "esxi"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _esxi_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
        )


class BmcStorageRouteProvider:
    key = "bmc"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return context.platform == "ipmi"

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _bmc_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
        )


class LinuxStorageRouteProvider:
    key = "linux"

    def matches(self, context: StorageFabricRouteContext) -> bool:
        return True

    def route(self, context: StorageFabricRouteContext) -> dict[str, Any]:
        return _linux_storage_route(
            context.system,
            context.snapshot,
            context.slot,
            context.raw_status,
            context.fabric_kind,
            context.platform_label,
        )


_PLATFORM_STORAGE_ROUTE_PROVIDERS: tuple[StorageFabricRouteProvider, ...] = (
    QuantastorStorageRouteProvider(),
    EsxiStorageRouteProvider(),
    BmcStorageRouteProvider(),
    LinuxStorageRouteProvider(),
)


def _build_storage_fabric_route_context(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    fabric_kind: str,
    platform_label: str,
) -> StorageFabricRouteContext:
    return StorageFabricRouteContext(
        system=system,
        snapshot=snapshot,
        slot=slot,
        raw_status=slot.raw_status if isinstance(slot.raw_status, dict) else {},
        operator_context=slot.operator_context if isinstance(slot.operator_context, dict) else {},
        fabric_kind=fabric_kind,
        platform_label=platform_label,
        platform=normalize_text(system.truenas.platform).lower(),
    )


def _select_storage_fabric_route_provider(context: StorageFabricRouteContext) -> StorageFabricRouteProvider:
    for provider in _PLATFORM_STORAGE_ROUTE_PROVIDERS:
        if provider.matches(context):
            return provider
    raise RuntimeError(f"No Storage Fabric route provider registered for {context.platform!r}")


def _storage_fabric_route_for_slot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    fabric_kind: str,
    platform_label: str,
) -> dict[str, Any]:
    context = _build_storage_fabric_route_context(
        system=system,
        snapshot=snapshot,
        slot=slot,
        fabric_kind=fabric_kind,
        platform_label=platform_label,
    )
    return _select_storage_fabric_route_provider(context).route(context)


def _quantastor_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    operator_context: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    selected_label = (
        normalize_text(operator_context.get("selected_view_label"))
        or normalize_text(snapshot.selected_enclosure_label)
        or normalize_text(system.label)
        or system.id
    )
    controller_name = _object_id_token(f"quantastor-{selected_label}")
    ses_device = _slot_primary_ses_device(slot) or normalize_text(raw_status.get("ses_device"))
    owner_label = normalize_text(operator_context.get("pool_owner_label"))
    fence_label = normalize_text(operator_context.get("fence_owner_label"))
    path_anchor = ses_device or owner_label or _pool_vdev_label(slot, selected_label)
    path_label = ses_device or _pool_vdev_label(slot, selected_label)
    evidence = ["Quantastor REST", "inventory snapshot"]
    if raw_status.get("quantastor_cli_disk"):
        evidence.append("qs disk-list")
    if raw_status.get("quantastor_hw_disk"):
        evidence.append("qs hw-disk-list")
    if ses_device:
        evidence.append("sg_ses AES/EC")
    enclosure_kind = "ses-enclosure" if ses_device else "storage-enclosure"
    enclosure_token = _object_id_token(snapshot.selected_enclosure_id or selected_label)
    return {
        "source": "quantastor",
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": selected_label,
        "controller_device": operator_context.get("selected_view_label") or snapshot.selected_enclosure_id,
        "controller_status": "online",
        "controller_evidence": _dedupe_strings(evidence),
        "controller_raw": {
            "selected_view_label": selected_label,
            "pool_owner_label": owner_label,
            "fence_owner_label": fence_label,
            "visible_on_labels": operator_context.get("visible_on_labels"),
            "io_fencing_enabled": operator_context.get("io_fencing_enabled"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_anchor)}",
        "path_label": path_label,
        "path_state": _slot_state_text(slot),
        "path_type": "quantastor-ha" if operator_context else "quantastor",
        "path_evidence": _dedupe_strings(evidence),
        "path_raw": {
            "source": "quantastor",
            "fabric_kind": fabric_kind,
            "ses_device": ses_device,
            "pool_owner_label": owner_label,
            "fence_owner_label": fence_label,
            "visible_on_labels": operator_context.get("visible_on_labels"),
            "topology_label": slot.topology_label,
        },
        "enclosure_id": f"{enclosure_kind}:{controller_name}:{enclosure_token}",
        "enclosure_kind": enclosure_kind,
        "enclosure_label": snapshot.selected_enclosure_label or selected_label,
        "enclosure_raw_id": ses_device or snapshot.selected_enclosure_id,
        "enclosure_evidence": ["sg_ses AES/EC"] if ses_device else ["Quantastor REST"],
        "enclosure_raw": {
            "source": "quantastor",
            "fabric_kind": fabric_kind,
            "ses_device": ses_device,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": snapshot.selected_enclosure_label,
        },
    }


def _esxi_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    drive = raw_status.get("storcli_physical_drive") if isinstance(raw_status.get("storcli_physical_drive"), dict) else {}
    controller = normalize_text(drive.get("controller_id") or raw_status.get("controller_id") or "storage")
    controller_name = _object_id_token(f"esxi-{controller}")
    controller_label = f"StorCLI {controller}" if controller != "storage" else "ESXi storage"
    connector = normalize_text(
        drive.get("connector_name")
        or drive.get("connected_port")
        or raw_status.get("esxi_runtime_name")
        or slot.vdev_name
        or _pool_vdev_label(slot, "ESXi local storage")
    )
    path_label = connector or _pool_vdev_label(slot, "ESXi local storage")
    evidence = ["ESXCLI storage", "inventory snapshot"]
    if drive:
        evidence.append("StorCLI physical drive")
    if raw_status.get("esxi_smart"):
        evidence.append("esxcli storage core device smart get")
    enclosure_token = _object_id_token(
        raw_status.get("storcli_enclosure_id")
        or drive.get("enclosure_id")
        or drive.get("eid")
        or snapshot.selected_enclosure_id
        or "esxi-local"
    )
    return {
        "source": "esxi",
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": controller_label,
        "controller_device": drive.get("controller_id") or controller,
        "controller_status": "online" if drive else "mapped",
        "controller_evidence": _dedupe_strings(evidence),
        "controller_raw": {
            "controller_id": drive.get("controller_id") or controller,
            "storcli_controller": drive.get("controller_id"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": (normalize_text(drive.get("state") or "") or "").lower() or _slot_state_text(slot),
        "path_type": "storcli-member" if drive else "esxi-storage",
        "path_evidence": _dedupe_strings(evidence),
        "path_raw": {
            "source": "esxi",
            "fabric_kind": fabric_kind,
            "connector_name": drive.get("connector_name"),
            "connected_port": drive.get("connected_port"),
            "esxi_runtime_name": raw_status.get("esxi_runtime_name"),
            "esxi_device_id": raw_status.get("esxi_device_id"),
            "storcli_slot": drive.get("slot_key") or raw_status.get("storcli_slot"),
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{enclosure_token}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": snapshot.selected_enclosure_label or f"ESXi enclosure {enclosure_token}",
        "enclosure_raw_id": raw_status.get("storcli_enclosure_id") or drive.get("enclosure_id"),
        "enclosure_evidence": ["StorCLI physical drive"] if drive else ["ESXCLI storage"],
        "enclosure_raw": {
            "source": "esxi",
            "fabric_kind": fabric_kind,
            "enclosure_id": raw_status.get("storcli_enclosure_id") or drive.get("enclosure_id"),
            "enclosure_label": snapshot.selected_enclosure_label,
        },
    }


def _linux_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
    platform_label: str,
) -> dict[str, Any]:
    devices = _slot_device_candidates(slot)
    device_text = " ".join(devices).lower()
    topology_text = " ".join([slot.topology_label or "", slot.vdev_name or "", slot.pool_name or ""]).lower()
    if "nvme" in device_text or "nvme" in topology_text:
        source_name = "linux-nvme"
        source_label = f"{platform_label} NVMe"
        source = "linux_nvme"
        evidence = ["lsblk", "smartctl", "nvme-cli"]
        path_type = "nvme"
    elif re.search(r"\bmd\d+\b", device_text) or "mdadm" in topology_text or re.search(r"\bmd\d+\b", topology_text):
        source_name = "linux-mdadm"
        source_label = f"{platform_label} mdadm"
        source = "linux_mdadm"
        evidence = ["lsblk", "mdadm", "smartctl"]
        path_type = "mdadm"
    elif fabric_kind == "storage_scale":
        source_name = "scale-storage"
        source_label = "TrueNAS SCALE storage"
        source = "scale_storage"
        evidence = ["TrueNAS SCALE API", "Linux storage"]
        path_type = "scale-storage"
    else:
        source_name = "linux-block"
        source_label = f"{platform_label} block"
        source = "linux_block"
        evidence = ["lsblk", "smartctl"]
        path_type = "block"
    controller_name = _object_id_token(source_name)
    path_label = _pool_vdev_label(slot, devices[0] if devices else source_label)
    enclosure_label = snapshot.selected_enclosure_label or snapshot.selected_enclosure_name or "Storage view"
    return {
        "source": source,
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": source_label,
        "controller_device": devices[0] if devices else source_name,
        "controller_status": "online",
        "controller_evidence": _dedupe_strings([*evidence, "inventory snapshot"]),
        "controller_raw": {"devices": devices, "source": source},
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": _slot_state_text(slot),
        "path_type": path_type,
        "path_evidence": _dedupe_strings([*evidence, "inventory snapshot"]),
        "path_raw": {
            "source": source,
            "fabric_kind": fabric_kind,
            "devices": devices,
            "topology_label": slot.topology_label,
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{_object_id_token(snapshot.selected_enclosure_id or enclosure_label)}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": enclosure_label,
        "enclosure_raw_id": snapshot.selected_enclosure_id,
        "enclosure_evidence": ["profile/storage view"],
        "enclosure_raw": {
            "source": source,
            "fabric_kind": fabric_kind,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": enclosure_label,
        },
    }


def _bmc_storage_route(
    system: SystemConfig,
    snapshot: InventorySnapshot,
    slot: SlotView,
    raw_status: dict[str, Any],
    fabric_kind: str,
) -> dict[str, Any]:
    controller_name = "bmc-ipmi"
    chassis_label = snapshot.selected_enclosure_label or system.label or "BMC chassis"
    path_label = raw_status.get("bmc_controller_id")
    if path_label is not None:
        path_label = f"BMC controller {path_label}"
    else:
        path_label = "BMC slot inventory"
    return {
        "source": "bmc",
        "controller_id": f"controller:{controller_name}",
        "controller_name": controller_name,
        "controller_label": "BMC / IPMI",
        "controller_device": raw_status.get("bmc_controller_id") or "ipmi",
        "controller_status": "present" if slot.present else "mapped",
        "controller_evidence": ["BMC inventory"],
        "controller_raw": {
            "bmc_controller_id": raw_status.get("bmc_controller_id"),
            "bmc_physical_index": raw_status.get("bmc_physical_index"),
        },
        "path_id": f"path:{controller_name}:{_object_id_token(path_label)}",
        "path_label": path_label,
        "path_state": "present" if slot.present else "mapped",
        "path_type": "bmc-slot",
        "path_evidence": ["BMC inventory"],
        "path_raw": {
            "source": "bmc",
            "fabric_kind": fabric_kind,
            "bmc_slot_number": raw_status.get("bmc_slot_number"),
            "bmc_physical_index": raw_status.get("bmc_physical_index"),
        },
        "enclosure_id": f"storage-enclosure:{controller_name}:{_object_id_token(snapshot.selected_enclosure_id or chassis_label)}",
        "enclosure_kind": "storage-enclosure",
        "enclosure_label": chassis_label,
        "enclosure_raw_id": snapshot.selected_enclosure_id,
        "enclosure_evidence": ["BMC inventory"],
        "enclosure_raw": {
            "source": "bmc",
            "fabric_kind": fabric_kind,
            "enclosure_id": snapshot.selected_enclosure_id,
            "enclosure_label": chassis_label,
        },
    }


def _build_mpr_trace_index(
    *,
    unit_data: dict[int, dict[str, Any]],
    controllers: list[dict[str, Any]],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    controller_ids_by_unit = {
        item["unit"]: item["id"]
        for item in controllers
        if item.get("unit") is not None
    }
    devices: dict[tuple[str, str], dict[str, Any]] = {}
    devices_by_location: dict[tuple[str, str, int], dict[str, Any]] = {}
    enclosures: dict[tuple[str, str], str] = {}
    enclosure_keys_by_handle: dict[tuple[str, str], str] = {}
    expanders: dict[tuple[str, str], list[str]] = defaultdict(list)

    for unit, payload in sorted(unit_data.items()):
        controller_id = controller_ids_by_unit.get(unit, f"controller:mpr{unit}" if unit >= 0 else "controller:mpr")
        controller_name = controller_id.removeprefix("controller:")
        for enclosure in payload.get("enclosures") or []:
            enc_handle = str(enclosure.get("enc_handle") or "")
            if not enc_handle:
                continue
            enclosure_id = f"mpr-enclosure:{controller_id}:{enc_handle or enclosure.get('logical_id')}"
            enclosures[(controller_id, enc_handle)] = enclosure_id
            logical_key = _identifier_lookup_key(enclosure.get("logical_id"))
            if logical_key:
                enclosure_keys_by_handle[(controller_id, enc_handle)] = logical_key

        for expander in payload.get("expanders") or []:
            enc_handle = str(expander.get("enc_handle") or "")
            if not enc_handle:
                continue
            expander_id = f"expander:{controller_id}:{expander.get('sas_address') or expander.get('dev_handle')}"
            expanders[(controller_id, enc_handle)].append(expander_id)

        for device in payload.get("devices") or []:
            context = {
                "controller_id": controller_id,
                "controller_name": controller_name,
                "unit": unit,
                **device,
            }
            context["diagnostics"] = _diagnostics_for_mpr_device(
                diagnostics or {},
                controller_name,
                context,
            )
            for candidate in _device_name_candidates(device.get("device")):
                devices[(controller_name, candidate)] = context
            enc_handle = str(device.get("enclosure_handle") or "")
            enclosure_key = enclosure_keys_by_handle.get((controller_id, enc_handle))
            device_slot = _parse_mpr_slot_number(device.get("slot"))
            if enclosure_key and device_slot is not None and _is_mpr_disk_target(device.get("device")):
                devices_by_location[(controller_name, enclosure_key, device_slot)] = context

    return {
        "devices": devices,
        "devices_by_location": devices_by_location,
        "enclosures": enclosures,
        "expanders": {key: _dedupe_strings(value) for key, value in expanders.items()},
    }


def _add_mpr_member_trace_context(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    trace_index: dict[str, Any],
    controller_name: str,
    path_id: str,
    bay_id: str,
    slot_number: int,
    path_state: str,
    member_device_name: str | None,
    slot_enclosure_ids: list[str] | None = None,
    slot_location_numbers: list[int] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    device_context = _lookup_mpr_device_context(
        trace_index,
        controller_name,
        member_device_name,
        slot_enclosure_ids=slot_enclosure_ids,
        slot_location_numbers=slot_location_numbers,
    )
    if not device_context:
        return {}

    controller_id = str(device_context.get("controller_id") or f"controller:{controller_name}")
    try:
        unit = int(device_context.get("unit"))
    except (TypeError, ValueError):
        unit = -1
    evidence = _mpr_evidence(unit, "devices")
    diagnostic_summary = device_context.get("diagnostics") or _diagnostics_for_member_device(
        diagnostics or {},
        member_device_name,
    )
    enc_handle = str(device_context.get("enclosure_handle") or "")
    enclosure_id = trace_index.get("enclosures", {}).get((controller_id, enc_handle))
    expander_ids = list(trace_index.get("expanders", {}).get((controller_id, enc_handle), []))

    node_ids: list[str] = []
    path_link_ids: list[str] = []
    bay_link_ids: list[str] = []
    for expander_id in expander_ids:
        existing_expander = nodes.get(expander_id)
        add_node(
            nodes,
            SasFabricNode(
                id=expander_id,
                kind="expander",
                label=existing_expander.label if existing_expander else expander_id,
                raw_id=existing_expander.raw_id if existing_expander else expander_id,
                controller_id=controller_id,
                related_slots=[slot_number],
                evidence=evidence,
            ),
        )
        node_ids.append(expander_id)
        link = add_link(
            links,
            path_id,
            expander_id,
            "path-expander",
            status=path_state,
            related_slots=[slot_number],
            evidence=evidence,
        )
        path_link_ids.append(link.id)
        bay_link_ids.append(link.id)
        if enclosure_id:
            enclosure_link = add_link(
                links,
                expander_id,
                enclosure_id,
                "expander-enclosure",
                related_slots=[slot_number],
                evidence=evidence,
            )
            path_link_ids.append(enclosure_link.id)
            bay_link_ids.append(enclosure_link.id)

    if enclosure_id:
        existing_enclosure = nodes.get(enclosure_id)
        add_node(
            nodes,
            SasFabricNode(
                id=enclosure_id,
                kind="mpr-enclosure",
                label=existing_enclosure.label if existing_enclosure else enclosure_id,
                raw_id=existing_enclosure.raw_id if existing_enclosure else enclosure_id,
                controller_id=controller_id,
                related_slots=[slot_number],
                evidence=evidence,
            ),
        )
        node_ids.append(enclosure_id)
        if not expander_ids:
            path_link = add_link(
                links,
                path_id,
                enclosure_id,
                "path-mpr-enclosure",
                status=path_state,
                related_slots=[slot_number],
                evidence=evidence,
            )
            path_link_ids.append(path_link.id)
            bay_link_ids.append(path_link.id)
        bay_link = add_link(
            links,
            enclosure_id,
            bay_id,
            "mpr-enclosure-bay",
            status=path_state,
            slot=slot_number,
            related_slots=[slot_number],
            evidence=evidence,
        )
        bay_link_ids.append(bay_link.id)

    return {
        "node_ids": _dedupe_strings(node_ids),
        "path_link_ids": _dedupe_strings(path_link_ids),
        "bay_link_ids": _dedupe_strings(bay_link_ids),
        "evidence": evidence,
        "metric": {
            "controller": controller_name,
            "state": path_state,
            "member_device_name": member_device_name,
            "mpr_device": device_context.get("device"),
            "sas_address": device_context.get("sas_address"),
            "handle": device_context.get("handle"),
            "parent": device_context.get("parent"),
            "speed": device_context.get("speed"),
            "enclosure_handle": enc_handle or None,
            "mpr_slot": device_context.get("slot"),
            "enclosure_id": enclosure_id,
            "expander_ids": expander_ids,
            "diagnostics": diagnostic_summary,
        },
    }


def _lookup_mpr_device_context(
    trace_index: dict[str, Any],
    controller_name: str,
    device_name: str | None,
    *,
    slot_enclosure_ids: list[str] | None = None,
    slot_location_numbers: list[int] | None = None,
) -> dict[str, Any] | None:
    devices = trace_index.get("devices", {})
    for candidate in _device_name_candidates(device_name):
        context = devices.get((controller_name, candidate))
        if context:
            return context
    devices_by_location = trace_index.get("devices_by_location", {})
    for enclosure_id in slot_enclosure_ids or []:
        enclosure_key = _identifier_lookup_key(enclosure_id)
        if not enclosure_key:
            continue
        for location_number in slot_location_numbers or []:
            context = devices_by_location.get((controller_name, enclosure_key, location_number))
            if context:
                return context
    return None


def _diagnostics_for_mpr_device(
    diagnostics: dict[str, Any],
    controller_name: str,
    device_context: dict[str, Any],
) -> dict[str, Any]:
    target = normalize_text(device_context.get("target"))
    if not target:
        return {}
    return dict((diagnostics.get("by_controller_target") or {}).get(f"{controller_name}:{target}") or {})


def _diagnostics_for_member_device(diagnostics: dict[str, Any], member_device_name: str | None) -> dict[str, Any]:
    by_device = diagnostics.get("by_device") or {}
    for candidate in _device_name_candidates(member_device_name):
        summary = by_device.get(candidate)
        if summary:
            return dict(summary)
    return {}


def _diagnostic_metric_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    return {
        "event_count": summary.get("event_count", 0),
        "error_count": summary.get("error_count", 0),
        "retry_count": summary.get("retry_count", 0),
        "sense_count": summary.get("sense_count", 0),
        "ioc_terminated_count": summary.get("ioc_terminated_count", 0),
        "devices": summary.get("devices") or [],
        "targets": summary.get("targets") or [],
        "sense_counts": summary.get("sense_counts") or {},
        "loginfo_counts": summary.get("loginfo_counts") or {},
        "fault_family_counts": summary.get("fault_family_counts") or {},
        "operation_counts": summary.get("operation_counts") or {},
        "top_findings": summary.get("top_findings") or [],
        "primary_fault": summary.get("primary_fault"),
        "operator_summary": summary.get("operator_summary"),
        "recent_events": summary.get("recent_events") or [],
        "decoded_records": summary.get("decoded_records") or [],
        "event_table": summary.get("event_table")
        or {"schema_version": 1, "total_count": 0, "page_size": 25, "rows": []},
    }


def _slot_enclosure_candidates(slot: SlotView) -> list[str]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    return _dedupe_strings(
        [
            raw_status.get("enclosure_id"),
            slot.enclosure_id,
            slot.enclosure_name,
            raw_status.get("enclosure_name"),
        ]
    )


def _slot_location_number_candidates(slot: SlotView) -> list[int]:
    raw_status = slot.raw_status if isinstance(slot.raw_status, dict) else {}
    candidates = [slot.slot]
    for value in (
        raw_status.get("ses_slot_number"),
        raw_status.get("slot_number"),
        raw_status.get("element_index"),
        slot.ssh_ses_element_id,
    ):
        parsed = _parse_mpr_slot_number(value)
        if parsed is None:
            continue
        candidates.append(parsed)
        if parsed > 0:
            candidates.append(parsed - 1)
    return _dedupe_ints(candidates)


def _device_name_candidates(*values: Any) -> list[str]:
    candidates: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        cleaned = normalize_text(text)
        if cleaned.startswith("/dev/") and _looks_like_disk_device(cleaned[5:]):
            candidates.append(cleaned[5:])
        if _looks_like_disk_device(cleaned):
            candidates.append(cleaned)
        for token in re.split(r"[^A-Za-z0-9_.-]+", text):
            token_cleaned = normalize_text(token)
            if _looks_like_disk_device(token_cleaned):
                candidates.append(token_cleaned)
    return _dedupe_strings(candidates)


def _looks_like_disk_device(value: str | None) -> bool:
    cleaned = normalize_text(value)
    return bool(re.match(r"^(?:/dev/)?(?:da|ada|pass|mfid|nvd|nvme|sd|hd|xbd)\d", cleaned))


def _is_mpr_disk_target(value: str | None) -> bool:
    cleaned = (normalize_text(value) or "").lower()
    return "sas target" in cleaned or "sata target" in cleaned


def _identifier_lookup_key(value: Any) -> str | None:
    cleaned = normalize_text(str(value)) if value is not None else None
    if not cleaned:
        return None
    cleaned = cleaned.lower().removeprefix("0x")
    return cleaned or None


def _parse_mpr_slot_number(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    try:
        return int(match.group(0), 10)
    except ValueError:
        return None


def _mpr_evidence(unit: int, subcommand: str) -> list[str]:
    return [f"mprutil -u {unit} show {subcommand}"] if unit >= 0 else [f"mprutil show {subcommand}"]


def _add_mpr_infrastructure(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    controllers: list[dict[str, Any]],
    unit_data: dict[int, dict[str, Any]],
    selected_enclosure_keys: set[str] | None = None,
) -> None:
    controller_ids_by_unit = {
        item["unit"]: item["id"]
        for item in controllers
        if item.get("unit") is not None
    }
    selected_keys = selected_enclosure_keys or set()
    for unit, payload in sorted(unit_data.items()):
        controller_id = controller_ids_by_unit.get(unit, f"controller:mpr{unit}" if unit >= 0 else "controller:mpr")
        enclosure_counts = Counter(str(device.get("enclosure_handle") or "") for device in payload.get("devices") or [])
        selected_enc_handles = {
            str(enclosure.get("enc_handle") or "")
            for enclosure in payload.get("enclosures") or []
            if _identifier_lookup_key(enclosure.get("logical_id")) in selected_keys
        }
        selected_enc_handles.discard("")
        expander_ids_by_enc_handle: dict[str, list[str]] = defaultdict(list)
        for expander in payload.get("expanders") or []:
            expander_id = f"expander:{controller_id}:{expander.get('sas_address') or expander.get('dev_handle')}"
            expander_key = _identifier_lookup_key(expander.get("sas_address"))
            enc_handle = str(expander.get("enc_handle") or "")
            selected_candidate = bool(
                selected_keys
                and (expander_key in selected_keys or enc_handle in selected_enc_handles)
            )
            raw = {
                "id": expander_id,
                "controller_id": controller_id,
                "unit": unit,
                "selected_enclosure_candidate": selected_candidate,
                "fabric_scope": "selected_enclosure" if selected_candidate else "unscoped",
                **expander,
            }
            add_node(
                nodes,
                SasFabricNode(
                    id=expander_id,
                    kind="expander",
                    label=str(expander.get("sas_address") or expander.get("dev_handle") or "Expander"),
                    raw_id=str(expander.get("sas_address") or expander.get("dev_handle") or ""),
                    controller_id=controller_id,
                    metrics={
                        "linked_phys": expander.get("linked_phys"),
                        "num_phys": expander.get("num_phys"),
                        "sas_level": expander.get("sas_level"),
                    },
                    evidence=[f"mprutil -u {unit} show expanders"] if unit >= 0 else ["mprutil show expanders"],
                    raw=raw,
                ),
            )
            add_link(links, controller_id, expander_id, "controller-expander")
            if enc_handle:
                expander_ids_by_enc_handle[enc_handle].append(expander_id)

        for enclosure in payload.get("enclosures") or []:
            enc_handle = str(enclosure.get("enc_handle") or "")
            enclosure_id = f"mpr-enclosure:{controller_id}:{enc_handle or enclosure.get('logical_id')}"
            enclosure_key = _identifier_lookup_key(enclosure.get("logical_id"))
            selected_candidate = bool(
                selected_keys
                and (enclosure_key in selected_keys or enc_handle in selected_enc_handles)
            )
            raw = {
                "id": enclosure_id,
                "controller_id": controller_id,
                "unit": unit,
                "device_count": int(enclosure_counts.get(enc_handle, 0)),
                "selected_enclosure_candidate": selected_candidate,
                "fabric_scope": "selected_enclosure" if selected_candidate else "unscoped",
                **enclosure,
            }
            add_node(
                nodes,
                SasFabricNode(
                    id=enclosure_id,
                    kind="mpr-enclosure",
                    label=str(enclosure.get("logical_id") or enc_handle or "MPR enclosure"),
                    raw_id=str(enclosure.get("logical_id") or enc_handle or ""),
                    controller_id=controller_id,
                    metrics={"slots": enclosure.get("slots"), "device_count": raw["device_count"]},
                    evidence=[f"mprutil -u {unit} show enclosures"] if unit >= 0 else ["mprutil show enclosures"],
                    raw=raw,
                ),
            )
            for expander_id in expander_ids_by_enc_handle.get(enc_handle, []):
                add_link(links, expander_id, enclosure_id, "expander-enclosure")


def _scope_mpr_infrastructure(
    *,
    nodes: dict[str, SasFabricNode],
    links: dict[str, SasFabricLink],
    traces: dict[str, SasFabricTrace],
    snapshot: InventorySnapshot,
) -> None:
    selected_bay_slots = _snapshot_slot_numbers(snapshot.slots)
    selected_disk_slots = _snapshot_disk_slot_numbers(snapshot.slots)
    scoped_node_ids: set[str] = set()
    controller_slot_updates: dict[str, list[int]] = defaultdict(list)

    for node in list(nodes.values()):
        if node.kind not in {"expander", "mpr-enclosure"}:
            continue
        if not node.raw.get("selected_enclosure_candidate"):
            continue
        scoped_node_ids.add(node.id)
        if not node.related_slots and selected_bay_slots:
            node.related_slots = list(selected_bay_slots)
        node.metrics.update(
            {
                "selected_enclosure": True,
                "selected_bay_count": len(selected_bay_slots),
                "selected_disk_count": len(selected_disk_slots),
                "selected_disk_slots": selected_disk_slots,
            }
        )
        node.raw.update(
            {
                "selected_enclosure_candidate": True,
                "selected_bay_count": len(selected_bay_slots),
                "selected_disk_count": len(selected_disk_slots),
                "selected_disk_slots": selected_disk_slots,
            }
        )
        if node.controller_id:
            controller_slot_updates[node.controller_id].extend(node.related_slots)

    for controller_id, slots in controller_slot_updates.items():
        controller = nodes.get(controller_id)
        if not controller:
            continue
        controller.related_slots = _dedupe_ints([*controller.related_slots, *slots])
        controller.metrics["selected_enclosure"] = True
        controller.metrics["selected_bay_count"] = len(selected_bay_slots)
        controller.metrics["selected_disk_count"] = len(selected_disk_slots)

    for link in links.values():
        source = nodes.get(link.source)
        target = nodes.get(link.target)
        related_slots = _dedupe_ints([*(source.related_slots if source else []), *(target.related_slots if target else [])])
        if not link.related_slots and (link.source in scoped_node_ids or link.target in scoped_node_ids):
            link.related_slots = related_slots or list(selected_bay_slots)
        if link.kind == "host-controller" and link.target in controller_slot_updates:
            link.related_slots = _dedupe_ints([*link.related_slots, *controller_slot_updates[link.target]])

    drop_ids = {
        node.id
        for node in nodes.values()
        if node.kind in {"expander", "mpr-enclosure"}
        and not node.related_slots
        and not node.raw.get("selected_enclosure_candidate")
    }
    for node_id in drop_ids:
        nodes.pop(node_id, None)
    for link_id, link in list(links.items()):
        if link.source in drop_ids or link.target in drop_ids:
            links.pop(link_id, None)

    scoped_controller_ids = {
        node.controller_id
        for node_id in scoped_node_ids
        for node in [nodes.get(node_id)]
        if node and node.controller_id
    }
    scoped_link_ids = [
        link.id
        for link in links.values()
        if link.source in scoped_node_ids
        or link.target in scoped_node_ids
        or (link.kind == "host-controller" and link.target in scoped_controller_ids)
    ]
    scoped_trace_node_ids = _dedupe_strings(["host", *scoped_controller_ids, *scoped_node_ids])
    for trace in traces.values():
        if trace.kind != "bay" or not slots_overlap(trace.slots, selected_bay_slots):
            continue
        if any(node_id.startswith(("expander:", "mpr-enclosure:")) for node_id in trace.node_ids):
            continue
        trace.node_ids = _dedupe_strings([*trace.node_ids, *scoped_trace_node_ids])
        trace.link_ids = _dedupe_strings([*trace.link_ids, *scoped_link_ids])
        trace.evidence = _dedupe_strings([*trace.evidence, "selected enclosure scope"])
        trace.metrics["selected_enclosure_scope"] = True

    for trace in traces.values():
        trace.node_ids = [node_id for node_id in trace.node_ids if node_id in nodes]
        trace.link_ids = [link_id for link_id in trace.link_ids if link_id in links]


def slots_overlap(left: list[int], right: list[int]) -> bool:
    if not left or not right:
        return False
    return bool(set(left).intersection(right))


def _selected_enclosure_keys(snapshot: InventorySnapshot) -> set[str]:
    keys: set[str] = set()
    for value in (snapshot.selected_enclosure_id,):
        if not value:
            continue
        for part in re.split(r"[+,\s]+", str(value)):
            key = _identifier_lookup_key(part)
            if key and re.fullmatch(r"[0-9a-f]{8,}", key):
                keys.add(key)
    return keys


def _snapshot_slot_numbers(slots: list[SlotView]) -> list[int]:
    return _dedupe_ints(slot.slot for slot in slots if slot.slot is not None)


def _snapshot_disk_slot_numbers(slots: list[SlotView]) -> list[int]:
    return _dedupe_ints(slot.slot for slot in slots if slot.slot is not None and _slot_has_disk(slot))


def _object_id_token(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if text.startswith("/dev/"):
        text = text.removeprefix("/dev/")
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-").lower()
    return token or "unknown"


def _linux_ses_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", normalize_text(value))
    return (int(match.group(1)) if match else 1_000_000, normalize_text(value))


def _slot_has_disk(slot: SlotView) -> bool:
    if slot.device_name or slot.serial or slot.model or slot.pool_name or slot.vdev_name:
        return True
    if slot.multipath and (slot.multipath.device_name or slot.multipath.members):
        return True
    return False


def _backplane_zones_for_slots(slots: list[SlotView]) -> dict[int, dict[str, Any]]:
    slot_numbers = _snapshot_slot_numbers(slots)
    if not slot_numbers:
        return {}
    slot_count = max(slot_numbers) + 1
    zone_count = 4 if slot_count >= 4 else 1
    zone_size = max(1, (slot_count + zone_count - 1) // zone_count)
    zones_by_slot: dict[int, dict[str, Any]] = {}
    for zone_index in range(zone_count):
        start = zone_index * zone_size
        end = min(slot_count - 1, start + zone_size - 1)
        zone_slots = [slot for slot in slot_numbers if start <= slot <= end]
        if not zone_slots:
            continue
        zone = {
            "id": f"backplane:{zone_index}",
            "index": zone_index,
            "label": f"Backplane Zone {zone_index + 1}",
            "slots": zone_slots,
            "range": f"Bays {zone_slots[0]:02d}-{zone_slots[-1]:02d}",
        }
        for slot in zone_slots:
            zones_by_slot[slot] = zone
    return zones_by_slot


def _sas_fabric_alias_map(aliases: list[SasFabricAlias]) -> dict[str, SasFabricAlias]:
    return {alias.object_id: alias for alias in aliases if alias.object_id and alias.label}


def _apply_sas_fabric_aliases(
    *,
    nodes: dict[str, SasFabricNode],
    traces: dict[str, SasFabricTrace],
    controllers: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    aliases: dict[str, SasFabricAlias],
) -> None:
    for node in nodes.values():
        alias = aliases.get(node.id)
        if not alias:
            continue
        node.alias = alias.label
        node.display_label = alias.label
        node.raw["operator_alias"] = alias.label

    for trace in traces.values():
        alias = aliases.get(trace.id)
        if not alias:
            continue
        trace.alias = alias.label
        trace.display_label = alias.label
        trace.metrics["operator_alias"] = alias.label

    for controller in controllers:
        alias = aliases.get(str(controller.get("id") or ""))
        if alias:
            controller["alias"] = alias.label
            controller["display_label"] = alias.label

    for path in paths:
        alias = aliases.get(str(path.get("id") or ""))
        if alias:
            path["alias"] = alias.label
            path["display_label"] = alias.label


def _node_raw_for_payload(node: SasFabricNode) -> dict[str, Any]:
    raw = dict(node.raw or {})
    raw.setdefault("id", node.id)
    raw.setdefault("kind", node.kind)
    raw.setdefault("label", node.label)
    raw.setdefault("display_label", node.display_label)
    raw.setdefault("alias", node.alias)
    raw.setdefault("raw_id", node.raw_id)
    raw["related_slots"] = list(node.related_slots)
    raw["status"] = node.status
    return raw


def _controller_status(counts: dict[str, int]) -> str | None:
    if counts.get("fail"):
        return "degraded"
    if counts.get("active") or counts.get("passive"):
        return "online"
    return None


def _mpr_unit_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\bmpr(?P<unit>\d+)\b", value)
    return match.group("unit") if match else None


def _path_state_sort_key(state: str) -> int:
    return {"fail": 0, "missing": 1, "active": 2, "passive": 3, "unknown": 4}.get(state, 10)


def _link_id(source: str, target: str, kind: str, slot: int | None = None) -> str:
    suffix = f":{slot}" if slot is not None else ""
    return f"{kind}:{source}->{target}{suffix}"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def _dedupe_ints(values: Any) -> list[int]:
    seen: set[int] = set()
    deduped: list[int] = []
    for value in values:
        try:
            integer = int(value)
        except (TypeError, ValueError):
            continue
        if integer in seen:
            continue
        seen.add(integer)
        deduped.append(integer)
    return sorted(deduped)
