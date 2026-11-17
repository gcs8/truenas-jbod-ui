"""StorCLI JSON readers: controller info, physical drives, the drive census check and virtual drives.

Moved out of ``app.services.parsers``, which re-exports every name defined here.
This module must not import ``app.services.parsers``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import normalize_text, normalize_value_text


def _coerce_int_like(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
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


def _storcli_controller_id(
    controller: dict[str, Any],
    response_data: dict[str, Any],
    controller_index: int,
    default_controller_id: str | None = None,
) -> str:
    command_status = controller.get("Command Status")
    if isinstance(command_status, dict):
        for key in ("Controller", "Controller Number", "Ctl"):
            value = normalize_text(
                str(command_status.get(key)) if command_status.get(key) is not None else None
            )
            if value:
                return value.lower() if value.lower().startswith("c") else f"c{value}"
    for key in response_data:
        if not isinstance(key, str):
            continue
        match = re.search(r"/c(?P<controller>\d+)(?:/|\b)", key, re.IGNORECASE)
        if match:
            return f"c{match.group('controller')}"
    return normalize_text(default_controller_id) or f"c{controller_index}"


def _parse_storcli_controllers(
    output: str,
    warnings: list[str] | None = None,
    default_controller_id: str | None = None,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    controllers = payload.get("Controllers")
    if not isinstance(controllers, list) or not controllers:
        return []
    parsed: list[dict[str, Any]] = []
    for controller_index, controller in enumerate(controllers):
        if not isinstance(controller, dict):
            if warnings is not None:
                warnings.append(
                    f"StorCLI controller block {controller_index} was invalid and was not used: "
                    "controller entry was not an object."
                )
            continue
        raw_command_status = controller.get("Command Status")
        command_status: dict[str, Any] = (
            raw_command_status if isinstance(raw_command_status, dict) else {}
        )
        response_data = controller.get("Response Data")
        if not isinstance(response_data, dict):
            if warnings is not None:
                detail = normalize_text(
                    str(command_status.get("Description") or command_status.get("Status") or "")
                ) or "Response Data was not an object"
                warnings.append(
                    f"StorCLI controller block {controller_index} was invalid and was not used: "
                    f"{detail.rstrip('.')}."
                )
            continue
        parsed.append(
            {
                "controller_id": _storcli_controller_id(
                    controller,
                    response_data,
                    controller_index,
                    default_controller_id if len(controllers) == 1 else None,
                ),
                "controller_index": controller_index,
                "command_status": command_status,
                "response_data": response_data,
            }
        )
    return parsed


def _parse_storcli_json(output: str) -> dict[str, Any]:
    controllers = _parse_storcli_controllers(output)
    return controllers[0] if controllers else {}


def parse_storcli_controller_info(output: str) -> dict[str, Any]:
    parsed = _parse_storcli_json(output)
    response_data = parsed.get("response_data")
    return response_data if isinstance(response_data, dict) else {}


def _storcli_slot_key(enclosure_id: Any, slot: Any) -> str | None:
    enclosure_text = normalize_value_text(enclosure_id)
    slot_text = normalize_value_text(slot)
    if not enclosure_text or not slot_text:
        return None
    return f"{enclosure_text}:{slot_text}"


def _parse_storcli_eid_slot(value: Any) -> tuple[str | None, int | None, str | None]:
    text = normalize_value_text(value)
    if not text or ":" not in text:
        return None, None, None
    enclosure_text, slot_text = text.split(":", 1)
    enclosure_id = normalize_text(enclosure_text)
    slot = _coerce_int_like(slot_text)
    if slot is None:
        return enclosure_id, None, None
    return enclosure_id, slot, _storcli_slot_key(enclosure_id, slot)


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
        if controller_id:
            details.setdefault(f"{controller_id}:{slot_key}", {}).update(flattened)
        details.setdefault(slot_key, {}).update(flattened)
    return details


def _collect_storcli_physical_drive_rows(response_data: dict[str, Any]) -> list[Any]:
    # Every entry, objects or not: the parser skips the others, and the census
    # check refuses a reply that has any (#1218).
    rows = response_data.get("Drive Information") or response_data.get("PD LIST") or []
    if isinstance(rows, list) and rows:
        return list(rows)

    fallback_rows: list[Any] = []
    for key, value in response_data.items():
        if not isinstance(key, str) or not re.match(r"^Drive\s+/c\d+/e\d+/s\d+$", key, re.IGNORECASE):
            continue
        if isinstance(value, list):
            fallback_rows.extend(value)
        elif isinstance(value, dict):
            fallback_rows.append(value)
    return fallback_rows


def _parse_storcli_physical_drive_response(
    response_data: dict[str, Any],
    *,
    controller_id: str,
    controller_index: int,
) -> list[dict[str, Any]]:
    rows = _collect_storcli_physical_drive_rows(response_data)
    details_by_slot = _collect_storcli_drive_details(response_data)
    drives: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enclosure_id, slot, slot_key = _parse_storcli_eid_slot(row.get("EID:Slt") or row.get("EID:Slt "))
        if slot_key is None:
            continue
        row_controller_id = normalize_text(
            str(row.get("Ctl") or row.get("Controller") or controller_id)
        )
        detail = (
            details_by_slot.get(f"{row_controller_id.lower()}:{slot_key}", {})
            if row_controller_id
            else {}
        ) or details_by_slot.get(slot_key, {})
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
        media_error_count = _coerce_int_like(
            _first_detail_value(detail, "Media Error Count", "Media Errors", "Media_Error_Count")
        )
        other_error_count = _coerce_int_like(
            _first_detail_value(detail, "Other Error Count", "Other Errors", "Other_Error_Count")
        )
        predictive_failure_count = _coerce_int_like(
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
                "controller_id": normalize_text(
                    str(detail.get("controller_id") or row_controller_id or controller_id)
                ),
                "controller_index": controller_index,
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
                "temperature_c": _coerce_int_like(
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


def parse_storcli_physical_drives(
    output: str,
    warnings: list[str] | None = None,
    default_controller_id: str | None = None,
) -> list[dict[str, Any]]:
    drives: list[dict[str, Any]] = []
    for controller in _parse_storcli_controllers(
        output,
        warnings,
        default_controller_id,
    ):
        drives.extend(
            _parse_storcli_physical_drive_response(
                controller["response_data"],
                controller_id=controller["controller_id"],
                controller_index=controller["controller_index"],
            )
        )
    return drives


def storcli_physical_drive_list_complete(output: str) -> bool:
    """True when a `/eall/sall show all J` reply is taken as the drive census (#1218).

    Every controller block answered `Success`, the drive parser read at least
    one drive record (from the `Drive Information`/`PD LIST` table or from
    `Drive /cN/eM/sK` entries), every record names an enclosure and slot, and
    no `Drive /cN/eM/sK` entry names a drive the records do not; a slot it
    lists no drive in is then taken as empty. A failed or unparsed reply, one
    with no drive record, a record without its enclosure and slot, or a
    `Drive /cN/eM/sK` entry for a drive the records do not list proves no
    absence. The check runs one way only (the records may list drives no such
    entry names), and nothing compares the records with the controller's own
    drive count or enclosure list: a well-formed reply that leaves drives out
    is taken as the census, as before #1218.
    """
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False
    controllers = payload.get("Controllers") if isinstance(payload, dict) else None
    if not isinstance(controllers, list) or not controllers:
        return False
    for controller in controllers:
        status = controller.get("Command Status") if isinstance(controller, dict) else None
        response = controller.get("Response Data") if isinstance(controller, dict) else None
        if not isinstance(status, dict) or str(status.get("Status") or "").strip().casefold() != "success":
            return False
        if not isinstance(response, dict):
            return False
        # The records the drive parser reads, in either form it accepts.
        listed = [
            _parse_storcli_eid_slot(row.get("EID:Slt") or row.get("EID:Slt "))[2] if isinstance(row, dict) else None
            for row in _collect_storcli_physical_drive_rows(response)
        ]
        named = {
            _storcli_slot_key(detail.get("enclosure_id"), detail.get("slot"))
            for detail in _collect_storcli_drive_details(response).values()
        }
        if not listed or None in listed or not named <= set(listed):
            return False
    return True


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


_STORCLI_PD_SECTION_HEADING_PATTERN = re.compile(r"\bPDs\s+for\s+VD\s+", re.IGNORECASE)
_STORCLI_PD_SECTION_VD_PATTERN = re.compile(r"\w*")


def _collect_storcli_pd_sections(response_data: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """Each "PDs for VD <n>" section value, indexed once by its VD number.

    The VD loop used to search every response key again for each VD (#1216).
    A decimal VD number names a section exactly when the word that follows the
    heading is that number, so one pass over the keys answers every VD, in
    key order, with the same sections as the per-VD search.
    """

    sections: dict[str, list[list[Any]]] = {}
    for key, value in response_data.items():
        if not isinstance(key, str):
            continue
        named: set[str] = set()
        for heading in _STORCLI_PD_SECTION_HEADING_PATTERN.finditer(key):
            vd_id = _STORCLI_PD_SECTION_VD_PATTERN.match(key, heading.end()).group(0)
            if vd_id in named or not (vd_id.isascii() and vd_id.isdigit()):
                continue
            named.add(vd_id)
            if isinstance(value, list):
                sections.setdefault(vd_id, []).append(value)
    return sections


def _parse_storcli_virtual_drive_response(
    response_data: dict[str, Any],
    *,
    controller_id: str,
    controller_index: int,
) -> list[dict[str, Any]]:
    rows = _collect_storcli_virtual_drive_rows(response_data)
    details_by_vd = _collect_storcli_virtual_drive_details(response_data)
    pd_sections_by_vd = _collect_storcli_pd_sections(response_data)
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
        # Only a decimal VD number names its sections. A row without one is
        # given no section rather than every section in the response.
        pd_rows = [
            item for section in pd_sections_by_vd.get(vd_id or "", ()) for item in section if isinstance(item, dict)
        ]
        physical_drives = []
        for pd_row in pd_rows:
            enclosure_id, slot, slot_key = _parse_storcli_eid_slot(pd_row.get("EID:Slt") or pd_row.get("EID:Slt "))
            if slot_key:
                physical_drives.append(
                    {
                        "controller_id": controller_id,
                        "slot_key": slot_key,
                        "enclosure_id": enclosure_id,
                        "slot": slot,
                        "raw": pd_row,
                    }
                )
        virtual_drives.append(
            {
                "controller_id": controller_id,
                "controller_index": controller_index,
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


def parse_storcli_virtual_drives(
    output: str,
    warnings: list[str] | None = None,
    default_controller_id: str | None = None,
) -> list[dict[str, Any]]:
    virtual_drives: list[dict[str, Any]] = []
    for controller in _parse_storcli_controllers(
        output,
        warnings,
        default_controller_id,
    ):
        virtual_drives.extend(
            _parse_storcli_virtual_drive_response(
                controller["response_data"],
                controller_id=controller["controller_id"],
                controller_index=controller["controller_index"],
            )
        )
    return virtual_drives
