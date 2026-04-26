from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.profile_registry import (
    SCALE_SSG_FRONT_24_PROFILE_ID,
    SCALE_SSG_REAR_12_PROFILE_ID,
)


DEVICE_REGEX = re.compile(
    r"(?P<device>(?:/dev/)?(?:(?:da|ada|sd|nvd)\d+|nvme\d+(?:n\d+)?|multipath/disk[0-9A-Za-z-]+)(?:p\d+)?)",
    re.IGNORECASE,
)
GPTID_REGEX = re.compile(r"(?P<gptid>(?:/dev/)?gptid/[A-Za-z0-9\-_.]+)", re.IGNORECASE)
GUID_REGEX = re.compile(r"^[0-9]{16,}$")
SLOT_REGEX = re.compile(r"(?:slot|bay|element)\D{0,4}(?P<slot>\d{1,3})", re.IGNORECASE)
HEX_VALUE_REGEX = re.compile(r"^(?:0x)?(?P<value>[0-9a-fA-F]+)$")
HEX_IDENTIFIER_ERROR_SPLIT_REGEX = re.compile(r"(?i)error:")


@dataclass(slots=True)
class GlabelInfo:
    gptid_to_device: dict[str, str] = field(default_factory=dict)
    device_to_gptid: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ZpoolMember:
    pool_name: str
    vdev_class: str
    vdev_name: str | None
    topology_label: str | None
    health: str | None
    raw_name: str
    raw_path: str | None


@dataclass(slots=True)
class ParsedSSHData:
    glabel: GlabelInfo = field(default_factory=GlabelInfo)
    zpool_members: dict[str, ZpoolMember] = field(default_factory=dict)
    multipath_info: dict[str, "MultipathInfo"] = field(default_factory=dict)
    ses_slot_to_device: dict[int, str] = field(default_factory=dict)
    camcontrol_models: dict[str, str] = field(default_factory=dict)
    camcontrol_controllers: dict[str, str] = field(default_factory=dict)
    camcontrol_peer_devices: dict[str, list[str]] = field(default_factory=dict)
    ses_slot_candidates: dict[int, dict[str, Any]] = field(default_factory=dict)
    ses_selected_meta: dict[str, str | None] = field(default_factory=dict)
    ses_enclosures: list["SESMapEnclosure"] = field(default_factory=list)
    linux_blockdevices: list[dict[str, Any]] = field(default_factory=list)
    linux_mdadm_arrays: dict[str, "LinuxMdArray"] = field(default_factory=dict)
    linux_nvme_subsystems: dict[str, dict[str, str | None]] = field(default_factory=dict)
    ubntstorage_disks: list[dict[str, Any]] = field(default_factory=list)
    ubntstorage_spaces: list[dict[str, Any]] = field(default_factory=list)
    unifi_led_states: dict[int, bool] = field(default_factory=dict)
    esxi_storage_adapters: list[dict[str, Any]] = field(default_factory=list)
    esxi_storage_devices: list[dict[str, Any]] = field(default_factory=list)
    esxi_filesystems: list[dict[str, Any]] = field(default_factory=list)
    esxi_vmfs_extents: list[dict[str, Any]] = field(default_factory=list)
    esxi_sas_adapters: list[dict[str, Any]] = field(default_factory=list)
    esxi_storcli_controller: dict[str, Any] = field(default_factory=dict)
    esxi_storcli_virtual_drives: list[dict[str, Any]] = field(default_factory=list)
    esxi_storcli_physical_drives: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class LinuxMdArray:
    device_path: str
    name: str | None = None
    uuid: str | None = None
    metadata: str | None = None


@dataclass(slots=True)
class SESMapSlot:
    slot_number: int
    element_id: int | None = None
    ses_device: str | None = None
    control_targets: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    description: str | None = None
    device_names: list[str] = field(default_factory=list)
    identify_active: bool = False
    serial: str | None = None
    model: str | None = None
    size_text: str | None = None
    present: bool | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    sas_device_type: str | None = None
    predicted_failure: bool | None = None
    disabled: bool | None = None
    hot_spare: bool | None = None
    do_not_remove: bool | None = None
    fault_sensed: bool | None = None
    fault_requested: bool | None = None


@dataclass(slots=True)
class SESMapEnclosure:
    ses_device: str | None = None
    enclosure_id: str | None = None
    enclosure_name: str | None = None
    enclosure_label: str | None = None
    profile_id: str | None = None
    layout_rows: int | None = None
    layout_columns: int | None = None
    slot_layout: list[list[int | None]] | None = None
    slots: dict[int, SESMapSlot] = field(default_factory=dict)


@dataclass(slots=True)
class MultipathConsumer:
    device_name: str
    state: str | None = None
    mode: str | None = None
    controller_label: str | None = None


@dataclass(slots=True)
class MultipathInfo:
    name: str
    device_name: str
    uuid: str | None = None
    mode: str | None = None
    state: str | None = None
    provider_state: str | None = None
    consumers: list[MultipathConsumer] = field(default_factory=list)


@dataclass(slots=True)
class CamcontrolInfo:
    models: dict[str, str] = field(default_factory=dict)
    controllers: dict[str, str] = field(default_factory=dict)
    peer_devices: dict[str, list[str]] = field(default_factory=dict)


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def normalize_device_name(value: str | None) -> str | None:
    if not value:
        return None
    match = DEVICE_REGEX.search(value)
    if not match:
        return value.strip().removeprefix("/dev/")
    normalized = match.group("device").removeprefix("/dev/")
    return re.sub(r"(p\d+)$", "", normalized, flags=re.IGNORECASE)


def normalize_gptid(value: str | None) -> str | None:
    if not value:
        return None
    match = GPTID_REGEX.search(value)
    if not match:
        return value.strip().removeprefix("/dev/")
    return match.group("gptid").removeprefix("/dev/")


def normalize_lookup_keys(value: str | None) -> set[str]:
    if not value:
        return set()

    stripped = value.strip().removeprefix("/dev/")
    keys = {stripped.lower()}

    device = normalize_device_name(value)
    if device:
        keys.add(device.lower())

    gptid = normalize_gptid(value)
    if gptid:
        keys.add(gptid.lower())

    return keys


def extract_nvme_controller_name(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(?P<controller>nvme\d+)(?:n\d+)?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    return match.group("controller").lower()


def normalize_hex_identifier(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = HEX_IDENTIFIER_ERROR_SPLIT_REGEX.split(value.strip(), maxsplit=1)[0].strip()
    if not cleaned:
        return None
    match = HEX_VALUE_REGEX.match(cleaned)
    if not match:
        return None
    normalized = match.group("value").lower().lstrip("0")
    return normalized or "0"


def shift_hex_identifier(value: str | None, delta: int) -> str | None:
    normalized = normalize_hex_identifier(value)
    if normalized is None:
        return None
    shifted = int(normalized, 16) + delta
    if shifted < 0:
        return None
    return f"{shifted:x}"


def format_hex_identifier(value: str | None) -> str | None:
    normalized = normalize_hex_identifier(value)
    if normalized is not None:
        return f"0x{normalized}"
    return None


def format_bytes(size_bytes: int | None) -> str | None:
    if size_bytes in {None, 0}:
        return None

    binary_text = _format_scaled_size(size_bytes, 1024, ["B", "KiB", "MiB", "GiB", "TiB", "PiB"])
    if size_bytes < 10**9:
        return binary_text

    decimal_text = _format_scaled_size(size_bytes, 1000, ["B", "KB", "MB", "GB", "TB", "PB"])
    if decimal_text == binary_text:
        return binary_text
    return f"{binary_text} ({decimal_text})"


def _format_scaled_size(size_bytes: int, base: int, suffixes: list[str]) -> str:
    size = float(size_bytes)
    for suffix in suffixes:
        if size < base or suffix == suffixes[-1]:
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= base
    return f"{int(size_bytes)} B"


def _coerce_non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _coerce_int_like(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = re.search(r"-?\d+", text.replace(",", ""))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _nvme_data_units_to_bytes(value: Any) -> int | None:
    units = _coerce_non_negative_int(value)
    if units is None:
        return None
    # NVMe SMART "data units" are reported in units of 1000 * 512 bytes.
    return units * 512_000


def _scsi_gigabytes_processed_to_bytes(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        try:
            return int(Decimal(str(value)) * Decimal(1_000_000_000))
        except (InvalidOperation, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(Decimal(text) * Decimal(1_000_000_000))
    except (InvalidOperation, ValueError):
        return None


def _annualize_bytes_written(
    bytes_written: int | None,
    power_on_hours: int | None,
    *,
    minimum_hours: int = 24 * 30,
) -> int | None:
    if bytes_written is None or not isinstance(power_on_hours, int) or power_on_hours < minimum_hours:
        return None
    return int(bytes_written * 24 * 365 / power_on_hours)


def _kelvin_to_celsius(value: Any) -> int | None:
    kelvin = _coerce_non_negative_int(value)
    if kelvin is None or kelvin == 0:
        return None
    return int(round(kelvin - 273.15))


def _format_nvme_version(value: Any) -> str | None:
    if not isinstance(value, int) or value < 0:
        return None
    major = (value >> 16) & 0xFFFF
    minor = (value >> 8) & 0xFF
    tertiary = value & 0xFF
    if tertiary:
        return f"{major}.{minor}.{tertiary}"
    return f"{major}.{minor}"


def _format_nvme_eui64(value: Any) -> str | None:
    text = normalize_text(str(value) if value is not None else None)
    if not text:
        return None
    lowered = text.lower()
    return lowered if lowered.startswith("eui.") else f"eui.{lowered}"


def _format_nvme_nguid(value: Any) -> str | None:
    text = normalize_text(str(value) if value is not None else None)
    return text.lower() if text else None


def _extract_ata_attribute_raw_value(payload: dict[str, Any], *attribute_ids: int) -> int | None:
    table = (
        payload.get("ata_smart_attributes", {}).get("table")
        if isinstance(payload.get("ata_smart_attributes"), dict)
        else None
    )
    if not isinstance(table, list):
        return None

    attribute_id_set = set(attribute_ids)
    for entry in table:
        if not isinstance(entry, dict) or entry.get("id") not in attribute_id_set:
            continue
        raw_value = entry.get("raw")
        if isinstance(raw_value, dict):
            for candidate in (raw_value.get("value"), raw_value.get("string")):
                parsed = _coerce_int_like(candidate)
                if parsed is not None and parsed >= 0:
                    return parsed
        parsed = _coerce_int_like(raw_value)
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _extract_ata_attribute_entry(payload: dict[str, Any], *attribute_ids: int) -> dict[str, Any] | None:
    table = (
        payload.get("ata_smart_attributes", {}).get("table")
        if isinstance(payload.get("ata_smart_attributes"), dict)
        else None
    )
    if not isinstance(table, list):
        return None

    attribute_id_set = set(attribute_ids)
    for entry in table:
        if isinstance(entry, dict) and entry.get("id") in attribute_id_set:
            return entry
    return None


def _ata_attribute_raw_value_to_bytes(entry: dict[str, Any], sector_size: int) -> int | None:
    raw_value = entry.get("raw")
    parsed_raw_value = None
    if isinstance(raw_value, dict):
        for candidate in (raw_value.get("value"), raw_value.get("string")):
            parsed_raw_value = _coerce_int_like(candidate)
            if parsed_raw_value is not None and parsed_raw_value >= 0:
                break
    else:
        parsed_raw_value = _coerce_int_like(raw_value)

    if parsed_raw_value is None or parsed_raw_value < 0:
        return None

    attribute_name = normalize_text(entry.get("name")) or ""
    name_match = re.search(r"_(\d+)(KiB|MiB|GiB|TiB)$", attribute_name)
    if name_match:
        unit_value = int(name_match.group(1))
        unit_name = name_match.group(2)
        unit_scale = {
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
            "TiB": 1024**4,
        }.get(unit_name)
        if unit_scale is not None:
            return parsed_raw_value * unit_value * unit_scale

    return parsed_raw_value * sector_size


def _extract_ata_device_stat_bytes(payload: dict[str, Any], stat_name: str, sector_size: int) -> int | None:
    ata_device_statistics = (
        payload.get("ata_device_statistics")
        if isinstance(payload.get("ata_device_statistics"), dict)
        else {}
    )
    pages = ata_device_statistics.get("pages") if isinstance(ata_device_statistics.get("pages"), list) else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        table = page.get("table") if isinstance(page.get("table"), list) else []
        for entry in table:
            if not isinstance(entry, dict):
                continue
            if normalize_text(entry.get("name")) != stat_name:
                continue
            value = _coerce_int_like(entry.get("value"))
            if value is not None and value >= 0:
                return value * sector_size
    return None


def _extract_ata_device_stat_int(payload: dict[str, Any], stat_name: str) -> int | None:
    ata_device_statistics = (
        payload.get("ata_device_statistics")
        if isinstance(payload.get("ata_device_statistics"), dict)
        else {}
    )
    pages = ata_device_statistics.get("pages") if isinstance(ata_device_statistics.get("pages"), list) else []
    for page in pages:
        if not isinstance(page, dict):
            continue
        table = page.get("table") if isinstance(page.get("table"), list) else []
        for entry in table:
            if not isinstance(entry, dict):
                continue
            if normalize_text(entry.get("name")) != stat_name:
                continue
            value = _coerce_int_like(entry.get("value"))
            if value is not None and value >= 0:
                return value
    return None


def parse_glabel_status(output: str) -> GlabelInfo:
    info = GlabelInfo()
    for line in output.splitlines():
        if "gptid/" not in line:
            continue

        gptid_match = GPTID_REGEX.search(line)
        device_match = DEVICE_REGEX.search(line)
        if not gptid_match or not device_match:
            continue

        gptid = gptid_match.group("gptid").removeprefix("/dev/")
        device = normalize_device_name(device_match.group("device"))
        if device:
            info.gptid_to_device[gptid.lower()] = device
            info.device_to_gptid[device.lower()] = gptid
    return info


def parse_camcontrol_devlist(output: str) -> CamcontrolInfo:
    info = CamcontrolInfo()
    current_controller: str | None = None
    grouped_devices: dict[tuple[str, str | None, str | None], list[str]] = {}

    for line in output.splitlines():
        bus_match = re.match(r"^(?:scbus|umass-sim)\d+\s+on\s+(?P<controller>\S+)\s+bus\s+\d+:", line.strip(), re.IGNORECASE)
        if bus_match:
            current_controller = normalize_text(bus_match.group("controller"))
            continue

        match = re.search(
            r"<(?P<model>[^>]+)>.*?target\s+(?P<target>\S+)\s+lun\s+(?P<lun>\S+)\s+\((?P<devices>[^)]+)\)",
            line,
        )
        if not match:
            continue

        model = match.group("model").strip()
        group_key = (
            model,
            normalize_text(match.group("target")),
            normalize_text(match.group("lun")),
        )
        parsed_devices: list[str] = []
        for device in match.group("devices").split(","):
            if not DEVICE_REGEX.search(device.strip()):
                continue
            normalized = normalize_device_name(device)
            if normalized:
                parsed_devices.append(normalized)
                info.models[normalized.lower()] = model
                if current_controller:
                    info.controllers[normalized.lower()] = current_controller
        if parsed_devices:
            grouped_devices.setdefault(group_key, []).extend(parsed_devices)

    for devices in grouped_devices.values():
        deduped = list(dict.fromkeys(devices))
        if len(deduped) < 2:
            continue
        for device in deduped:
            peers = [peer for peer in deduped if peer.lower() != device.lower()]
            if peers:
                info.peer_devices[device.lower()] = peers
    return info


def parse_gmultipath_list(output: str) -> dict[str, MultipathInfo]:
    multipaths: dict[str, MultipathInfo] = {}
    current: MultipathInfo | None = None
    current_consumer: MultipathConsumer | None = None
    section: str | None = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("Geom name:"):
            name = normalize_text(stripped.split(":", 1)[1]) or "unknown"
            current = MultipathInfo(name=name, device_name=f"multipath/{name}")
            multipaths[current.device_name.lower()] = current
            multipaths[name.lower()] = current
            current_consumer = None
            section = None
            continue

        if current is None:
            continue

        if stripped == "Providers:":
            section = "providers"
            current_consumer = None
            continue

        if stripped == "Consumers:":
            section = "consumers"
            current_consumer = None
            continue

        if section == "providers":
            provider_match = re.match(r"^\d+\.\s+Name:\s+(?P<device>\S+)", stripped)
            if provider_match:
                device_name = normalize_device_name(provider_match.group("device"))
                if device_name:
                    current.device_name = device_name
                    multipaths[current.device_name.lower()] = current
                continue

            # Only indented provider state lines belong to the Providers block.
            # Unindented "State:" / "Mode:" lines that follow Consumers are the
            # top-level multipath summary and need to fall through below.
            if line != stripped and stripped.startswith("State:"):
                current.provider_state = normalize_text(stripped.split(":", 1)[1])
                continue

            if line == stripped:
                section = None

        if section == "consumers":
            consumer_match = re.match(r"^\d+\.\s+Name:\s+(?P<device>\S+)", stripped)
            if consumer_match:
                device_name = normalize_device_name(consumer_match.group("device")) or consumer_match.group("device")
                current_consumer = MultipathConsumer(device_name=device_name)
                current.consumers.append(current_consumer)
                continue

            if current_consumer and line != stripped and stripped.startswith("State:"):
                current_consumer.state = normalize_text(stripped.split(":", 1)[1])
                continue

            if current_consumer and line != stripped and stripped.startswith("Mode:"):
                current_consumer.mode = normalize_text(stripped.split(":", 1)[1])
                continue

            if line == stripped:
                section = None
                current_consumer = None

        if section is not None:
            continue

        if stripped.startswith("UUID:"):
            current.uuid = normalize_text(stripped.split(":", 1)[1])
            continue

        if stripped.startswith("Mode:"):
            current.mode = normalize_text(stripped.split(":", 1)[1])
            continue

        if stripped.startswith("State:"):
            current.state = normalize_text(stripped.split(":", 1)[1])

    return {key: value for key, value in multipaths.items() if key.startswith("multipath/")}


def parse_sesutil_map(output: str) -> list[SESMapEnclosure]:
    """
    Parse `sesutil map` output into enclosure/slot records.

    This format is the most useful one we have seen on TrueNAS CORE for JBOD slot
    mapping because it includes both `Description: SlotNN` and `Device Names: daX`.
    """

    enclosures: list[SESMapEnclosure] = []
    current_enclosure: SESMapEnclosure | None = None
    current_slot: SESMapSlot | None = None
    in_extra_status = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        ses_match = re.match(r"^(ses\d+):\s*$", stripped)
        if ses_match:
            current_enclosure = SESMapEnclosure(ses_device=f"/dev/{ses_match.group(1)}")
            enclosures.append(current_enclosure)
            current_slot = None
            in_extra_status = False
            continue

        if current_enclosure is None:
            continue

        if stripped.startswith("Enclosure Name:"):
            current_enclosure.enclosure_name = normalize_text(stripped.split(":", 1)[1])
            continue

        if stripped.startswith("Enclosure ID:"):
            current_enclosure.enclosure_id = normalize_text(stripped.split(":", 1)[1])
            continue

        element_match = re.match(r"Element\s+(?P<element>\d+),\s+Type:\s+(?P<type>.+)$", stripped)
        if element_match:
            current_slot = None
            in_extra_status = False
            if "Array Device Slot" in element_match.group("type"):
                current_slot = SESMapSlot(
                    slot_number=-1,
                    element_id=int(element_match.group("element")),
                    ses_device=current_enclosure.ses_device,
                    control_targets=[
                        {
                            "ses_device": current_enclosure.ses_device,
                            "ses_element_id": int(element_match.group("element")),
                        }
                    ],
                )
                continue

        if current_slot is None:
            continue

        if stripped.startswith("Status:"):
            current_slot.status = normalize_text(stripped.split(":", 1)[1])
            if current_slot.status:
                lowered = current_slot.status.lower()
                if "not installed" in lowered or "absent" in lowered:
                    current_slot.present = False
                elif "ok" in lowered or "ready" in lowered:
                    current_slot.present = True
            continue

        if stripped.startswith("Description:"):
            description = normalize_text(stripped.split(":", 1)[1])
            current_slot.description = description
            slot_match = SLOT_REGEX.search(description or "")
            if slot_match:
                current_slot.slot_number = int(slot_match.group("slot"))
                current_enclosure.slots[current_slot.slot_number] = current_slot
            continue

        if stripped.startswith("Device Names:"):
            names = [item.strip() for item in stripped.split(":", 1)[1].split(",")]
            current_slot.device_names = [item for item in names if item and not item.startswith("pass")]
            if current_slot.device_names:
                current_slot.present = True
            continue

        if stripped.startswith("Extra status:"):
            in_extra_status = True
            continue

        if in_extra_status and "LED=locate" in stripped:
            current_slot.identify_active = True

    parsed = [item for item in enclosures if item.slots]
    for enclosure in parsed:
        _apply_inferred_ses_profile(enclosure)
    return parsed


def parse_sesutil_show_enclosures(output: str) -> list[SESMapEnclosure]:
    """
    Parse `sesutil show` output into enclosure/slot records.

    Compared with `sesutil map`, this format carries friendlier serial/model/size
    hints, so we use it as a fallback parser and as a metadata overlay when both
    commands are available.
    """

    enclosures: list[SESMapEnclosure] = []
    current_enclosure: SESMapEnclosure | None = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        header_match = re.match(r"^(ses\d+):\s+<(?P<name>[^>]+)>;\s+ID:\s+(?P<id>\S+)", stripped)
        if header_match:
            current_enclosure = SESMapEnclosure(
                ses_device=f"/dev/{header_match.group(1)}",
                enclosure_id=normalize_text(header_match.group("id")),
                enclosure_name=normalize_text(header_match.group("name")),
            )
            enclosures.append(current_enclosure)
            continue

        if current_enclosure is None or stripped.startswith("Desc"):
            continue

        if stripped.startswith(("Temperatures:", "Voltages:")):
            continue

        columns = re.split(r"\s{2,}", stripped, maxsplit=4)
        if len(columns) < 5:
            continue

        slot_match = SLOT_REGEX.search(columns[0])
        if not slot_match:
            continue

        slot_number = int(slot_match.group("slot"))
        device_name = normalize_device_name(columns[1] if columns[1] != "-" else None)
        model = normalize_text(columns[2]) if columns[2] != "-" else None
        serial = normalize_text(columns[3]) if columns[3] != "-" else None
        status_text = normalize_text(columns[4]) or "Unknown"
        status_lower = status_text.lower()

        current_enclosure.slots[slot_number] = SESMapSlot(
            slot_number=slot_number,
            ses_device=current_enclosure.ses_device,
            control_targets=[
                {
                    "ses_device": current_enclosure.ses_device,
                    "ses_element_id": slot_number,
                }
            ],
            status=status_text,
            description=normalize_text(columns[0]),
            device_names=[device_name] if device_name else [],
            identify_active="led=locate" in status_lower or "identify" in status_lower,
            serial=serial,
            model=model,
            size_text=None if "not installed" in status_lower else normalize_text(status_text.split(",", 1)[0]),
            present=bool(device_name) and "not installed" not in status_lower,
        )

    parsed = [item for item in enclosures if item.slots]
    for enclosure in parsed:
        _apply_inferred_ses_profile(enclosure)
    return parsed


def parse_sg_ses_aes(output: str, command: str | None = None) -> SESMapEnclosure | None:
    """
    Parse `sg_ses -p aes /dev/sgN` output into an enclosure/slot record.

    On Linux/TrueNAS SCALE, the AES page is the first practical slot source we
    have found for these Supermicro shelves. It exposes per-slot SAS addresses
    and element indices, which is enough to build a slot map even before we have
    prettier enclosure APIs available.
    """

    ses_device = _extract_sg_ses_device(command)
    enclosure = SESMapEnclosure(ses_device=ses_device)
    current_slot: SESMapSlot | None = None
    in_array_slots = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if enclosure.enclosure_name is None:
            name = normalize_text(" ".join(stripped.split()))
            if name:
                enclosure.enclosure_name = name
            continue

        if stripped.startswith("Primary enclosure logical identifier"):
            enclosure.enclosure_id = normalize_text(stripped.split(":", 1)[1])
            continue

        if stripped.startswith("Element type:"):
            in_array_slots = "Array device slot" in stripped
            current_slot = None
            continue

        if not in_array_slots:
            continue

        element_match = re.match(r"Element index:\s*(?P<element>\d+)", stripped)
        if element_match:
            current_slot = SESMapSlot(
                slot_number=-1,
                element_id=int(element_match.group("element")),
                ses_device=ses_device,
            )
            continue

        if current_slot is None:
            continue

        slot_match = re.search(r"device slot number:\s*(?P<slot>\d+)", stripped, re.IGNORECASE)
        if slot_match:
            current_slot.slot_number = int(slot_match.group("slot"))
            current_slot.description = f"Slot {current_slot.slot_number:02d}"
            current_slot.control_targets = _merge_control_targets(
                current_slot.control_targets,
                [
                    {
                        "ses_device": ses_device,
                        "ses_element_id": current_slot.element_id,
                        "ses_slot_number": current_slot.slot_number,
                    }
                ],
            )
            enclosure.slots[current_slot.slot_number] = current_slot
            continue

        if stripped.startswith("SAS device type:"):
            current_slot.sas_device_type = normalize_text(stripped.split(":", 1)[1])
            if current_slot.sas_device_type:
                lowered = current_slot.sas_device_type.lower()
                current_slot.present = "no sas device attached" not in lowered
            continue

        if stripped.startswith("SAS address:"):
            current_slot.sas_address = normalize_hex_identifier(stripped.split(":", 1)[1])
            if current_slot.sas_address == "0":
                current_slot.present = False
            elif current_slot.sas_address:
                current_slot.present = True
            continue

        if stripped.startswith("attached SAS address:"):
            current_slot.attached_sas_address = normalize_hex_identifier(stripped.split(":", 1)[1])
            continue

    if not enclosure.slots:
        return None

    _apply_inferred_ses_profile(enclosure)
    return enclosure


def parse_sg_ses_enclosure_status(output: str, command: str | None = None) -> SESMapEnclosure | None:
    """
    Parse `sg_ses -p ec /dev/sgN` output into slot status/identify metadata.

    The AES page tells us which SAS address belongs to which slot. The
    Enclosure Status page is what lets us track whether an identify LED is
    currently asserted on a given slot after a refresh.
    """

    ses_device = _extract_sg_ses_device(command)
    enclosure = SESMapEnclosure(ses_device=ses_device)
    current_slot: SESMapSlot | None = None
    in_array_slots = False

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if enclosure.enclosure_name is None:
            name = normalize_text(" ".join(stripped.split()))
            if name:
                enclosure.enclosure_name = name
            continue

        if stripped.startswith("Primary enclosure logical identifier"):
            enclosure.enclosure_id = normalize_text(stripped.split(":", 1)[1])
            continue

        if stripped.startswith("Element type:"):
            in_array_slots = "Array device slot" in stripped
            current_slot = None
            continue

        if not in_array_slots:
            continue

        if stripped.startswith("Overall descriptor:"):
            current_slot = None
            continue

        element_match = re.match(r"Element\s+(?P<slot>\d+)\s+descriptor:", stripped)
        if element_match:
            slot_number = int(element_match.group("slot"))
            current_slot = SESMapSlot(
                slot_number=slot_number,
                element_id=slot_number,
                ses_device=ses_device,
                description=f"Slot {slot_number:02d}",
                control_targets=[
                    {
                        "ses_device": ses_device,
                        "ses_element_id": slot_number,
                        "ses_slot_number": slot_number,
                    }
                ],
            )
            enclosure.slots[slot_number] = current_slot
            continue

        if current_slot is None:
            continue

        if stripped.startswith("Predicted failure=") and "status:" in stripped:
            current_slot.status = normalize_text(stripped.split("status:", 1)[1])
            if current_slot.status:
                current_slot.present = "not installed" not in current_slot.status.lower()
            for field_name, attribute in (
                ("Predicted failure", "predicted_failure"),
                ("Disabled", "disabled"),
                ("Hot spare", "hot_spare"),
            ):
                match = re.search(rf"{re.escape(field_name)}=(?P<value>[01])", stripped)
                if match:
                    setattr(current_slot, attribute, match.group("value") == "1")
            continue

        ident_match = re.search(r"\bIdent=(?P<ident>[01])\b", stripped)
        if ident_match:
            current_slot.identify_active = ident_match.group("ident") == "1"
            continue

        for field_name, attribute in (
            ("Do not remove", "do_not_remove"),
            ("Fault sensed", "fault_sensed"),
            ("Fault reqstd", "fault_requested"),
        ):
            match = re.search(rf"{re.escape(field_name)}=(?P<value>[01])", stripped)
            if match:
                setattr(current_slot, attribute, match.group("value") == "1")

    if not enclosure.slots:
        return None

    _apply_inferred_ses_profile(enclosure)
    return enclosure


def _extract_sg_ses_device(command: str | None) -> str | None:
    if not command:
        return None
    match = re.search(r"(/dev/sg\d+)", command)
    return normalize_text(match.group(1)) if match else None


def _infer_ses_slot_count(enclosure: SESMapEnclosure) -> int:
    if not enclosure.slots:
        return 0
    slot_numbers = sorted(enclosure.slots)
    slot_base = 0 if slot_numbers[0] == 0 else 1
    return slot_numbers[-1] - slot_base + 1


def _apply_inferred_ses_profile(enclosure: SESMapEnclosure) -> None:
    slot_count = _infer_ses_slot_count(enclosure)
    (
        enclosure.profile_id,
        inferred_label,
        enclosure.layout_rows,
        enclosure.layout_columns,
        enclosure.slot_layout,
    ) = _infer_scale_enclosure_profile(
        enclosure,
        slot_count,
    )
    if enclosure.profile_id or slot_count not in {30, 60}:
        enclosure.enclosure_label = inferred_label


def _infer_scale_enclosure_profile(
    enclosure: SESMapEnclosure,
    slot_count: int,
) -> tuple[str | None, str | None, int | None, int | None, list[list[int | None]] | None]:
    name = (enclosure.enclosure_name or "").lower()
    ses_device = (enclosure.ses_device or "").lower()

    if "sas3x40" in name or slot_count == 24 or ses_device.endswith("sg27"):
        # Front 24-bay CryoStorage chassis view: 4 columns across, 6 rows tall.
        # The operator-facing front view counts each column bottom-to-top, so
        # slot 0 sits at the bottom of column 1 and slot 5 sits at the top.
        # Each vertical 6-slot column corresponds to one front vdev.
        return (
            SCALE_SSG_FRONT_24_PROFILE_ID,
            "Front 24 Bay",
            6,
            4,
            [
                [5, 11, 17, 23],
                [4, 10, 16, 22],
                [3, 9, 15, 21],
                [2, 8, 14, 20],
                [1, 7, 13, 19],
                [0, 6, 12, 18],
            ],
        )
    if "sas3x28" in name or slot_count == 12 or ses_device.endswith("sg38"):
        # Rear 12-bay view: 4 columns across, 3 rows tall, matching the rear
        # backplane numbering diagrams and the operator's front-view notes.
        return (
            SCALE_SSG_REAR_12_PROFILE_ID,
            "Rear 12 Bay",
            3,
            4,
            [
                [2, 5, 8, 11],
                [1, 4, 7, 10],
                [0, 3, 6, 9],
            ],
        )
    if slot_count == 60:
        return None, "60 Bay Shelf", 4, 15, None
    if slot_count == 30:
        return None, "30 Bay Shelf", 3, 10, None

    rows = max(1, min(4, slot_count))
    columns = max(1, (slot_count + rows - 1) // rows)
    return None, f"{slot_count} Bay SES", rows, columns, None


def _merge_ses_enclosures(enclosures: list[SESMapEnclosure]) -> list[SESMapEnclosure]:
    merged: dict[str, SESMapEnclosure] = {}

    for enclosure in enclosures:
        key: str | None = None
        if enclosure.enclosure_id:
            for candidate_key, candidate in merged.items():
                if candidate.enclosure_id == enclosure.enclosure_id:
                    key = candidate_key
                    break
        if key is None and enclosure.ses_device:
            for candidate_key, candidate in merged.items():
                if candidate.ses_device == enclosure.ses_device:
                    key = candidate_key
                    break
        if key is None and enclosure.enclosure_name:
            for candidate_key, candidate in merged.items():
                if candidate.enclosure_name == enclosure.enclosure_name:
                    key = candidate_key
                    break
        if key is None:
            key = enclosure.enclosure_id or enclosure.ses_device or enclosure.enclosure_name or f"unknown-{len(merged)}"
            merged[key] = SESMapEnclosure(
                ses_device=enclosure.ses_device,
                enclosure_id=enclosure.enclosure_id,
                enclosure_name=enclosure.enclosure_name,
                enclosure_label=enclosure.enclosure_label,
                profile_id=enclosure.profile_id,
                layout_rows=enclosure.layout_rows,
                layout_columns=enclosure.layout_columns,
                slot_layout=enclosure.slot_layout,
                slots={},
            )
        target = merged[key]
        target.enclosure_id = target.enclosure_id or enclosure.enclosure_id
        target.enclosure_name = target.enclosure_name or enclosure.enclosure_name
        target.ses_device = target.ses_device or enclosure.ses_device
        target.enclosure_label = target.enclosure_label or enclosure.enclosure_label
        target.profile_id = target.profile_id or enclosure.profile_id
        target.layout_rows = target.layout_rows or enclosure.layout_rows
        target.layout_columns = target.layout_columns or enclosure.layout_columns
        target.slot_layout = target.slot_layout or enclosure.slot_layout

        for slot_number, slot in enclosure.slots.items():
            existing = target.slots.get(slot_number)
            if existing is None:
                target.slots[slot_number] = slot
                continue

            # Dual-path SAS shelves often expose the same physical slot twice.
            existing.device_names = list(dict.fromkeys(existing.device_names + slot.device_names))
            existing.identify_active = existing.identify_active or slot.identify_active
            existing.status = existing.status or slot.status
            existing.description = existing.description or slot.description
            existing.element_id = existing.element_id or slot.element_id
            existing.ses_device = existing.ses_device or slot.ses_device
            existing.control_targets = _merge_control_targets(existing.control_targets, slot.control_targets)
            existing.serial = existing.serial or slot.serial
            existing.model = existing.model or slot.model
            existing.size_text = existing.size_text or slot.size_text
            existing.sas_address = existing.sas_address or slot.sas_address
            existing.sas_device_type = existing.sas_device_type or slot.sas_device_type
            if existing.present is None:
                existing.present = slot.present
            elif slot.present is not None:
                existing.present = existing.present or slot.present

    return list(merged.values())


def _enclosure_sort_key(item: SESMapEnclosure) -> tuple[int, int, int, str, str]:
    name = (item.enclosure_name or "").lower()
    priority = 2
    if "front" in name:
        priority = 0
    elif "rear" in name:
        priority = 1

    present_slots = sum(
        1
        for slot in item.slots.values()
        if slot.present or slot.device_names or (slot.status and "not installed" not in slot.status.lower())
    )
    max_slot = max(item.slots.keys()) if item.slots else 0

    return (
        priority,
        -present_slots,
        -max_slot,
        item.enclosure_name or "",
        item.enclosure_id or "",
    )


def _pick_preferred_enclosures(
    enclosures: list[SESMapEnclosure],
    slot_count: int,
) -> list[SESMapEnclosure]:
    if not enclosures:
        return []

    front_rear = [
        item
        for item in enclosures
        if item.enclosure_name and ("front" in item.enclosure_name.lower() or "rear" in item.enclosure_name.lower())
    ]
    candidates = front_rear or enclosures
    ordered = sorted(candidates, key=_enclosure_sort_key)

    selected: list[SESMapEnclosure] = []
    capacity = 0
    for enclosure in ordered:
        selected.append(enclosure)
        capacity += max(enclosure.slots.keys()) if enclosure.slots else 0
        if slot_count and capacity >= slot_count:
            break

    return selected or ordered[:1]


def _merge_control_targets(
    base: list[dict[str, Any]],
    overlay: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, int | None]] = set()
    for item in base + overlay:
        if not isinstance(item, dict):
            continue
        ssh_host = normalize_text(item.get("ssh_host"))
        ses_device = normalize_text(item.get("ses_device"))
        ses_element_id = item.get("ses_element_id")
        pair = (ssh_host, ses_device, ses_element_id if isinstance(ses_element_id, int) else None)
        if not pair[1] or pair[2] is None or pair in seen:
            continue
        seen.add(pair)
        payload = {
            "ses_device": pair[1],
            "ses_element_id": pair[2],
            "ses_slot_number": item.get("ses_slot_number")
            if isinstance(item.get("ses_slot_number"), int)
            else None,
        }
        if pair[0]:
            payload["ssh_host"] = pair[0]
        merged.append(payload)
    return merged


def merge_slot_candidate_maps(
    base: dict[int, dict[str, Any]],
    overlay: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    merged = {slot: dict(payload) for slot, payload in base.items()}

    for slot, payload in overlay.items():
        target = merged.setdefault(slot, {})
        for key, value in payload.items():
            if key == "device_names":
                existing = target.get(key, [])
                if isinstance(existing, list) and isinstance(value, list):
                    target[key] = list(dict.fromkeys(existing + value))
                continue
            if key == "ses_targets":
                existing = target.get(key, [])
                if isinstance(existing, list) and isinstance(value, list):
                    combined = []
                    seen: set[tuple[str | None, str | None, int | None]] = set()
                    for item in existing + value:
                        if not isinstance(item, dict):
                            continue
                        pair = (
                            normalize_text(item.get("ssh_host")),
                            normalize_text(item.get("ses_device")),
                            item.get("ses_element_id"),
                        )
                        if pair in seen:
                            continue
                        seen.add(pair)
                        payload = {
                            "ses_device": pair[1],
                            "ses_element_id": pair[2],
                            "ses_slot_number": item.get("ses_slot_number")
                            if isinstance(item.get("ses_slot_number"), int)
                            else None,
                        }
                        if pair[0]:
                            payload["ssh_host"] = pair[0]
                        combined.append(payload)
                    target[key] = combined
                continue
            if key in {"identify_active", "present"}:
                existing = target.get(key)
                if isinstance(existing, bool) and isinstance(value, bool):
                    target[key] = existing or value
                    continue
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, (list, dict)) and not value:
                continue
            target[key] = value

    return merged


def merge_enclosure_meta(
    base: dict[str, str | None],
    overlay: dict[str, str | None],
) -> dict[str, str | None]:
    merged = dict(base)
    for key, value in overlay.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    return merged


def build_slot_candidates_from_ses_enclosures(
    enclosures: list[SESMapEnclosure],
    slot_count: int,
    enclosure_filter: str | None,
    selected_enclosure_id: str | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, str | None]]:
    """
    Convert parsed SES maps into the app's 0-based slot view.

    Many systems expose two 30-slot SES groups for a 60-bay shelf. When that pattern
    is detected, we stack the groups into one 60-slot view in name order, preferring
    "Front" before "Rear". Duplicate paths to the same enclosure ID are merged.
    """
    merged_enclosures = _merge_ses_enclosures(enclosures)
    selected_ids = {
        item
        for item in (
            normalize_text(value)
            for value in (selected_enclosure_id.split("+") if selected_enclosure_id else [])
        )
        if item
    }
    filter_text = enclosure_filter.lower() if enclosure_filter else None

    filtered = []
    for enclosure in merged_enclosures:
        if selected_ids:
            if enclosure.enclosure_id in selected_ids:
                filtered.append(enclosure)
            continue

        haystack = " ".join(filter(None, [enclosure.enclosure_id or "", enclosure.enclosure_name or ""])).lower()
        if filter_text and filter_text not in haystack:
            continue
        filtered.append(enclosure)

    ordered = _pick_preferred_enclosures(filtered, slot_count)

    candidates: dict[int, dict[str, Any]] = {}
    labels: list[str] = []
    offset = 0
    for enclosure in ordered:
        slot_numbers = sorted(enclosure.slots.keys())
        if not slot_numbers:
            continue
        min_slot = slot_numbers[0]
        max_slot = slot_numbers[-1]
        slot_base = 0 if min_slot == 0 else 1
        labels.append(enclosure.enclosure_name or enclosure.enclosure_id or "SES enclosure")
        for slot_number, slot in sorted(enclosure.slots.items()):
            combined_slot = offset + slot_number - slot_base
            if combined_slot < 0 or combined_slot >= slot_count:
                continue

            candidates[combined_slot] = {
                "status": slot.status,
                "descriptor": slot.description,
                "value": slot.status,
                "device_hint": slot.device_names[0] if slot.device_names else None,
                "device_names": slot.device_names,
                "identify_active": slot.identify_active,
                "serial_hint": slot.serial,
                "model_hint": slot.model,
                "reported_size": slot.size_text,
                "present": slot.present
                if slot.present is not None
                else bool(slot.device_names) and "not installed" not in (slot.status or "").lower(),
                "enclosure_id": enclosure.enclosure_id,
                "enclosure_label": enclosure.enclosure_label,
                "enclosure_name": enclosure.enclosure_name,
                "ses_device": enclosure.ses_device,
                "ses_element_id": slot.element_id,
                "ses_slot_number": slot.slot_number,
                "sas_address_hint": slot.sas_address,
                "attached_sas_address": slot.attached_sas_address,
                "sas_device_type": slot.sas_device_type,
                "ses_predicted_failure": slot.predicted_failure,
                "ses_disabled": slot.disabled,
                "ses_hot_spare": slot.hot_spare,
                "ses_do_not_remove": slot.do_not_remove,
                "ses_fault_sensed": slot.fault_sensed,
                "ses_fault_requested": slot.fault_requested,
                "ses_targets": _merge_control_targets(
                    slot.control_targets,
                    [
                        {
                            "ses_device": slot.ses_device or enclosure.ses_device,
                            "ses_element_id": slot.element_id,
                            "ses_slot_number": slot.slot_number,
                        }
                    ],
                ),
            }
        offset += max_slot - slot_base + 1

    return candidates, {
        "id": "+".join(filter(None, [item.enclosure_id for item in ordered])) or None,
        "label": " + ".join(
            filter(None, [item.enclosure_label or item.enclosure_name or item.enclosure_id for item in ordered])
        )
        if ordered
        else None,
        "name": ordered[0].enclosure_name if len(ordered) == 1 else "SES Combined View" if ordered else None,
    }


def _flatten_candidates(payload: Any, ancestry: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    candidates: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    if isinstance(payload, dict):
        candidates.append((ancestry, payload))
        for key, value in payload.items():
            candidates.extend(_flatten_candidates(value, ancestry + (str(key),)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            candidates.extend(_flatten_candidates(value, ancestry + (str(index),)))

    return candidates


def _extract_slot_number(candidate: dict[str, Any]) -> int | None:
    explicit_keys = ("slot", "slot_number", "slotNumber", "index", "number")
    for key in explicit_keys:
        value = candidate.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    for key in ("name", "descriptor", "label", "original", "value"):
        text = candidate.get(key)
        if not isinstance(text, str):
            continue
        match = SLOT_REGEX.search(text)
        if match:
            return int(match.group("slot"))

    return None


def extract_enclosure_slot_candidates(
    enclosures: list[dict[str, Any]],
    enclosure_filter: str | None,
    slot_count: int,
    api_slot_number_base: int,
    selected_enclosure_id: str | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, str | None]]:
    """
    Extract slot metadata from `enclosure.query` defensively.

    TrueNAS enclosure payloads vary between platforms and generations. This helper
    purposely searches for slot-looking records instead of trusting one exact shape.
    When the API shape changes, this is the main function to adjust.
    """

    selected_meta: dict[str, str | None] = {"id": None, "label": None, "name": None}
    scored: dict[int, tuple[int, dict[str, Any]]] = {}
    filter_text = enclosure_filter.lower() if enclosure_filter else None
    selected_ids = {
        item
        for item in (
            normalize_text(value)
            for value in (selected_enclosure_id.split("+") if selected_enclosure_id else [])
        )
        if item
    }

    for enclosure in enclosures:
        enclosure_id = str(enclosure.get("id") or "")
        enclosure_name = str(enclosure.get("name") or "")
        enclosure_label = str(enclosure.get("label") or "")
        haystack = " ".join([enclosure_id, enclosure_name, enclosure_label]).lower()
        if selected_ids:
            if enclosure_id not in selected_ids:
                continue
        elif filter_text and filter_text not in haystack:
            continue

        if selected_meta["id"] is None:
            selected_meta = {
                "id": enclosure_id or None,
                "label": enclosure_label or None,
                "name": enclosure_name or None,
            }
        elif selected_meta["id"] != enclosure_id:
            # First pass intentionally targets a single enclosure. Once selected,
            # ignore later candidates so slot data doesn't get merged across shelves.
            continue

        for ancestry, candidate in _flatten_candidates(enclosure):
            raw_slot = _extract_slot_number(candidate)
            if raw_slot is None:
                continue

            slot = raw_slot - api_slot_number_base
            if slot < 0 or slot >= slot_count:
                continue

            ancestry_text = " ".join(ancestry).lower()
            score = 1
            if any(keyword in ancestry_text for keyword in ("drive", "slot", "array", "element")):
                score += 3
            if any(key in candidate for key in ("dev", "device", "status", "value")):
                score += 2
            if candidate.get("descriptor") or candidate.get("name"):
                score += 1

            existing = scored.get(slot)
            if existing and existing[0] >= score:
                continue

            device_hint = None
            for key in ("dev", "device", "original", "value_raw", "value"):
                value = candidate.get(key)
                if isinstance(value, str) and DEVICE_REGEX.search(value):
                    device_hint = normalize_device_name(value)
                    break

            scored[slot] = (
                score,
                {
                    "status": candidate.get("status"),
                    "descriptor": candidate.get("descriptor") or candidate.get("name"),
                    "value": candidate.get("value"),
                    "value_raw": candidate.get("value_raw"),
                    "device_hint": device_hint,
                    "ancestry": list(ancestry),
                    "enclosure_id": enclosure_id,
                },
            )

    return {slot: payload for slot, (_, payload) in scored.items()}, selected_meta


def parse_zpool_status(output: str) -> dict[str, ZpoolMember]:
    members: dict[str, ZpoolMember] = {}
    current_pool: str | None = None
    current_class = "data"
    in_config = False
    stack: list[dict[str, Any]] = []
    class_names = {
        "logs": "log",
        "log": "log",
        "cache": "cache",
        "spares": "spare",
        "spare": "spare",
        "special": "special",
        "dedup": "dedup",
        "data": "data",
    }

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("pool:"):
            current_pool = stripped.split(":", 1)[1].strip()
            current_class = "data"
            in_config = False
            stack = []
            continue

        if stripped == "config:":
            in_config = True
            stack = []
            continue

        if not in_config:
            continue

        if not stripped or stripped.startswith("NAME") or stripped.startswith("errors:"):
            if stripped.startswith("errors:"):
                in_config = False
            continue

        indent = len(line) - len(line.lstrip(" "))
        parts = stripped.split()
        name = parts[0]
        health = parts[1] if len(parts) > 1 else None

        if len(parts) == 1 and name.lower().rstrip(":") in class_names:
            current_class = class_names[name.lower().rstrip(":")]
            stack = [{"indent": indent, "name": name, "kind": "class"}]
            continue

        while stack and indent <= stack[-1]["indent"]:
            stack.pop()

        kind = "node"
        if current_pool and name == current_pool:
            kind = "pool"
        stack.append({"indent": indent, "name": name, "kind": kind})

        if kind == "pool" or current_pool is None:
            continue

        is_leaf = bool(DEVICE_REGEX.search(name) or GPTID_REGEX.search(name) or name.startswith("/dev/") or GUID_REGEX.match(name))
        if not is_leaf:
            continue

        ancestors = [
            entry["name"]
            for entry in stack[:-1]
            if entry["kind"] == "node" and entry["name"] not in class_names and entry["name"] != current_pool
        ]
        vdev_name = ancestors[0] if ancestors else None
        topology_label = _compose_topology_label(current_pool, ancestors, current_class)
        member = ZpoolMember(
            pool_name=current_pool,
            vdev_class=current_class,
            vdev_name=vdev_name,
            topology_label=topology_label,
            health=health,
            raw_name=name,
            raw_path=name if name.startswith("/dev/") else None,
        )
        for key in normalize_lookup_keys(name):
            members[key] = member

    return members


def parse_pool_query_topology(pools: list[dict[str, Any]]) -> dict[str, ZpoolMember]:
    """
    Build disk-to-topology mappings directly from `pool.query`.

    This gives API-only mode useful pool/vdev information even when SSH is disabled.
    It does not solve physical slot mapping on its own, but it makes manual mapping
    and calibration much more informative on systems where `enclosure.query` is empty.
    """

    members: dict[str, ZpoolMember] = {}
    class_names = {
        "data": "data",
        "cache": "cache",
        "log": "log",
        "logs": "log",
        "special": "special",
        "spare": "spare",
        "spares": "spare",
        "dedup": "dedup",
    }

    def walk(
        pool_name: str,
        vdev_class: str,
        node: dict[str, Any],
        ancestors: list[str],
        current_label: str | None,
    ) -> None:
        node_type = str(node.get("type") or "").upper()
        path = node.get("path")
        device = node.get("device")
        disk = node.get("disk")
        guid = node.get("guid")
        health = normalize_text(node.get("status"))
        children = node.get("children") if isinstance(node.get("children"), list) else []
        current_path = list(ancestors)
        if current_label:
            current_path.append(current_label)

        if node_type == "DISK" or (not children and any((path, device, disk, guid))):
            vdev_name = current_path[0] if current_path else None
            topology_label = _compose_topology_label(pool_name, current_path, vdev_class)
            member = ZpoolMember(
                pool_name=pool_name,
                vdev_class=vdev_class,
                vdev_name=vdev_name,
                topology_label=topology_label,
                health=health,
                raw_name=str(path or device or disk or guid or ""),
                raw_path=path,
            )
            for candidate in (path, device, disk, str(guid) if guid is not None else None):
                for key in normalize_lookup_keys(candidate):
                    members[key] = member
            return

        for child in children:
            if isinstance(child, dict):
                walk(
                    pool_name,
                    vdev_class,
                    child,
                    current_path,
                    _derive_nested_vdev_label(child),
                )

    for pool in pools:
        pool_name = normalize_text(pool.get("name")) or "unknown"
        topology = pool.get("topology") if isinstance(pool.get("topology"), dict) else {}
        composite_vdev_index = 0
        for raw_class, nodes in topology.items():
            vdev_class = class_names.get(str(raw_class).lower(), str(raw_class).lower())
            if not isinstance(nodes, list):
                continue
            for class_index, node in enumerate(nodes):
                if isinstance(node, dict):
                    node_type = str(node.get("type") or "").upper()
                    children = node.get("children") if isinstance(node.get("children"), list) else []
                    is_composite = bool(children) and node_type not in {"DISK", "FILE", "ROOT"}
                    top_level_label = _derive_top_level_vdev_label(
                        vdev_class=vdev_class,
                        node=node,
                        class_index=class_index,
                        composite_index=composite_vdev_index if is_composite else None,
                    )
                    walk(pool_name, vdev_class, node, [], top_level_label)
                    if is_composite:
                        composite_vdev_index += 1

    return members


def parse_smart_test_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    parsed: dict[str, dict[str, Any]] = {}

    for item in results:
        disk_name = normalize_device_name(item.get("disk"))
        tests = item.get("tests")
        if not disk_name or not isinstance(tests, list) or not tests:
            continue

        latest = tests[0] if isinstance(tests[0], dict) else None
        if not latest:
            continue

        parsed[disk_name.lower()] = {
            "description": normalize_text(latest.get("description")),
            "status": normalize_text(latest.get("status")),
            "status_verbose": normalize_text(latest.get("status_verbose")),
            "lifetime": latest.get("lifetime") if isinstance(latest.get("lifetime"), int) else None,
            "current_test": item.get("current_test"),
        }

    return parsed


def parse_smartctl_summary(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"available": False, "message": "SMART JSON parsing failed."}

    temperature = payload.get("temperature") if isinstance(payload.get("temperature"), dict) else {}
    environmental_reports = (
        payload.get("scsi_environmental_reports")
        if isinstance(payload.get("scsi_environmental_reports"), dict)
        else {}
    )
    environmental_temperature = (
        environmental_reports.get("temperature_1")
        if isinstance(environmental_reports.get("temperature_1"), dict)
        else {}
    )
    power_on_time = payload.get("power_on_time") if isinstance(payload.get("power_on_time"), dict) else {}
    logical_block_size = payload.get("logical_block_size")
    physical_block_size = payload.get("physical_block_size")
    firmware_version = normalize_text(payload.get("firmware_version"))
    protocol_version = (
        normalize_text(payload.get("nvme_version", {}).get("string"))
        if isinstance(payload.get("nvme_version"), dict)
        else None
    )
    if not protocol_version:
        protocol_version = (
            normalize_text(payload.get("sata_version", {}).get("string"))
            if isinstance(payload.get("sata_version"), dict)
            else None
        )
    nvme_health = (
        payload.get("nvme_smart_health_information_log")
        if isinstance(payload.get("nvme_smart_health_information_log"), dict)
        else {}
    )
    smart_status = payload.get("smart_status") if isinstance(payload.get("smart_status"), dict) else {}
    rotation_rate = payload.get("rotation_rate") if isinstance(payload.get("rotation_rate"), int) else None
    form_factor = (
        normalize_text(payload.get("form_factor", {}).get("name"))
        if isinstance(payload.get("form_factor"), dict)
        else None
    )
    transport_protocol = (
        normalize_text(payload.get("scsi_transport_protocol", {}).get("name"))
        if isinstance(payload.get("scsi_transport_protocol"), dict)
        else None
    )
    if not transport_protocol:
        transport_protocol = (
            normalize_text(payload.get("device", {}).get("protocol"))
            if isinstance(payload.get("device"), dict)
            else None
    )
    logical_unit_id = format_hex_identifier(payload.get("logical_unit_id"))
    smart_health_status = None
    if smart_status:
        passed = smart_status.get("passed")
        if passed is True:
            smart_health_status = "PASSED"
        elif passed is False:
            smart_health_status = "FAILED"
    power_on_hours = power_on_time.get("hours") if isinstance(power_on_time.get("hours"), int) else None
    power_cycle_count = _extract_ata_attribute_raw_value(payload, 12)
    power_on_resets = _extract_ata_device_stat_int(payload, "Lifetime Power-On Resets")
    warning_temperature_c = None
    critical_temperature_c = None
    available_spare_percent = _coerce_non_negative_int(nvme_health.get("available_spare"))
    available_spare_threshold_percent = _coerce_non_negative_int(nvme_health.get("available_spare_threshold"))
    endurance_used_percent = _coerce_non_negative_int(nvme_health.get("percentage_used"))
    endurance_remaining_percent = (
        max(0, 100 - endurance_used_percent) if endurance_used_percent is not None else None
    )
    bytes_read = _nvme_data_units_to_bytes(nvme_health.get("data_units_read"))
    bytes_written = _nvme_data_units_to_bytes(nvme_health.get("data_units_written"))
    media_errors = _coerce_non_negative_int(nvme_health.get("media_errors"))
    unsafe_shutdowns = _coerce_non_negative_int(nvme_health.get("unsafe_shutdowns"))
    scsi_error_counter_log = (
        payload.get("scsi_error_counter_log")
        if isinstance(payload.get("scsi_error_counter_log"), dict)
        else {}
    )
    scsi_read_error_log = (
        scsi_error_counter_log.get("read")
        if isinstance(scsi_error_counter_log.get("read"), dict)
        else {}
    )
    scsi_write_error_log = (
        scsi_error_counter_log.get("write")
        if isinstance(scsi_error_counter_log.get("write"), dict)
        else {}
    )
    if bytes_read is None:
        bytes_read = _scsi_gigabytes_processed_to_bytes(scsi_read_error_log.get("gigabytes_processed"))
    if bytes_written is None:
        bytes_written = _scsi_gigabytes_processed_to_bytes(scsi_write_error_log.get("gigabytes_processed"))
    sector_size_for_ata = logical_block_size if isinstance(logical_block_size, int) and logical_block_size > 0 else 512
    if bytes_read is None:
        bytes_read = _extract_ata_device_stat_bytes(payload, "Logical Sectors Read", sector_size_for_ata)
    if bytes_written is None:
        bytes_written = _extract_ata_device_stat_bytes(payload, "Logical Sectors Written", sector_size_for_ata)
    if bytes_read is None:
        ata_read_entry = _extract_ata_attribute_entry(payload, 242)
        if ata_read_entry is not None:
            bytes_read = _ata_attribute_raw_value_to_bytes(ata_read_entry, sector_size_for_ata)
    if bytes_written is None:
        ata_write_entry = _extract_ata_attribute_entry(payload, 241)
        if ata_write_entry is not None:
            bytes_written = _ata_attribute_raw_value_to_bytes(ata_write_entry, sector_size_for_ata)
    if available_spare_percent is None:
        available_spare_percent = _extract_ata_attribute_raw_value(payload, 232, 169)
    if endurance_used_percent is None:
        endurance_used_percent = _extract_ata_device_stat_int(payload, "Percentage Used Endurance Indicator")
    if endurance_remaining_percent is None and endurance_used_percent is not None:
        endurance_remaining_percent = max(0, 100 - endurance_used_percent)
    annualized_bytes_written = _annualize_bytes_written(bytes_written, power_on_hours)
    estimated_lifetime_bytes_written = (
        int(bytes_written * 100 / endurance_used_percent)
        if bytes_written is not None
        and endurance_used_percent is not None
        and endurance_used_percent > 0
        else None
    )
    estimated_remaining_bytes_written = (
        max(estimated_lifetime_bytes_written - bytes_written, 0)
        if estimated_lifetime_bytes_written is not None and bytes_written is not None
        else None
    )
    latest_test_type: str | None = None
    latest_test_status: str | None = None
    latest_test_lifetime_hours: int | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None
    read_commands = _extract_ata_device_stat_int(payload, "Number of Read Commands")
    write_commands = _extract_ata_device_stat_int(payload, "Number of Write Commands")
    hardware_resets = _extract_ata_device_stat_int(payload, "Number of Hardware Resets")
    interface_crc_errors = _extract_ata_device_stat_int(payload, "Number of Interface CRC Errors")
    if interface_crc_errors is None:
        interface_crc_errors = _extract_ata_attribute_raw_value(payload, 199)
    read_cache_enabled = (
        payload.get("read_lookahead", {}).get("enabled")
        if isinstance(payload.get("read_lookahead"), dict)
        and isinstance(payload.get("read_lookahead", {}).get("enabled"), bool)
        else None
    )
    writeback_cache_enabled = (
        payload.get("write_cache", {}).get("enabled")
        if isinstance(payload.get("write_cache"), dict)
        and isinstance(payload.get("write_cache", {}).get("enabled"), bool)
        else None
    )
    interface_speed = payload.get("interface_speed") if isinstance(payload.get("interface_speed"), dict) else {}
    interface_speed_current = interface_speed.get("current") if isinstance(interface_speed.get("current"), dict) else {}
    if not negotiated_link_rate:
        negotiated_link_rate = normalize_text(interface_speed_current.get("string"))

    if rotation_rate is None and transport_protocol and transport_protocol.upper() == "NVME":
        rotation_rate = 0

    nvme_namespaces = payload.get("nvme_namespaces") if isinstance(payload.get("nvme_namespaces"), list) else []
    namespace_eui64 = None
    if nvme_namespaces and isinstance(nvme_namespaces[0], dict):
        eui64_payload = nvme_namespaces[0].get("eui64")
        if isinstance(eui64_payload, dict):
            oui = eui64_payload.get("oui")
            ext_id = eui64_payload.get("ext_id")
            if isinstance(oui, int) and isinstance(ext_id, int):
                namespace_eui64 = _format_nvme_eui64(f"{oui:06x}{ext_id:010x}")

    sas_port_phys: list[dict[str, Any]] = []
    for key, value in payload.items():
        if not (key.startswith("scsi_sas_port_") and isinstance(value, dict)):
            continue
        for subkey, phy in value.items():
            if subkey.startswith("phy_") and isinstance(phy, dict):
                sas_port_phys.append(phy)

    selected_phy = next(
        (
            phy
            for phy in sas_port_phys
            if format_hex_identifier(phy.get("attached_sas_address")) not in {None, "0x0"}
            or normalize_text(phy.get("attached_device_type")) not in {None, "no device attached"}
        ),
        sas_port_phys[0] if sas_port_phys else None,
    )
    if selected_phy:
        sas_address = format_hex_identifier(selected_phy.get("sas_address"))
        attached_sas_address = format_hex_identifier(selected_phy.get("attached_sas_address"))
        negotiated_link_rate = normalize_text(selected_phy.get("negotiated_logical_link_rate"))

    scsi_tests: list[tuple[int, dict[str, Any]]] = []
    for key, value in payload.items():
        if not (key.startswith("scsi_self_test_") and isinstance(value, dict)):
            continue
        suffix = key.removeprefix("scsi_self_test_")
        if suffix.isdigit():
            scsi_tests.append((int(suffix), value))

    if scsi_tests:
        _, latest_test = min(scsi_tests, key=lambda item: item[0])
        code = latest_test.get("code") if isinstance(latest_test.get("code"), dict) else {}
        result = latest_test.get("result") if isinstance(latest_test.get("result"), dict) else {}
        lifetime = latest_test.get("power_on_time") if isinstance(latest_test.get("power_on_time"), dict) else {}
        latest_test_type = normalize_text(code.get("string")) or normalize_text(code.get("name"))
        latest_test_status = normalize_text(result.get("string")) or normalize_text(result.get("name"))
        latest_test_lifetime_hours = lifetime.get("hours") if isinstance(lifetime.get("hours"), int) else None
    else:
        ata_log = payload.get("ata_smart_self_test_log") if isinstance(payload.get("ata_smart_self_test_log"), dict) else {}
        standard = ata_log.get("standard") if isinstance(ata_log.get("standard"), dict) else {}
        table = standard.get("table") if isinstance(standard.get("table"), list) else []
        if table and isinstance(table[0], dict):
            latest_test = table[0]
            latest_test_type = normalize_text(latest_test.get("type", {}).get("string")) if isinstance(latest_test.get("type"), dict) else normalize_text(latest_test.get("type"))
            latest_test_status = normalize_text(latest_test.get("status", {}).get("string")) if isinstance(latest_test.get("status"), dict) else normalize_text(latest_test.get("status"))
            lifetime = latest_test.get("lifetime_hours") if isinstance(latest_test.get("lifetime_hours"), dict) else {}
            latest_test_lifetime_hours = lifetime.get("hours") if isinstance(lifetime.get("hours"), int) else None

    current_temperature = (
        temperature.get("current")
        if isinstance(temperature.get("current"), int)
        else environmental_temperature.get("current")
        if isinstance(environmental_temperature.get("current"), int)
        else None
    )

    summary = {
        "available": any(
            value is not None
            for value in (
                current_temperature,
                power_on_hours,
                smart_health_status,
                latest_test_type,
                latest_test_status,
                latest_test_lifetime_hours,
                power_cycle_count,
                power_on_resets,
                logical_block_size if isinstance(logical_block_size, int) else None,
                physical_block_size if isinstance(physical_block_size, int) else None,
                available_spare_percent,
                available_spare_threshold_percent,
                endurance_used_percent,
                endurance_remaining_percent,
                bytes_read,
                bytes_written,
                annualized_bytes_written,
                estimated_remaining_bytes_written,
                read_commands,
                write_commands,
                media_errors,
                unsafe_shutdowns,
                hardware_resets,
                interface_crc_errors,
                rotation_rate,
                form_factor,
                firmware_version,
                protocol_version,
                namespace_eui64,
                warning_temperature_c,
                critical_temperature_c,
                read_cache_enabled,
                writeback_cache_enabled,
                transport_protocol,
                logical_unit_id,
                sas_address,
                attached_sas_address,
                negotiated_link_rate,
            )
        ),
        "temperature_c": current_temperature,
        "warning_temperature_c": warning_temperature_c,
        "critical_temperature_c": critical_temperature_c,
        "smart_health_status": smart_health_status,
        "power_cycle_count": power_cycle_count,
        "power_on_resets": power_on_resets,
        "power_on_hours": power_on_hours,
        "power_on_days": power_on_hours // 24 if isinstance(power_on_hours, int) else None,
        "last_test_type": latest_test_type,
        "last_test_status": latest_test_status,
        "last_test_lifetime_hours": latest_test_lifetime_hours,
        "last_test_age_hours": (
            power_on_hours - latest_test_lifetime_hours
            if isinstance(power_on_hours, int)
            and isinstance(latest_test_lifetime_hours, int)
            and power_on_hours >= latest_test_lifetime_hours
            else None
        ),
        "logical_block_size": logical_block_size if isinstance(logical_block_size, int) else None,
        "physical_block_size": physical_block_size if isinstance(physical_block_size, int) else None,
        "available_spare_percent": available_spare_percent,
        "available_spare_threshold_percent": available_spare_threshold_percent,
        "endurance_used_percent": endurance_used_percent,
        "endurance_remaining_percent": endurance_remaining_percent,
        "bytes_read": bytes_read,
        "bytes_written": bytes_written,
        "annualized_bytes_written": annualized_bytes_written,
        "estimated_lifetime_bytes_written": estimated_lifetime_bytes_written,
        "estimated_remaining_bytes_written": estimated_remaining_bytes_written,
        "read_commands": read_commands,
        "write_commands": write_commands,
        "media_errors": media_errors,
        "unsafe_shutdowns": unsafe_shutdowns,
        "hardware_resets": hardware_resets,
        "interface_crc_errors": interface_crc_errors,
        "rotation_rate_rpm": rotation_rate,
        "form_factor": form_factor,
        "firmware_version": firmware_version,
        "protocol_version": protocol_version,
        "namespace_eui64": namespace_eui64,
        "namespace_nguid": None,
        "read_cache_enabled": read_cache_enabled,
        "writeback_cache_enabled": writeback_cache_enabled,
        "transport_protocol": transport_protocol,
        "logical_unit_id": logical_unit_id,
        "sas_address": sas_address,
        "attached_sas_address": attached_sas_address,
        "negotiated_link_rate": negotiated_link_rate,
        "message": None,
    }

    if not summary["available"]:
        summary["message"] = "No SMART summary fields were returned for this disk."

    return summary


def parse_nvme_smart_log_summary(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"available": False, "message": "NVMe smart-log JSON parsing failed."}

    temperature_c = _kelvin_to_celsius(payload.get("temperature"))
    power_on_hours = _coerce_non_negative_int(payload.get("power_on_hours"))
    available_spare_percent = _coerce_non_negative_int(payload.get("avail_spare"))
    available_spare_threshold_percent = _coerce_non_negative_int(payload.get("spare_thresh"))
    endurance_used_percent = _coerce_non_negative_int(payload.get("percent_used"))
    endurance_remaining_percent = (
        max(0, 100 - endurance_used_percent) if endurance_used_percent is not None else None
    )
    bytes_read = _nvme_data_units_to_bytes(payload.get("data_units_read"))
    bytes_written = _nvme_data_units_to_bytes(payload.get("data_units_written"))
    media_errors = _coerce_non_negative_int(payload.get("media_errors"))
    unsafe_shutdowns = _coerce_non_negative_int(payload.get("unsafe_shutdowns"))
    annualized_bytes_written = _annualize_bytes_written(bytes_written, power_on_hours)
    estimated_lifetime_bytes_written = (
        int(bytes_written * 100 / endurance_used_percent)
        if bytes_written is not None
        and endurance_used_percent is not None
        and endurance_used_percent > 0
        else None
    )
    estimated_remaining_bytes_written = (
        max(estimated_lifetime_bytes_written - bytes_written, 0)
        if estimated_lifetime_bytes_written is not None and bytes_written is not None
        else None
    )

    summary = {
        "available": any(
            value is not None
            for value in (
                temperature_c,
                power_on_hours,
                available_spare_percent,
                available_spare_threshold_percent,
                endurance_used_percent,
                endurance_remaining_percent,
                bytes_read,
                bytes_written,
                annualized_bytes_written,
                estimated_remaining_bytes_written,
                media_errors,
                unsafe_shutdowns,
            )
        ),
        "temperature_c": temperature_c,
        "power_on_hours": power_on_hours,
        "power_on_days": power_on_hours // 24 if isinstance(power_on_hours, int) else None,
        "available_spare_percent": available_spare_percent,
        "available_spare_threshold_percent": available_spare_threshold_percent,
        "endurance_used_percent": endurance_used_percent,
        "endurance_remaining_percent": endurance_remaining_percent,
        "bytes_read": bytes_read,
        "bytes_written": bytes_written,
        "annualized_bytes_written": annualized_bytes_written,
        "estimated_lifetime_bytes_written": estimated_lifetime_bytes_written,
        "estimated_remaining_bytes_written": estimated_remaining_bytes_written,
        "media_errors": media_errors,
        "unsafe_shutdowns": unsafe_shutdowns,
        "rotation_rate_rpm": 0,
        "transport_protocol": "NVMe",
        "message": None,
    }
    if not summary["available"]:
        summary["message"] = "No NVMe smart-log summary fields were returned for this disk."
    return summary


def parse_nvme_id_ctrl_summary(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"available": False, "message": "NVMe id-ctrl JSON parsing failed."}

    firmware_version = normalize_text(payload.get("fr"))
    protocol_version = _format_nvme_version(payload.get("ver"))
    warning_temperature_c = _kelvin_to_celsius(payload.get("wctemp"))
    critical_temperature_c = _kelvin_to_celsius(payload.get("cctemp"))

    summary = {
        "available": any(
            value is not None
            for value in (
                firmware_version,
                protocol_version,
                warning_temperature_c,
                critical_temperature_c,
            )
        ),
        "firmware_version": firmware_version,
        "protocol_version": protocol_version,
        "warning_temperature_c": warning_temperature_c,
        "critical_temperature_c": critical_temperature_c,
        "message": None,
    }
    if not summary["available"]:
        summary["message"] = "No NVMe controller identity fields were returned for this disk."
    return summary


def parse_nvme_id_ns_summary(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {"available": False, "message": "NVMe id-ns JSON parsing failed."}

    summary = {
        "available": any(
            value is not None
            for value in (
                _format_nvme_eui64(payload.get("eui64")),
                _format_nvme_nguid(payload.get("nguid")),
            )
        ),
        "namespace_eui64": _format_nvme_eui64(payload.get("eui64")),
        "namespace_nguid": _format_nvme_nguid(payload.get("nguid")),
        "message": None,
    }
    if not summary["available"]:
        summary["message"] = "No NVMe namespace identity fields were returned for this disk."
    return summary


def parse_smartctl_text_enrichment(output: str) -> dict[str, Any]:
    read_cache_enabled: bool | None = None
    writeback_cache_enabled: bool | None = None
    smart_health_status: str | None = None
    protocol_version: str | None = None
    transport_protocol: str | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None
    trim_supported: bool | None = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Read Cache is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("enabled"):
                    read_cache_enabled = True
                elif lowered.startswith("disabled"):
                    read_cache_enabled = False
        elif line.startswith("Rd look-ahead is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("enabled"):
                    read_cache_enabled = True
                elif lowered.startswith("disabled"):
                    read_cache_enabled = False
        elif line.startswith("Writeback Cache is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("enabled"):
                    writeback_cache_enabled = True
                elif lowered.startswith("disabled"):
                    writeback_cache_enabled = False
        elif line.startswith("Write cache is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("enabled"):
                    writeback_cache_enabled = True
                elif lowered.startswith("disabled"):
                    writeback_cache_enabled = False
        elif line.startswith("SMART Health Status:"):
            smart_health_status = normalize_text(line.split(":", 1)[1])
        elif line.startswith("SMART overall-health self-assessment test result:"):
            smart_health_status = normalize_text(line.split(":", 1)[1])
        elif line.startswith("SATA Version is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                current_match = re.search(r"\(current:\s*([^)]+)\)", value, re.IGNORECASE)
                if current_match:
                    negotiated_link_rate = normalize_text(current_match.group(1))
                    value = normalize_text(value[: current_match.start()].rstrip(" ,")) or value
                protocol_version = value
        elif line.startswith("Transport protocol:"):
            transport_protocol = normalize_text(line.split(":", 1)[1])
        elif line.startswith("Logical Unit id:"):
            logical_unit_id = format_hex_identifier(line.split(":", 1)[1])
        elif line.startswith("SAS address ="):
            sas_address = format_hex_identifier(line.split("=", 1)[1])
        elif line.startswith("attached SAS address ="):
            attached_sas_address = format_hex_identifier(line.split("=", 1)[1])
        elif line.startswith("negotiated logical link rate:"):
            negotiated_link_rate = normalize_text(line.split(":", 1)[1])
        elif line.startswith("TRIM Command:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("available"):
                    trim_supported = True
                elif lowered.startswith("unavailable") or lowered.startswith("not supported"):
                    trim_supported = False

    return {
        "available": any(
            value is not None
            for value in (
                read_cache_enabled,
                writeback_cache_enabled,
                smart_health_status,
                protocol_version,
                transport_protocol,
                logical_unit_id,
                sas_address,
                attached_sas_address,
                negotiated_link_rate,
                trim_supported,
            )
        ),
        "read_cache_enabled": read_cache_enabled,
        "writeback_cache_enabled": writeback_cache_enabled,
        "smart_health_status": smart_health_status,
        "protocol_version": protocol_version,
        "transport_protocol": transport_protocol,
        "logical_unit_id": logical_unit_id,
        "sas_address": sas_address,
        "attached_sas_address": attached_sas_address,
        "negotiated_link_rate": negotiated_link_rate,
        "trim_supported": trim_supported,
    }


def _compose_topology_label(pool_name: str, vdev_path: list[str], vdev_class: str | None) -> str | None:
    parts = [pool_name]
    parts.extend(item for item in vdev_path if item)
    if vdev_class:
        parts.append(vdev_class)
    return " > ".join(parts) if parts else None


def _derive_top_level_vdev_label(
    vdev_class: str,
    node: dict[str, Any],
    class_index: int,
    composite_index: int | None,
) -> str:
    explicit = normalize_text(node.get("name") or node.get("vdev_name") or node.get("label"))
    if explicit:
        return explicit

    node_type = str(node.get("type") or "").lower()
    children = node.get("children") if isinstance(node.get("children"), list) else []
    if children and node_type and node_type not in {"disk", "file", "root"}:
        ordinal = composite_index if composite_index is not None else class_index
        return f"{node_type}-{ordinal}"
    if node_type and node_type not in {"", "disk", "file", "root"}:
        return f"{node_type}-{class_index}"
    return f"{vdev_class}-{class_index}"


def _derive_nested_vdev_label(node: dict[str, Any]) -> str | None:
    explicit = normalize_text(node.get("name") or node.get("vdev_name") or node.get("label"))
    if explicit:
        return explicit

    node_type = str(node.get("type") or "").lower()
    if node_type and node_type not in {"", "disk", "file", "root"}:
        return node_type
    return None


def canonicalize_ssh_command(command: str) -> str:
    """
    Normalize SSH command strings into stable parser keys.

    The app lets operators override SSH commands for least-privilege setups.
    In practice that often means using absolute paths and `sudo -n` prefixes,
    for example:

    - `/sbin/glabel status`
    - `/usr/local/sbin/zpool status -gP`
    - `sudo -n /usr/sbin/sesutil show`

    Parser lookups should not depend on the exact transport string, so we strip
    wrappers and reduce the command to the subcommand shape we care about.
    """

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return command.strip()

    while tokens and tokens[0] == "sudo":
        tokens.pop(0)
        while tokens and tokens[0].startswith("-"):
            tokens.pop(0)

    if not tokens:
        return command.strip()

    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]

    if executable == "glabel" and args[:1] == ["status"]:
        return "glabel status"
    if executable == "zpool" and args[:1] == ["status"]:
        return "zpool status -gP" if "-gP" in args[1:] else "zpool status"
    if executable == "camcontrol" and args[:1] == ["devlist"]:
        return "camcontrol devlist -v" if "-v" in args[1:] else "camcontrol devlist"
    if executable == "gmultipath" and args[:1] == ["list"]:
        return "gmultipath list"
    if executable == "sesutil" and args[:1] == ["map"]:
        return "sesutil map"
    if executable == "sesutil" and args[:1] == ["show"]:
        return "sesutil show"
    if executable == "sg_ses":
        target_device = next((arg for arg in reversed(args) if arg.startswith("/dev/sg")), None)
        has_aes_page = False
        has_ec_page = False
        for index, arg in enumerate(args):
            if arg == "-p" and index + 1 < len(args) and args[index + 1].lower() == "aes":
                has_aes_page = True
                continue
            if arg == "-p" and index + 1 < len(args) and args[index + 1].lower() == "ec":
                has_ec_page = True
                continue
            if arg.lower() == "aes":
                has_aes_page = True
                continue
            if arg.lower() == "ec":
                has_ec_page = True
                continue
        if has_aes_page and target_device:
            return f"sg_ses aes {target_device}"
        if has_ec_page and target_device:
            return f"sg_ses ec {target_device}"
    if executable == "lsblk":
        normalized_args = {arg.lower() for arg in args}
        if "-oj" in normalized_args or "-jo" in normalized_args or ("-o" in normalized_args and "-j" in normalized_args):
            return "lsblk -OJ"
    if executable == "mdadm" and args[:2] == ["--detail", "--scan"]:
        return "mdadm --detail --scan"
    if executable == "nvme" and args[:2] == ["list-subsys", "-o"] and len(args) >= 3 and args[2].lower() == "json":
        return "nvme list-subsys -o json"
    if executable == "nvme" and args[:2] == ["list", "-o"] and len(args) >= 3 and args[2].lower() == "json":
        return "nvme list -o json"
    if executable == "ubntstorage" and args[:2] == ["disk", "inspect"]:
        return "ubntstorage disk inspect"
    if executable == "ubntstorage" and args[:2] == ["space", "inspect"]:
        return "ubntstorage space inspect"
    lowered_args = [arg.lower() for arg in args]
    if executable == "esxcli":
        if lowered_args[:4] == ["storage", "core", "adapter", "list"]:
            return "esxcli storage core adapter list"
        if lowered_args[:4] == ["storage", "core", "device", "list"]:
            return "esxcli storage core device list"
        if lowered_args[:4] == ["storage", "core", "path", "list"]:
            return "esxcli storage core path list"
        if lowered_args[:3] == ["storage", "filesystem", "list"]:
            return "esxcli storage filesystem list"
        if lowered_args[:4] == ["storage", "vmfs", "extent", "list"]:
            return "esxcli storage vmfs extent list"
        if lowered_args[:4] == ["storage", "san", "sas", "list"]:
            return "esxcli storage san sas list"
    if executable in {"storcli", "storcli64"}:
        normalized_args = [arg for arg in args if arg]
        has_json = any(arg.lower() == "j" for arg in normalized_args)
        lowered_storcli_args = [arg.lower() for arg in normalized_args]
        if has_json and len(lowered_storcli_args) >= 4 and lowered_storcli_args[1:3] == ["show", "all"]:
            target = lowered_storcli_args[0]
            if target in {"/c0", "/call", "/c0/vall", "/c0/eall/sall"}:
                return f"storcli {target} show all J"
    if "/sys/kernel/debug/gpio" in command:
        return "gpio debug"

    return " ".join([executable] + args).strip()


def parse_lsblk_json(output: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    blockdevices = payload.get("blockdevices") if isinstance(payload, dict) else None
    if not isinstance(blockdevices, list):
        return []
    return [item for item in blockdevices if isinstance(item, dict)]


def parse_ubntstorage_json(output: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("items", "data", "disks", "spaces", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    return []


def parse_unifi_gpio_debug(output: str) -> dict[int, bool]:
    slot_states: dict[int, bool] = {}
    line_pattern = re.compile(
        r"gpio-\d+\s+\(.*\|hdd@(?P<slot>\d+)\s+\)\s+(?P<direction>out|in)\s+(?P<state>hi|lo|\?)",
        re.IGNORECASE,
    )
    for raw_line in output.splitlines():
        match = line_pattern.search(raw_line)
        if not match:
            continue
        if match.group("direction").lower() != "out":
            continue
        slot = int(match.group("slot"))
        state = match.group("state").lower()
        if state not in {"hi", "lo"}:
            continue
        # The UniFi UNVR GPIO dump exposes multiple hdd@N lines per slot.
        # The last writable output line is the one that toggles during fault/locate.
        slot_states[slot] = state == "hi"
    return slot_states


def _normalize_table_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def parse_esxcli_key_value_sections(output: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        if not raw_line[:1].isspace():
            current = {"id": raw_line.strip()}
            sections.append(current)
            continue
        if current is None or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        normalized_key = _normalize_table_key(key)
        if normalized_key:
            current[normalized_key] = value.strip()
    return sections


def parse_esxcli_table(output: str) -> list[dict[str, Any]]:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return []

    header_index = next(
        (
            index
            for index, line in enumerate(lines[:-1])
            if re.search(r"\s{2,}", line)
            and set(lines[index + 1].strip()) <= {"-", " "}
        ),
        None,
    )
    if header_index is None:
        return []

    headers = [_normalize_table_key(item) for item in re.split(r"\s{2,}", lines[header_index].strip())]
    rows: list[dict[str, Any]] = []
    for line in lines[header_index + 2:]:
        values = re.split(r"\s{2,}", line.strip(), maxsplit=max(0, len(headers) - 1))
        if len(values) < len(headers):
            continue
        rows.append({header: values[index].strip() for index, header in enumerate(headers) if header})
    return rows


def _parse_storcli_json(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    controllers = payload.get("Controllers")
    if not isinstance(controllers, list) or not controllers:
        return {}
    controller = controllers[0]
    if not isinstance(controller, dict):
        return {}
    response_data = controller.get("Response Data")
    if not isinstance(response_data, dict):
        response_data = {}
    return {
        "command_status": controller.get("Command Status") if isinstance(controller.get("Command Status"), dict) else {},
        "response_data": response_data,
    }


def _storcli_response_data(output: str) -> dict[str, Any]:
    parsed = _parse_storcli_json(output)
    response_data = parsed.get("response_data")
    return response_data if isinstance(response_data, dict) else {}


def parse_storcli_controller_info(output: str) -> dict[str, Any]:
    parsed = _parse_storcli_json(output)
    response_data = parsed.get("response_data")
    return response_data if isinstance(response_data, dict) else {}


def _storcli_slot_key(enclosure_id: Any, slot: Any) -> str | None:
    enclosure_text = normalize_text(str(enclosure_id) if enclosure_id is not None else None)
    slot_text = normalize_text(str(slot) if slot is not None else None)
    if not enclosure_text or not slot_text:
        return None
    return f"{enclosure_text}:{slot_text}"


def _parse_storcli_eid_slot(value: Any) -> tuple[str | None, int | None, str | None]:
    text = normalize_text(str(value) if value is not None else None)
    if not text or ":" not in text:
        return None, None, None
    enclosure_text, slot_text = text.split(":", 1)
    slot = _coerce_int_like(slot_text)
    if slot is None:
        return normalize_text(enclosure_text), None, None
    return normalize_text(enclosure_text), slot, f"{normalize_text(enclosure_text)}:{slot}"


def _extract_storcli_drive_path(value: str) -> tuple[str | None, str | None, int | None, str | None]:
    match = re.search(r"/(?P<controller>c\d+)/e(?P<enclosure>\d+)/s(?P<slot>\d+)", value, re.IGNORECASE)
    if not match:
        return None, None, None, None
    enclosure_id = match.group("enclosure")
    slot = int(match.group("slot"))
    return match.group("controller").lower(), enclosure_id, slot, f"{enclosure_id}:{slot}"


def _flatten_storcli_detail(value: Any) -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    visit(child)
                else:
                    flattened[str(key)] = child
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return flattened


def _first_detail_value(payload: dict[str, Any], *keys: str) -> Any:
    lowered = {key.lower(): value for key, value in payload.items()}
    for key in keys:
        if key in payload:
            return payload[key]
        lowered_value = lowered.get(key.lower())
        if lowered_value is not None:
            return lowered_value
    return None


def _storcli_int(value: Any) -> int | None:
    return _coerce_int_like(value)


def _storcli_temperature_c(value: Any) -> int | None:
    return _coerce_int_like(value)


def _collect_storcli_drive_details(response_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for key, value in response_data.items():
        if not isinstance(key, str) or not key.lower().startswith("drive /c"):
            continue
        controller_id, enclosure_id, slot, slot_key = _extract_storcli_drive_path(key)
        if not slot_key:
            continue
        flattened = _flatten_storcli_detail(value)
        if controller_id:
            flattened["controller_id"] = controller_id
        if enclosure_id:
            flattened["enclosure_id"] = enclosure_id
        if slot is not None:
            flattened["slot"] = slot
        details.setdefault(slot_key, {}).update(flattened)
    return details


def _collect_storcli_physical_drive_rows(response_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = response_data.get("Drive Information") or response_data.get("PD LIST") or []
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]

    fallback_rows: list[dict[str, Any]] = []
    for key, value in response_data.items():
        if not isinstance(key, str) or not re.match(r"^Drive\s+/c\d+/e\d+/s\d+$", key, re.IGNORECASE):
            continue
        if isinstance(value, list):
            fallback_rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            fallback_rows.append(value)
    return fallback_rows


def parse_storcli_physical_drives(output: str) -> list[dict[str, Any]]:
    response_data = _storcli_response_data(output)
    rows = _collect_storcli_physical_drive_rows(response_data)
    details_by_slot = _collect_storcli_drive_details(response_data)
    drives: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enclosure_id, slot, slot_key = _parse_storcli_eid_slot(row.get("EID:Slt") or row.get("EID:Slt "))
        if slot_key is None:
            continue
        detail = details_by_slot.get(slot_key, {})
        serial = normalize_text(
            str(
                _first_detail_value(detail, "SN", "Serial Number", "Inquiry Data")
                or row.get("SN")
                or ""
            )
        )
        connector_name = normalize_text(
            str(_first_detail_value(detail, "Connector Name", "Connector") or row.get("Cntrl") or "")
        )
        connected_port = normalize_text(
            str(_first_detail_value(detail, "Connected Port Number(path)", "Connected Port Number", "Port") or "")
        )
        media_error_count = _storcli_int(
            _first_detail_value(detail, "Media Error Count", "Media Errors", "Media_Error_Count")
        )
        other_error_count = _storcli_int(
            _first_detail_value(detail, "Other Error Count", "Other Errors", "Other_Error_Count")
        )
        predictive_failure_count = _storcli_int(
            _first_detail_value(detail, "Predictive Failure Count", "Predictive Failure", "Predictive_Failure_Count")
        )
        smart_alert = normalize_text(
            str(
                _first_detail_value(
                    detail,
                    "S.M.A.R.T alert flagged by drive",
                    "SMART alert flagged by drive",
                    "SMART Alert",
                )
                or ""
            )
        )
        firmware = normalize_text(
            str(_first_detail_value(detail, "Firmware Revision", "Firmware", "F/W") or row.get("F/W") or "")
        )
        link_speed = normalize_text(
            str(_first_detail_value(detail, "Link Speed", "Negotiated Link Speed", "Drive Speed") or row.get("Sp") or "")
        )
        drives.append(
            {
                "slot_key": slot_key,
                "enclosure_id": enclosure_id,
                "slot": slot,
                "controller_id": normalize_text(str(detail.get("controller_id") or row.get("Ctl") or "c0")),
                "device_id": normalize_text(str(row.get("DID") if row.get("DID") is not None else "")),
                "state": normalize_text(str(row.get("State") or "")),
                "drive_group": normalize_text(str(row.get("DG") if row.get("DG") is not None else "")),
                "size": normalize_text(str(row.get("Size") or "")),
                "interface": normalize_text(str(row.get("Intf") or "")),
                "media": normalize_text(str(row.get("Med") or "")),
                "sector_size": normalize_text(str(row.get("SeSz") or row.get("SeSz ") or "")),
                "model": normalize_text(str(row.get("Model") or _first_detail_value(detail, "Model Number") or "")),
                "serial": serial,
                "firmware": firmware,
                "temperature_c": _storcli_temperature_c(
                    _first_detail_value(detail, "Drive Temperature", "Temperature", "Drive Temperature(C)")
                ),
                "media_errors": media_error_count,
                "other_errors": other_error_count,
                "predictive_errors": predictive_failure_count,
                "smart_alert": smart_alert,
                "connector_name": connector_name,
                "connected_port": connected_port,
                "link_speed": link_speed,
                "raw_size": normalize_text(str(_first_detail_value(detail, "Raw Size") or "")),
                "unmap_capable": normalize_text(str(_first_detail_value(detail, "Unmap Capable") or "")),
                "raw": row,
                "detail": detail,
            }
        )
    return drives


def _collect_storcli_virtual_drive_details(response_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for key, value in response_data.items():
        if not isinstance(key, str):
            continue
        match = re.search(r"/c\d+/v(?P<vd>\d+)", key, re.IGNORECASE)
        if not match:
            match = re.search(r"^VD(?P<vd>\d+)\s+Properties$", key, re.IGNORECASE)
        if not match:
            continue
        flattened = _flatten_storcli_detail(value)
        details.setdefault(match.group("vd"), {}).update(flattened)
    return details


def _collect_storcli_virtual_drive_rows(response_data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = response_data.get("VD LIST") or response_data.get("Virtual Drives") or []
    if isinstance(rows, list) and rows:
        return [row for row in rows if isinstance(row, dict)]

    fallback_rows: list[dict[str, Any]] = []
    for key, value in response_data.items():
        if not isinstance(key, str) or not re.match(r"^/c\d+/v\d+$", key, re.IGNORECASE):
            continue
        if isinstance(value, list):
            fallback_rows.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            fallback_rows.append(value)
    return fallback_rows


def parse_storcli_virtual_drives(output: str) -> list[dict[str, Any]]:
    response_data = _storcli_response_data(output)
    rows = _collect_storcli_virtual_drive_rows(response_data)
    details_by_vd = _collect_storcli_virtual_drive_details(response_data)
    virtual_drives: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_dg_vd = normalize_text(str(row.get("DG/VD") or row.get("DG VD") or ""))
        vd_id = None
        if raw_dg_vd and "/" in raw_dg_vd:
            vd_id = raw_dg_vd.split("/", 1)[1]
        if vd_id is None:
            vd_id = normalize_text(str(row.get("VD") if row.get("VD") is not None else ""))
        detail = details_by_vd.get(vd_id or "", {})
        pd_rows = []
        for key, value in response_data.items():
            if isinstance(key, str) and re.search(rf"\bPDs\s+for\s+VD\s+{re.escape(vd_id or '')}\b", key, re.IGNORECASE):
                if isinstance(value, list):
                    pd_rows.extend(item for item in value if isinstance(item, dict))
        physical_drives = []
        for pd_row in pd_rows:
            enclosure_id, slot, slot_key = _parse_storcli_eid_slot(pd_row.get("EID:Slt") or pd_row.get("EID:Slt "))
            if slot_key:
                physical_drives.append(
                    {
                        "slot_key": slot_key,
                        "enclosure_id": enclosure_id,
                        "slot": slot,
                        "raw": pd_row,
                    }
                )
        virtual_drives.append(
            {
                "vd_id": vd_id,
                "name": normalize_text(str(row.get("Name") or _first_detail_value(detail, "Name") or "")),
                "raid": normalize_text(str(row.get("TYPE") or row.get("Type") or "")),
                "state": normalize_text(str(row.get("State") or "")),
                "size": normalize_text(str(row.get("Size") or "")),
                "scsi_naa": normalize_text(str(_first_detail_value(detail, "SCSI NAA Id", "SCSI NAA ID") or "")),
                "physical_drives": physical_drives,
                "raw": row,
                "detail": detail,
            }
        )
    return virtual_drives


def parse_mdadm_detail_scan(output: str) -> dict[str, LinuxMdArray]:
    arrays: dict[str, LinuxMdArray] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("ARRAY "):
            continue
        match = re.match(
            r"^ARRAY\s+(?P<device>\S+)(?:\s+metadata=(?P<metadata>\S+))?(?:\s+name=(?P<name>\S+))?(?:\s+UUID=(?P<uuid>\S+))?",
            line,
        )
        if not match:
            continue
        device_path = normalize_text(match.group("device"))
        if not device_path:
            continue
        array = LinuxMdArray(
            device_path=device_path,
            name=normalize_text(match.group("name")),
            uuid=normalize_text(match.group("uuid")),
            metadata=normalize_text(match.group("metadata")),
        )
        arrays[device_path.lower()] = array
        arrays[device_path.rsplit("/", 1)[-1].lower()] = array
    return arrays


def parse_nvme_list_subsys_json(output: str) -> dict[str, dict[str, str | None]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}

    subsystems = payload.get("Subsystems") if isinstance(payload, dict) else None
    if not isinstance(subsystems, list):
        return {}

    controllers: dict[str, dict[str, str | None]] = {}
    for subsystem in subsystems:
        if not isinstance(subsystem, dict):
            continue
        nqn = normalize_text(subsystem.get("NQN"))
        paths = subsystem.get("Paths") if isinstance(subsystem.get("Paths"), list) else []
        for path in paths:
            if not isinstance(path, dict):
                continue
            controller_name = normalize_text(path.get("Name"))
            if not controller_name:
                continue
            controllers[controller_name.lower()] = {
                "controller_name": controller_name.lower(),
                "transport": normalize_text(path.get("Transport")),
                "address": normalize_text(path.get("Address")),
                "state": normalize_text(path.get("State")),
                "nqn": nqn,
            }
    return controllers


def parse_ssh_outputs(
    outputs: dict[str, str],
    slot_count: int,
    enclosure_filter: str | None,
    selected_enclosure_id: str | None = None,
) -> ParsedSSHData:
    parsed = ParsedSSHData()
    normalized_outputs: dict[str, str] = {}

    for command, output in outputs.items():
        normalized_outputs[canonicalize_ssh_command(command)] = output

    if normalized_outputs.get("glabel status"):
        parsed.glabel = parse_glabel_status(normalized_outputs["glabel status"])
    if normalized_outputs.get("zpool status -gP"):
        parsed.zpool_members = parse_zpool_status(normalized_outputs["zpool status -gP"])
    if normalized_outputs.get("lsblk -OJ"):
        parsed.linux_blockdevices = parse_lsblk_json(normalized_outputs["lsblk -OJ"])
    if normalized_outputs.get("mdadm --detail --scan"):
        parsed.linux_mdadm_arrays = parse_mdadm_detail_scan(normalized_outputs["mdadm --detail --scan"])
    if normalized_outputs.get("nvme list-subsys -o json"):
        parsed.linux_nvme_subsystems = parse_nvme_list_subsys_json(normalized_outputs["nvme list-subsys -o json"])
    if normalized_outputs.get("ubntstorage disk inspect"):
        parsed.ubntstorage_disks = parse_ubntstorage_json(normalized_outputs["ubntstorage disk inspect"])
    if normalized_outputs.get("ubntstorage space inspect"):
        parsed.ubntstorage_spaces = parse_ubntstorage_json(normalized_outputs["ubntstorage space inspect"])
    if normalized_outputs.get("gpio debug"):
        parsed.unifi_led_states = parse_unifi_gpio_debug(normalized_outputs["gpio debug"])
    if normalized_outputs.get("esxcli storage core adapter list"):
        parsed.esxi_storage_adapters = parse_esxcli_table(normalized_outputs["esxcli storage core adapter list"])
    if normalized_outputs.get("esxcli storage core device list"):
        parsed.esxi_storage_devices = parse_esxcli_key_value_sections(normalized_outputs["esxcli storage core device list"])
    if normalized_outputs.get("esxcli storage filesystem list"):
        parsed.esxi_filesystems = parse_esxcli_table(normalized_outputs["esxcli storage filesystem list"])
    if normalized_outputs.get("esxcli storage vmfs extent list"):
        parsed.esxi_vmfs_extents = parse_esxcli_table(normalized_outputs["esxcli storage vmfs extent list"])
    if normalized_outputs.get("esxcli storage san sas list"):
        parsed.esxi_sas_adapters = parse_esxcli_key_value_sections(normalized_outputs["esxcli storage san sas list"])
    if normalized_outputs.get("storcli /c0 show all J"):
        parsed.esxi_storcli_controller = parse_storcli_controller_info(normalized_outputs["storcli /c0 show all J"])
    if normalized_outputs.get("storcli /c0/vall show all J"):
        parsed.esxi_storcli_virtual_drives = parse_storcli_virtual_drives(normalized_outputs["storcli /c0/vall show all J"])
    if normalized_outputs.get("storcli /c0/eall/sall show all J"):
        parsed.esxi_storcli_physical_drives = parse_storcli_physical_drives(normalized_outputs["storcli /c0/eall/sall show all J"])
    if normalized_outputs.get("gmultipath list"):
        parsed.multipath_info = parse_gmultipath_list(normalized_outputs["gmultipath list"])
    if normalized_outputs.get("camcontrol devlist"):
        camcontrol_info = parse_camcontrol_devlist(normalized_outputs["camcontrol devlist"])
        parsed.camcontrol_models = camcontrol_info.models
        parsed.camcontrol_controllers = camcontrol_info.controllers
        parsed.camcontrol_peer_devices = camcontrol_info.peer_devices
    if normalized_outputs.get("camcontrol devlist -v"):
        camcontrol_info = parse_camcontrol_devlist(normalized_outputs["camcontrol devlist -v"])
        parsed.camcontrol_models = camcontrol_info.models
        parsed.camcontrol_controllers = camcontrol_info.controllers
        parsed.camcontrol_peer_devices = camcontrol_info.peer_devices

    if normalized_outputs.get("sesutil map"):
        ses_map_enclosures = parse_sesutil_map(normalized_outputs["sesutil map"])
        parsed.ses_enclosures.extend(ses_map_enclosures)
        parsed.ses_slot_candidates, parsed.ses_selected_meta = build_slot_candidates_from_ses_enclosures(
            ses_map_enclosures,
            slot_count,
            enclosure_filter,
            selected_enclosure_id,
        )

    if normalized_outputs.get("sesutil show"):
        ses_show_enclosures = parse_sesutil_show_enclosures(normalized_outputs["sesutil show"])
        parsed.ses_enclosures.extend(ses_show_enclosures)
        show_candidates, show_meta = build_slot_candidates_from_ses_enclosures(
            ses_show_enclosures,
            slot_count,
            enclosure_filter,
            selected_enclosure_id,
        )
        parsed.ses_slot_candidates = merge_slot_candidate_maps(parsed.ses_slot_candidates, show_candidates)
        parsed.ses_selected_meta = merge_enclosure_meta(parsed.ses_selected_meta, show_meta)

    for command_key, output in normalized_outputs.items():
        if not command_key.startswith("sg_ses aes /dev/sg"):
            continue
        enclosure = parse_sg_ses_aes(output, command_key)
        if enclosure:
            parsed.ses_enclosures.append(enclosure)

    for command_key, output in normalized_outputs.items():
        if not command_key.startswith("sg_ses ec /dev/sg"):
            continue
        enclosure = parse_sg_ses_enclosure_status(output, command_key)
        if enclosure:
            parsed.ses_enclosures.append(enclosure)

    if parsed.ses_enclosures:
        parsed.ses_enclosures = _merge_ses_enclosures(parsed.ses_enclosures)
        sg_candidates, sg_meta = build_slot_candidates_from_ses_enclosures(
            parsed.ses_enclosures,
            slot_count,
            enclosure_filter,
            selected_enclosure_id,
        )
        parsed.ses_slot_candidates = merge_slot_candidate_maps(parsed.ses_slot_candidates, sg_candidates)
        parsed.ses_selected_meta = merge_enclosure_meta(parsed.ses_selected_meta, sg_meta)

    for slot, payload in parsed.ses_slot_candidates.items():
        device_hint = normalize_device_name(payload.get("device_hint"))
        if device_hint:
            parsed.ses_slot_to_device[slot] = device_hint

    return parsed
