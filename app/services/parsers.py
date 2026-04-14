from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any


DEVICE_REGEX = re.compile(
    r"(?P<device>(?:/dev/)?(?:(?:da|ada|sd|nvd)\d+|nvme\d+(?:n\d+)?|multipath/disk[0-9A-Za-z-]+)(?:p\d+)?)",
    re.IGNORECASE,
)
GPTID_REGEX = re.compile(r"(?P<gptid>(?:/dev/)?gptid/[A-Za-z0-9\-_.]+)", re.IGNORECASE)
GUID_REGEX = re.compile(r"^[0-9]{16,}$")
SLOT_REGEX = re.compile(r"(?:slot|bay|element)\D{0,4}(?P<slot>\d{1,3})", re.IGNORECASE)
HEX_VALUE_REGEX = re.compile(r"^(?:0x)?(?P<value>[0-9a-fA-F]+)$")


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
    ses_slot_candidates: dict[int, dict[str, Any]] = field(default_factory=dict)
    ses_selected_meta: dict[str, str | None] = field(default_factory=dict)
    ses_enclosures: list["SESMapEnclosure"] = field(default_factory=list)


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
    sas_device_type: str | None = None


@dataclass(slots=True)
class SESMapEnclosure:
    ses_device: str | None = None
    enclosure_id: str | None = None
    enclosure_name: str | None = None
    enclosure_label: str | None = None
    layout_rows: int | None = None
    layout_columns: int | None = None
    slot_layout: list[list[int]] | None = None
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


def normalize_hex_identifier(value: str | None) -> str | None:
    if not value:
        return None
    match = HEX_VALUE_REGEX.match(value.strip())
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
    return normalize_text(value)


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

    for line in output.splitlines():
        bus_match = re.match(r"^(?:scbus|umass-sim)\d+\s+on\s+(?P<controller>\S+)\s+bus\s+\d+:", line.strip(), re.IGNORECASE)
        if bus_match:
            current_controller = normalize_text(bus_match.group("controller"))
            continue

        match = re.search(r"<(?P<model>[^>]+)>.*\((?P<devices>[^)]+)\)", line)
        if not match:
            continue

        model = match.group("model").strip()
        for device in match.group("devices").split(","):
            if not DEVICE_REGEX.search(device.strip()):
                continue
            normalized = normalize_device_name(device)
            if normalized:
                info.models[normalized.lower()] = model
                if current_controller:
                    info.controllers[normalized.lower()] = current_controller
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

    return [item for item in enclosures if item.slots]


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

    return [item for item in enclosures if item.slots]


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

    if not enclosure.slots:
        return None

    slot_count = max(enclosure.slots) + 1
    (
        enclosure.enclosure_label,
        enclosure.layout_rows,
        enclosure.layout_columns,
        enclosure.slot_layout,
    ) = _infer_scale_enclosure_profile(
        enclosure,
        slot_count,
    )
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
            continue

        ident_match = re.search(r"\bIdent=(?P<ident>[01])\b", stripped)
        if ident_match:
            current_slot.identify_active = ident_match.group("ident") == "1"
            continue

    if not enclosure.slots:
        return None

    slot_count = max(enclosure.slots) + 1
    (
        enclosure.enclosure_label,
        enclosure.layout_rows,
        enclosure.layout_columns,
        enclosure.slot_layout,
    ) = _infer_scale_enclosure_profile(
        enclosure,
        slot_count,
    )
    return enclosure


def _extract_sg_ses_device(command: str | None) -> str | None:
    if not command:
        return None
    match = re.search(r"(/dev/sg\d+)", command)
    return normalize_text(match.group(1)) if match else None


def _infer_scale_enclosure_profile(
    enclosure: SESMapEnclosure,
    slot_count: int,
) -> tuple[str | None, int | None, int | None, list[list[int]] | None]:
    name = (enclosure.enclosure_name or "").lower()
    ses_device = (enclosure.ses_device or "").lower()

    if "sas3x40" in name or slot_count == 24 or ses_device.endswith("sg27"):
        # Front 24-bay CryoStorage chassis view: 4 columns across, 6 rows tall.
        # The operator-facing front view counts each column bottom-to-top, so
        # slot 0 sits at the bottom of column 1 and slot 5 sits at the top.
        # Each vertical 6-slot column corresponds to one front vdev.
        return (
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
        return "60 Bay Shelf", 4, 15, None
    if slot_count == 30:
        return "30 Bay Shelf", 3, 10, None

    rows = max(1, min(4, slot_count))
    columns = max(1, (slot_count + rows - 1) // rows)
    return f"{slot_count} Bay SES", rows, columns, None


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
    seen: set[tuple[str | None, int | None]] = set()
    for item in base + overlay:
        if not isinstance(item, dict):
            continue
        ses_device = normalize_text(item.get("ses_device"))
        ses_element_id = item.get("ses_element_id")
        pair = (ses_device, ses_element_id if isinstance(ses_element_id, int) else None)
        if not pair[0] or pair[1] is None or pair in seen:
            continue
        seen.add(pair)
        merged.append(
            {
                "ses_device": pair[0],
                "ses_element_id": pair[1],
                "ses_slot_number": item.get("ses_slot_number")
                if isinstance(item.get("ses_slot_number"), int)
                else None,
            }
        )
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
                    seen: set[tuple[str | None, int | None]] = set()
                    for item in existing + value:
                        if not isinstance(item, dict):
                            continue
                        pair = (normalize_text(item.get("ses_device")), item.get("ses_element_id"))
                        if pair in seen:
                            continue
                        seen.add(pair)
                        combined.append(
                            {
                                "ses_device": pair[0],
                                "ses_element_id": pair[1],
                                "ses_slot_number": item.get("ses_slot_number")
                                if isinstance(item.get("ses_slot_number"), int)
                                else None,
                            }
                        )
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
                "sas_device_type": slot.sas_device_type,
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
    logical_unit_id = format_hex_identifier(payload.get("logical_unit_id"))
    power_on_hours = power_on_time.get("hours") if isinstance(power_on_time.get("hours"), int) else None
    latest_test_type: str | None = None
    latest_test_status: str | None = None
    latest_test_lifetime_hours: int | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None

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
                latest_test_type,
                latest_test_status,
                latest_test_lifetime_hours,
                logical_block_size if isinstance(logical_block_size, int) else None,
                physical_block_size if isinstance(physical_block_size, int) else None,
                rotation_rate,
                form_factor,
                transport_protocol,
                logical_unit_id,
                sas_address,
                attached_sas_address,
                negotiated_link_rate,
            )
        ),
        "temperature_c": current_temperature,
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
        "rotation_rate_rpm": rotation_rate,
        "form_factor": form_factor,
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


def parse_smartctl_text_enrichment(output: str) -> dict[str, Any]:
    read_cache_enabled: bool | None = None
    writeback_cache_enabled: bool | None = None
    transport_protocol: str | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None

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
        elif line.startswith("Writeback Cache is:"):
            value = normalize_text(line.split(":", 1)[1])
            if value:
                lowered = value.lower()
                if lowered.startswith("enabled"):
                    writeback_cache_enabled = True
                elif lowered.startswith("disabled"):
                    writeback_cache_enabled = False
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

    return {
        "available": any(
            value is not None
            for value in (
                read_cache_enabled,
                writeback_cache_enabled,
                transport_protocol,
                logical_unit_id,
                sas_address,
                attached_sas_address,
                negotiated_link_rate,
            )
        ),
        "read_cache_enabled": read_cache_enabled,
        "writeback_cache_enabled": writeback_cache_enabled,
        "transport_protocol": transport_protocol,
        "logical_unit_id": logical_unit_id,
        "sas_address": sas_address,
        "attached_sas_address": attached_sas_address,
        "negotiated_link_rate": negotiated_link_rate,
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

    return " ".join([executable] + args).strip()


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
    if normalized_outputs.get("gmultipath list"):
        parsed.multipath_info = parse_gmultipath_list(normalized_outputs["gmultipath list"])
    if normalized_outputs.get("camcontrol devlist"):
        camcontrol_info = parse_camcontrol_devlist(normalized_outputs["camcontrol devlist"])
        parsed.camcontrol_models = camcontrol_info.models
        parsed.camcontrol_controllers = camcontrol_info.controllers
    if normalized_outputs.get("camcontrol devlist -v"):
        camcontrol_info = parse_camcontrol_devlist(normalized_outputs["camcontrol devlist -v"])
        parsed.camcontrol_models = camcontrol_info.models
        parsed.camcontrol_controllers = camcontrol_info.controllers

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

    for slot, payload in parsed.ses_slot_candidates.items():
        device_hint = normalize_device_name(payload.get("device_hint"))
        if device_hint:
            parsed.ses_slot_to_device[slot] = device_hint

    return parsed
