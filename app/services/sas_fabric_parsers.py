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


def _skip_whitespace(line: str, index: int) -> int:
    while index < len(line) and line[index].isspace():
        index += 1
    return index


def _skip_rate_characters(line: str, index: int) -> int:
    while index < len(line) and (line[index] == "." or line[index].isdecimal()):
        index += 1
    return index


def _mpr_phy_rate_end(line: str, index: int) -> int | None:
    """End of a `[\\d.]+` or `???` field at `index` that whitespace follows."""

    end = _skip_rate_characters(line, index)
    if end == index and line.startswith("???", index):
        end = index + 3
    return end if index < end < len(line) and line[end].isspace() else None


def _mpr_phy_max_and_device(line: str, index: int) -> tuple[str, str] | None:
    """`<max> <device>` from `index`; the device is the rest of the line."""

    max_end = _mpr_phy_rate_end(line, index)
    # At least one separator and one more character, which may be whitespace.
    if max_end is None or len(line) - max_end < 2:
        return None
    return line[index:max_end], line[max_end:].strip()


def _mpr_phy_min_max_and_device(line: str, index: int) -> tuple[str, str, str] | None:
    min_end = _mpr_phy_rate_end(line, index)
    if min_end is None:
        return None
    rest = _mpr_phy_max_and_device(line, _skip_whitespace(line, min_end))
    return None if rest is None else (line[index:min_end], *rest)


def _match_mpr_expander_phy_line(line: str) -> dict[str, str | None] | None:
    """Read one `mprutil show expanders` PHY row with work linear in the line.

    Same result as the pattern
    `^\\s*(\\d+)\\s+(?:(\\d+)\\s+([0-9a-fA-F]{4})\\s+([\\d.]+)|)\\s*([\\d.]+|\\?\\?\\?)\\s+([\\d.]+|\\?\\?\\?)\\s+(.+?)\\s*$`
    with the device stripped (#1216). That pattern retried overlapping
    whitespace and digit runs from every position. Each field here is scanned
    once: the full row (PHY, remote PHY, handle, speed, min, max, device) is
    tried first, then the short row (PHY, min, max, device).
    """

    phy_start = _skip_whitespace(line, 0)
    phy_end = _skip_rate_characters(line, phy_start)
    if phy_end == phy_start or "." in line[phy_start:phy_end] or phy_end == len(line) or not line[phy_end].isspace():
        return None
    fields_start = _skip_whitespace(line, phy_end)
    row: dict[str, str | None] = {
        "phy": line[phy_start:phy_end],
        "remote_phy": None,
        "dev_handle": None,
        "speed": None,
        "min": None,
        "max": None,
        "device": None,
    }

    remote_end = _skip_rate_characters(line, fields_start)
    handle_start = _skip_whitespace(line, remote_end)
    handle_end = handle_start + 4
    speed_start = _skip_whitespace(line, handle_end)
    speed_end = _skip_rate_characters(line, speed_start)
    if (
        remote_end > fields_start
        and "." not in line[fields_start:remote_end]
        and handle_start > remote_end
        and handle_end < len(line)
        and not line[handle_start:handle_end].strip(_HEX_DIGITS)
        and len(line[handle_start:handle_end]) == 4
        and speed_start > handle_end
        and speed_end > speed_start
    ):
        rates_start = _skip_whitespace(line, speed_end)
        full = _mpr_phy_min_max_and_device(line, rates_start)
        speed = line[speed_start:speed_end]
        if full is None and speed_end - speed_start >= 2 and rates_start > speed_end:
            # The pattern's backtracking: the last speed character becomes min.
            rest = _mpr_phy_max_and_device(line, rates_start)
            if rest is not None:
                full = (speed[-1], *rest)
                speed = speed[:-1]
        if full is not None:
            row.update(
                remote_phy=line[fields_start:remote_end],
                dev_handle=line[handle_start:handle_end],
                speed=speed,
            )
            row["min"], row["max"], row["device"] = full
            return row

    short = _mpr_phy_min_max_and_device(line, fields_start)
    if short is None:
        return None
    row["min"], row["max"], row["device"] = short
    return row


def parse_mpr_expanders(text: str) -> list[dict[str, Any]]:
    expanders: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    header_pattern = re.compile(
        r"^\s*(\d+)\s+([0-9a-fA-F]{16})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+([0-9a-fA-F]{4})\s+(\d+)\s*$"
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
        phy = _match_mpr_expander_phy_line(line)
        if not phy:
            continue
        if str(phy["device"]).lower().startswith("phy "):
            continue
        current["phys"].append(phy)
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


# `key=value` attributes. A match can only start where a key run starts: the
# same matches as without the guard, because a run that fails at its start
# fails at every later position too, but a long run without "=" is read once
# instead of once per character (#1216).
_PCICONF_ATTRIBUTE_PATTERN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]+)=([^\s]+)")
_SYSCTL_LOCATION_ATTRIBUTE_PATTERN = re.compile(r"(?<![a-zA-Z_])([a-zA-Z_]+)=([^\s]+)")


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
            for key, value in _PCICONF_ATTRIBUTE_PATTERN.findall(header.group("attrs")):
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
            for key, value in _SYSCTL_LOCATION_ATTRIBUTE_PATTERN.findall(location_match.group("value")):
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


_HEX_DIGITS = "0123456789abcdefABCDEF"
MPR_SENSE_PREFIX = "SCSI sense:"
# From "SCSI sense:" to the "(" that opens the reason. Kernel sense lines are a
# sense key of a few words and a two-number ASC, so this is generous.
MAX_MPR_SENSE_WINDOW_CHARS = 256
_MPR_SENSE_PATTERN = re.compile(
    r"SCSI sense:\s*(?P<sense>\S.*?)\s+asc:(?P<asc>[0-9a-fA-F]+,[0-9a-fA-F]+)\s+\("
)


def _match_mpr_controller_line(line: str) -> dict[str, str | None] | None:
    """Split `mprN: <message>[ tgt N SMID N loginfo X]` on its literal fields.

    Same result as the pattern
    `^(mpr\\d+):\\s+(.+?)(?:\\s+tgt\\s+(\\d+)\\s+SMID\\s+(\\d+)\\s+loginfo\\s+([0-9a-fA-F]+))?$`
    on a single line, with work linear in the line (#1216). The optional fields
    contain no other "tgt", so only the last "tgt" can start them.
    """

    colon = line.find(":")
    unit = line[3:colon] if colon > 3 and line.startswith("mpr") else ""
    if not unit.isdecimal():
        return None
    rest = line[colon + 1 :]
    message = rest.lstrip()
    if len(message) == len(rest) or (not message and len(rest) < 2):
        return None
    fields: dict[str, str | None] = {
        "controller": line[:colon],
        "message": message,
        "target": None,
        "smid": None,
        "loginfo": None,
    }
    tgt = message.rfind("tgt")
    if tgt <= 0 or not message[tgt - 1].isspace() or message[-1].isspace():
        return fields
    tokens = message[tgt:].split()
    if (
        len(tokens) == 6
        and tokens[0] == "tgt"
        and tokens[1].isdecimal()
        and tokens[2] == "SMID"
        and tokens[3].isdecimal()
        and tokens[4] == "loginfo"
        and tokens[5]
        and not tokens[5].strip(_HEX_DIGITS)
    ):
        fields.update(message=message[:tgt].rstrip(), target=tokens[1], smid=tokens[3], loginfo=tokens[5])
    return fields


def parse_mpr_dmesg_events(text: str) -> dict[str, Any]:
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
            controller_match = _match_mpr_controller_line(source_line)
            if controller_match:
                message = str(controller_match["message"]).strip()
                target = _bounded_numeric_token(controller_match["target"])
                event = {
                    "source": "controller",
                    "controller": controller_match["controller"],
                    "target": target,
                    "smid": controller_match["smid"],
                    "loginfo": controller_match["loginfo"],
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
    """Parse `SCSI sense: <key> asc:<a>,<q> (<reason>)` with bounded regex work.

    The regex sees only a short window after the first "SCSI sense:" (#1216).
    A later occurrence can only match a subset of the same text, so it is not
    tried. A sense line whose "(" is outside the window, or whose sense key is
    empty, is not decoded. The reason runs to the last ")" on the line.
    """

    start = message.find(MPR_SENSE_PREFIX)
    if start < 0:
        return {}
    match = _MPR_SENSE_PATTERN.match(message, start, start + MAX_MPR_SENSE_WINDOW_CHARS)
    if not match:
        return {}
    reason_end = message.rfind(")")
    if reason_end <= match.end():
        return {}
    return {
        "sense": match.group("sense").strip(),
        "sense_key": match.group("sense").strip().upper(),
        "asc": match.group("asc").lower(),
        "reason": message[match.end() : reason_end].strip(),
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
    match = re.search(r"\bmpr(?P<unit>\d+)\b", value)
    return match.group("unit") if match else None
