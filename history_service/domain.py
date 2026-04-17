from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


FIELD_LABELS = {
    "present": "Present",
    "state": "State",
    "identify_active": "Identify Active",
    "health": "Health",
    "device_name": "Device",
    "serial": "Serial",
    "model": "Model",
    "gptid": "Persistent ID",
    "pool_name": "Pool",
    "vdev_name": "Vdev",
    "topology_label": "Topology",
    "multipath_device": "Multipath Device",
    "multipath_mode": "Multipath Mode",
    "multipath_state": "Multipath State",
    "multipath_lunid": "LUN ID",
    "multipath_primary_path": "Primary Path",
    "multipath_alternate_path": "Alternate Path",
    "multipath_active_paths": "Active Paths",
    "multipath_passive_paths": "Passive Paths",
    "multipath_failed_paths": "Failed Paths",
    "multipath_other_paths": "Other Paths",
    "multipath_active_controllers": "Active Controllers",
    "multipath_passive_controllers": "Passive Controllers",
    "multipath_failed_controllers": "Failed Controllers",
}

EVENT_GROUPS = {
    "slot_state_changed": ("present", "state", "identify_active", "health"),
    "slot_identity_changed": ("device_name", "serial", "model", "gptid"),
    "slot_topology_changed": ("pool_name", "vdev_name", "topology_label"),
    "slot_multipath_changed": (
        "multipath_device",
        "multipath_mode",
        "multipath_state",
        "multipath_lunid",
        "multipath_primary_path",
        "multipath_alternate_path",
        "multipath_active_paths",
        "multipath_passive_paths",
        "multipath_failed_paths",
        "multipath_other_paths",
        "multipath_active_controllers",
        "multipath_passive_controllers",
        "multipath_failed_controllers",
    ),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def unique_join(values: list[Any]) -> str | None:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parts.append(normalized)
    return ", ".join(parts) if parts else None


def classify_multipath_member_state(value: Any) -> str:
    state_name = normalize_text(value)
    if not state_name:
        return "other"
    normalized_state = state_name.upper()
    if normalized_state == "ACTIVE":
        return "active"
    if normalized_state == "PASSIVE":
        return "passive"
    if normalized_state in {"FAIL", "FAILED", "FAULT", "OFFLINE", "LOST"}:
        return "failed"
    return "other"


@dataclass(slots=True, frozen=True)
class SlotStateRecord:
    system_id: str
    system_label: str | None
    enclosure_key: str
    enclosure_id: str | None
    enclosure_label: str | None
    slot: int
    slot_label: str
    present: bool
    state: str | None
    identify_active: bool
    device_name: str | None
    serial: str | None
    model: str | None
    gptid: str | None
    pool_name: str | None
    vdev_name: str | None
    health: str | None
    topology_label: str | None = None
    multipath_device: str | None = None
    multipath_mode: str | None = None
    multipath_state: str | None = None
    multipath_lunid: str | None = None
    multipath_primary_path: str | None = None
    multipath_alternate_path: str | None = None
    multipath_active_paths: str | None = None
    multipath_passive_paths: str | None = None
    multipath_failed_paths: str | None = None
    multipath_other_paths: str | None = None
    multipath_active_controllers: str | None = None
    multipath_passive_controllers: str | None = None
    multipath_failed_controllers: str | None = None

    @classmethod
    def from_snapshot_slot(
        cls,
        snapshot: dict[str, Any],
        slot_payload: dict[str, Any],
    ) -> "SlotStateRecord":
        system_id = normalize_text(snapshot.get("selected_system_id")) or "default"
        enclosure_id = normalize_text(
            slot_payload.get("enclosure_id") or snapshot.get("selected_enclosure_id")
        )
        enclosure_label = normalize_text(
            slot_payload.get("enclosure_label") or snapshot.get("selected_enclosure_label")
        )
        slot_number = int(slot_payload.get("slot") or 0)
        multipath_payload = slot_payload.get("multipath")
        multipath_active_paths: list[Any] = []
        multipath_passive_paths: list[Any] = []
        multipath_failed_paths: list[Any] = []
        multipath_other_paths: list[Any] = []
        multipath_active_controllers: list[Any] = []
        multipath_passive_controllers: list[Any] = []
        multipath_failed_controllers: list[Any] = []

        if isinstance(multipath_payload, dict):
            for member in multipath_payload.get("members") or []:
                if not isinstance(member, dict):
                    continue
                bucket = classify_multipath_member_state(member.get("state") or member.get("mode"))
                member_device = member.get("device_name")
                member_controller = member.get("controller_label")
                if bucket == "active":
                    multipath_active_paths.append(member_device)
                    multipath_active_controllers.append(member_controller)
                elif bucket == "passive":
                    multipath_passive_paths.append(member_device)
                    multipath_passive_controllers.append(member_controller)
                elif bucket == "failed":
                    multipath_failed_paths.append(member_device)
                    multipath_failed_controllers.append(member_controller)
                else:
                    multipath_other_paths.append(member_device)
        return cls(
            system_id=system_id,
            system_label=normalize_text(snapshot.get("selected_system_label")),
            enclosure_key=enclosure_id or "",
            enclosure_id=enclosure_id,
            enclosure_label=enclosure_label,
            slot=slot_number,
            slot_label=normalize_text(slot_payload.get("slot_label")) or f"{slot_number:02d}",
            present=as_bool(slot_payload.get("present")),
            state=normalize_text(slot_payload.get("state")),
            identify_active=as_bool(slot_payload.get("identify_active")),
            device_name=normalize_text(slot_payload.get("device_name")),
            serial=normalize_text(slot_payload.get("serial")),
            model=normalize_text(slot_payload.get("model")),
            gptid=normalize_text(slot_payload.get("gptid")),
            pool_name=normalize_text(slot_payload.get("pool_name")),
            vdev_name=normalize_text(slot_payload.get("vdev_name")),
            health=normalize_text(slot_payload.get("health")),
            topology_label=normalize_text(slot_payload.get("topology_label")),
            multipath_device=normalize_text(
                multipath_payload.get("device_name") if isinstance(multipath_payload, dict) else None
            ),
            multipath_mode=normalize_text(
                multipath_payload.get("mode") if isinstance(multipath_payload, dict) else None
            ),
            multipath_state=normalize_text(
                (
                    multipath_payload.get("state") or multipath_payload.get("provider_state")
                )
                if isinstance(multipath_payload, dict)
                else None
            ),
            multipath_lunid=normalize_text(
                multipath_payload.get("lunid") if isinstance(multipath_payload, dict) else None
            ),
            multipath_primary_path=normalize_text(
                multipath_payload.get("path_device_name") if isinstance(multipath_payload, dict) else None
            ),
            multipath_alternate_path=normalize_text(
                multipath_payload.get("alternate_path_device") if isinstance(multipath_payload, dict) else None
            ),
            multipath_active_paths=unique_join(multipath_active_paths),
            multipath_passive_paths=unique_join(multipath_passive_paths),
            multipath_failed_paths=unique_join(multipath_failed_paths),
            multipath_other_paths=unique_join(multipath_other_paths),
            multipath_active_controllers=unique_join(multipath_active_controllers),
            multipath_passive_controllers=unique_join(multipath_passive_controllers),
            multipath_failed_controllers=unique_join(multipath_failed_controllers),
        )


@dataclass(slots=True, frozen=True)
class SlotEvent:
    observed_at: str
    system_id: str
    system_label: str | None
    enclosure_key: str
    enclosure_id: str | None
    enclosure_label: str | None
    slot: int
    slot_label: str
    event_type: str
    previous_value: str | None
    current_value: str | None
    device_name: str | None
    serial: str | None
    details_json: str


@dataclass(slots=True, frozen=True)
class MetricSample:
    observed_at: str
    system_id: str
    system_label: str | None
    enclosure_key: str
    enclosure_id: str | None
    enclosure_label: str | None
    slot: int
    slot_label: str
    metric_name: str
    value_integer: int | None
    value_real: float | None
    device_name: str | None
    serial: str | None
    model: str | None
    state: str | None


def summarize_record(record: SlotStateRecord | None, event_type: str) -> str | None:
    if record is None:
        return None
    if event_type == "slot_state_changed":
        state_name = record.state or ("present" if record.present else "empty")
        led_state = "identify" if record.identify_active else "steady"
        return f"{state_name} / {led_state}"
    if event_type == "slot_identity_changed":
        return record.serial or record.device_name or record.gptid or "unknown disk"
    if event_type == "slot_topology_changed":
        return (
            record.topology_label
            or " / ".join(part for part in (record.pool_name, record.vdev_name) if part)
            or "unassigned"
        )
    if event_type == "slot_multipath_changed":
        summary_parts: list[str] = []
        if record.multipath_device:
            summary_parts.append(record.multipath_device)
        elif record.multipath_primary_path or record.multipath_alternate_path:
            summary_parts.append("direct path set")
        else:
            return "no multipath"
        if record.multipath_state:
            summary_parts.append(record.multipath_state)
        if record.multipath_active_paths:
            summary_parts.append(f"active {record.multipath_active_paths}")
        elif record.multipath_primary_path:
            summary_parts.append(f"primary {record.multipath_primary_path}")
        if record.multipath_failed_paths:
            summary_parts.append(f"failed {record.multipath_failed_paths}")
        return " / ".join(summary_parts)
    return record.serial or record.device_name or record.slot_label


def build_slot_events(
    previous: SlotStateRecord | None,
    current: SlotStateRecord,
    observed_at: str,
) -> list[SlotEvent]:
    if previous is None:
        return []

    events: list[SlotEvent] = []
    for event_type, field_names in EVENT_GROUPS.items():
        changes: dict[str, dict[str, Any]] = {}
        for field_name in field_names:
            old_value = getattr(previous, field_name)
            new_value = getattr(current, field_name)
            if old_value == new_value:
                continue
            changes[field_name] = {
                "label": FIELD_LABELS.get(field_name, field_name),
                "previous": old_value,
                "current": new_value,
            }
        if not changes:
            continue
        events.append(
            SlotEvent(
                observed_at=observed_at,
                system_id=current.system_id,
                system_label=current.system_label,
                enclosure_key=current.enclosure_key,
                enclosure_id=current.enclosure_id,
                enclosure_label=current.enclosure_label,
                slot=current.slot,
                slot_label=current.slot_label,
                event_type=event_type,
                previous_value=summarize_record(previous, event_type),
                current_value=summarize_record(current, event_type),
                device_name=current.device_name,
                serial=current.serial,
                details_json=json.dumps(changes, sort_keys=True),
            )
        )
    return events
