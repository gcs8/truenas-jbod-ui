from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from app.services.parsers import canonicalize_ssh_command, normalize_text
from app.services.sas_diagnostics import (
    finalize_mpr_event_summary,
    make_decoded_event_record,
    new_mpr_event_summary,
    record_mpr_event_summary,
)
from app.services.sas_diagnostics.decoder import MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH, bound_diagnostic_value


CORE_MPRUTIL_UNIT_SUBCOMMANDS = ("adapter", "devices", "enclosures", "expanders", "iocfacts")


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
    malformed_phy_rows = 0
    discovering_phys: list[int] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Discovery still in progress":
            # mprutil prints this in place of the row of a PHY still being
            # discovered and goes on; it holds that PHY's place, adds no row.
            discovering_phys.append(len(detail["phy_rows"]) + len(discovering_phys))
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
            unattached = _MPR_UNATTACHED_ADAPTER_PHY.match(stripped)
            if unattached:
                # mprutil leaves both handles and the speed blank for a PHY
                # with nothing attached; the row is still part of the table.
                detail["phy_rows"].append(
                    {
                        "phy": parts[0],
                        "controller_handle": None,
                        "device_handle": None,
                        "disabled": parts[1],
                        "speed": None,
                        "min": parts[2],
                        "max": parts[3],
                        "device": " ".join(parts[4:]),
                    }
                )
            elif len(parts) >= 8:
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
            else:
                malformed_phy_rows += 1
    rows = detail["phy_rows"]
    # Counts come only from a PHY table that was read whole. No table, a
    # malformed row, a repeated PHY number or a gap in the PHY numbers
    # (0 to n-1, as mprutil lists them) leaves both counts unknown. A PHY
    # still in discovery fills only its own position; printed rows are counted.
    phy_numbers = [int(row["phy"]) for row in rows] + discovering_phys
    table_size = len(phy_numbers)
    if (
        table_size
        and not malformed_phy_rows
        and len(set(phy_numbers)) == len(phy_numbers)
        and all(number < table_size for number in phy_numbers)
    ):
        detail["phy_count"] = len(rows)
        detail["linked_phy_count"] = sum(
            1 for row in rows if row.get("device_handle") and row.get("device") and row.get("device") != "No Device"
        )
        detail["phy_table"] = "read"
    else:
        detail["phy_count"] = None
        detail["linked_phy_count"] = None
        # No table at all is "absent" (as before); rows that were seen but
        # did not form a whole table are "rejected": not read, not zero.
        detail["phy_table"] = "rejected" if rows or malformed_phy_rows else "absent"
    return detail


_MPR_PHY_SPEED = r"(?:[\d.]+|\?\?\?|Unk)"
_MPR_UNATTACHED_ADAPTER_PHY = re.compile(rf"^\d+\s+[YN]\s+{_MPR_PHY_SPEED}\s+{_MPR_PHY_SPEED}\s+\S.*$")


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


def parse_mpr_enclosures(text: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r"^\d+\s+[0-9a-fA-F]{8,}", stripped):
            continue
        parts = stripped.split()
        # mprutil prints the SEP handle as blanks for an enclosure that is not
        # an SES device; only those printed columns make a one-handle row.
        sep_handle = parts[2] if len(parts) >= 5 and re.fullmatch(r"[0-9a-fA-F]{4}", parts[3]) else None
        handles = 2 if sep_handle else 1
        if not sep_handle and not _MPR_ENCLOSURE_BLANK_SEP.match(line):
            continue
        rows.append(
            {
                "slots": parts[0],
                "logical_id": parts[1],
                "sep_handle": sep_handle,
                "enc_handle": parts[1 + handles],
                "type": " ".join(parts[2 + handles:]),
            }
        )
    return rows


# "  %.2d    %08x%08x    %s       %04x     %s" with a four-blank SEP column.
_MPR_ENCLOSURE_BLANK_SEP = re.compile(r"^\s*\d+ {4}[0-9a-fA-F]{16} {15}[0-9a-fA-F]{4} {5}\S")


def parse_mpr_expanders(text: str) -> list[dict[str, Any]]:
    expanders: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    phy_pattern = re.compile(
        r"^\s*(\d+)\s+(?:(\d+)\s+([0-9a-fA-F]{4})\s+([\d.]+)|)\s*([\d.]+|\?\?\?)\s+([\d.]+|\?\?\?)\s+(.+?)\s*$"
    )
    for line in text.splitlines():
        header = _match_mpr_expander_header(line)
        if header:
            current = {
                "num_phys": header.group(1),
                "sas_address": header.group(2),
                "dev_handle": header.group(3),
                "parent": header.group(4).strip() or None,
                "enc_handle": header.group(5).strip() or None,
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
    candidate_lines = _mpr_expander_phy_candidate_lines(text)
    for expander, candidates in zip(expanders, candidate_lines):
        # Counts come only from a PHY table read whole: one row per reported
        # PHY, each PHY number unique and in range, and no rejected rows.
        # Otherwise the linked count and device tally are unknown, not 0.
        if _mpr_expander_phy_table_complete(expander, candidates):
            counts = Counter(str(row.get("device") or "unknown").strip() for row in expander["phys"])
            expander["device_counts"] = dict(counts)
            expander["linked_phys"] = sum(1 for row in expander["phys"] if row.get("device") != "No Device")
        else:
            expander["device_counts"] = None
            expander["linked_phys"] = None
    return expanders


_MPR_EXPANDER_HEADER = re.compile(
    r"^\s*(\d+)\s+([0-9a-fA-F]{16})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+(\d+)\s*$"
)
# mprutil prints a zero parent or enclosure handle as four blanks in its fixed
# columns ("%04x       %s     %s       %d"), so those headers are read by position.
_MPR_EXPANDER_HEADER_BLANK_HANDLES = re.compile(
    r"^\s*(\d+)\s+([0-9a-fA-F]{16})\s+([0-9a-fA-F]{4}) {7}([0-9a-fA-F]{4}| {4}) {5}([0-9a-fA-F]{4}| {4}) {7}(\d+)\s*$"
)


def _match_mpr_expander_header(line: str) -> re.Match[str] | None:
    return _MPR_EXPANDER_HEADER.match(line) or _MPR_EXPANDER_HEADER_BLANK_HANDLES.match(line)


def _mpr_expander_phy_candidate_lines(text: str) -> list[int]:
    """Per expander header, the non-blank lines under it other than the column heading."""
    counts: list[int] = []
    for line in text.splitlines():
        if _match_mpr_expander_header(line):
            counts.append(0)
            continue
        stripped = line.strip()
        if counts and stripped and not stripped.lower().startswith("phy "):
            counts[-1] += 1
    return counts


def _mpr_expander_phy_table_complete(expander: dict[str, Any], candidate_lines: int) -> bool:
    rows = expander.get("phys") or []
    reported = str(expander.get("num_phys") or "")
    # Zero rows are whole only under a header that reports zero PHYs.
    if not reported.isdecimal() or len(rows) != int(reported) or candidate_lines != len(rows):
        return False
    numbers = [str(row.get("phy") or "") for row in rows]
    if not all(number.isdecimal() and int(number) < int(reported) for number in numbers):
        return False
    return len({int(number) for number in numbers}) == len(numbers)


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
            target = _bounded_numeric_token(disk_match.group("target"))
            event = {
                "source": "cam",
                "controller": disk_match.group("controller"),
                "device": disk_match.group("device"),
                "bus": disk_match.group("bus"),
                "target": target,
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
                target = _bounded_numeric_token(controller_match.group("target"))
                event = {
                    "source": "controller",
                    "controller": controller_match.group("controller"),
                    "target": target,
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
        if event["event_type"] == "scsi_status":
            event["severity"] = decoded_record["severity"]
        recent_events.append(bound_diagnostic_value(event))
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


def _bounded_numeric_token(value: str | None) -> str | None:
    if value is None or len(value) <= MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH:
        return value
    return f"{value[: MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH - 3]}..."


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


def admit_mpr_unit_number(token: str | None) -> int | None:
    """A controller unit number is 1 to 4 ASCII digits; anything else is rejected."""
    return int(token) if token is not None and _MPR_UNIT_NUMBER.fullmatch(token) else None


def rejected_mpr_summary_unit_tokens(text: str) -> set[str]:
    """Unit tokens of adapter summary rows naming an mpr device whose unit number was not admitted."""
    return {
        row["device"].removeprefix("/dev/mpr")
        for row in parse_mpr_adapter_summary(text)
        if row["device"].startswith("/dev/mpr") and admit_mpr_unit_number(row.get("unit")) is None
    }


def discover_mpr_units_from_adapter_summary(text: str) -> list[int]:
    units: set[int] = set()
    for row in parse_mpr_adapter_summary(text):
        unit = admit_mpr_unit_number(row.get("unit"))
        if unit is not None:
            units.add(unit)
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


def _mpr_unit_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\bmpr(?P<unit>[0-9]{1,4})\b", value)
    return match.group("unit") if match else None


_MPR_UNIT_NUMBER = re.compile(r"[0-9]{1,4}")
