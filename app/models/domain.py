from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SlotState(str, Enum):
    healthy = "healthy"
    empty = "empty"
    identify = "identify"
    fault = "fault"
    unknown = "unknown"
    unmapped = "unmapped"


class LedAction(str, Enum):
    identify = "IDENTIFY"
    fault = "FAULT"
    clear = "CLEAR"


class ManualMapping(BaseModel):
    system_id: str | None = None
    slot: int
    enclosure_id: str | None = None
    serial: str | None = None
    device_name: str | None = None
    gptid: str | None = None
    notes: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    source: str = "manual"

    @field_validator("serial", "device_name", "gptid", "notes")
    @classmethod
    def trim_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned[:256] if cleaned else None


class SlotView(BaseModel):
    slot: int
    slot_label: str
    row_index: int
    column_index: int
    enclosure_id: str | None = None
    enclosure_label: str | None = None
    enclosure_name: str | None = None
    present: bool = False
    state: SlotState = SlotState.unknown
    identify_active: bool = False
    device_name: str | None = None
    serial: str | None = None
    model: str | None = None
    size_bytes: int | None = None
    size_human: str | None = None
    gptid: str | None = None
    pool_name: str | None = None
    vdev_name: str | None = None
    vdev_class: str | None = None
    topology_label: str | None = None
    health: str | None = None
    enclosure_identifier: str | None = None
    led_supported: bool = False
    led_backend: str | None = None
    led_reason: str | None = None
    ssh_ses_device: str | None = None
    ssh_ses_element_id: int | None = None
    ssh_ses_targets: list[dict[str, Any]] = Field(default_factory=list)
    mapping_source: str = "unknown"
    notes: str | None = None
    search_text: str = ""
    raw_status: dict[str, Any] = Field(default_factory=dict)


class SourceStatus(BaseModel):
    enabled: bool
    ok: bool
    message: str | None = None


class SystemOption(BaseModel):
    id: str
    label: str


class EnclosureOption(BaseModel):
    id: str
    label: str
    name: str | None = None


class InventorySummary(BaseModel):
    disk_count: int = 0
    pool_count: int = 0
    enclosure_count: int = 0
    mapped_slot_count: int = 0
    manual_mapping_count: int = 0
    ssh_slot_hint_count: int = 0


class InventorySnapshot(BaseModel):
    slots: list[SlotView]
    last_updated: datetime = Field(default_factory=utcnow)
    generated_at: datetime = Field(default_factory=utcnow)
    refresh_interval_seconds: int
    selected_system_id: str | None = None
    selected_system_label: str | None = None
    selected_enclosure_id: str | None = None
    selected_enclosure_label: str | None = None
    selected_enclosure_name: str | None = None
    systems: list[SystemOption] = Field(default_factory=list)
    enclosures: list[EnclosureOption] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sources: dict[str, SourceStatus] = Field(default_factory=dict)
    summary: InventorySummary = Field(default_factory=InventorySummary)


class LedRequest(BaseModel):
    action: LedAction


class MappingRequest(BaseModel):
    serial: str | None = None
    device_name: str | None = None
    gptid: str | None = None
    notes: str | None = None
    clear_identify_after_save: bool = True

    @field_validator("serial", "device_name", "gptid", "notes")
    @classmethod
    def sanitize_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned[:256] if cleaned else None
