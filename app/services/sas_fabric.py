from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

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


def build_sas_fabric_snapshot(
    *,
    system: SystemConfig,
    snapshot: InventorySnapshot,
    ssh_outputs: dict[str, str],
    sources: dict[str, SourceStatus] | None = None,
    warnings: list[str] | None = None,
    aliases: list[SasFabricAlias] | None = None,
) -> SasFabricSnapshot:
    normalized_outputs = _canonical_outputs(ssh_outputs)
    fabric_warnings = list(warnings or [])
    alias_map = _sas_fabric_alias_map(aliases or [])
    selected_enclosure_keys = _selected_enclosure_keys(snapshot)
    if system.truenas.platform != "core":
        fabric_warnings.append("SAS Fabric topology is currently implemented for TrueNAS CORE source data only.")
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
        )

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
        aliases=alias_map,
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
        aliases=list(alias_map.values()),
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
        candidates: list[str] = []
        if slot.ssh_ses_device:
            candidates.append(slot.ssh_ses_device)
        for target in slot.ssh_ses_targets:
            ses_device = target.get("ses_device") if isinstance(target, dict) else None
            if isinstance(ses_device, str) and ses_device:
                candidates.append(ses_device)
        if candidates:
            devices[slot.slot] = _dedupe_strings(candidates)
    return devices


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
