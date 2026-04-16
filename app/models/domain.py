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


class MultipathMember(BaseModel):
    device_name: str
    state: str | None = None
    mode: str | None = None
    controller_label: str | None = None


class MultipathView(BaseModel):
    name: str
    device_name: str
    uuid: str | None = None
    mode: str | None = None
    state: str | None = None
    provider_state: str | None = None
    path_device_name: str | None = None
    alternate_path_device: str | None = None
    lunid: str | None = None
    bus: str | None = None
    members: list[MultipathMember] = Field(default_factory=list)


class SmartSummaryView(BaseModel):
    available: bool = False
    temperature_c: int | None = None
    warning_temperature_c: int | None = None
    critical_temperature_c: int | None = None
    smart_health_status: str | None = None
    last_test_type: str | None = None
    last_test_status: str | None = None
    last_test_lifetime_hours: int | None = None
    last_test_age_hours: int | None = None
    power_on_hours: int | None = None
    power_on_days: int | None = None
    logical_block_size: int | None = None
    physical_block_size: int | None = None
    available_spare_percent: int | None = None
    available_spare_threshold_percent: int | None = None
    endurance_used_percent: int | None = None
    endurance_remaining_percent: int | None = None
    bytes_read: int | None = None
    bytes_written: int | None = None
    annualized_bytes_written: int | None = None
    estimated_lifetime_bytes_written: int | None = None
    estimated_remaining_bytes_written: int | None = None
    media_errors: int | None = None
    predictive_errors: int | None = None
    non_medium_errors: int | None = None
    uncorrected_read_errors: int | None = None
    uncorrected_write_errors: int | None = None
    unsafe_shutdowns: int | None = None
    rotation_rate_rpm: int | None = None
    form_factor: str | None = None
    firmware_version: str | None = None
    protocol_version: str | None = None
    namespace_eui64: str | None = None
    namespace_nguid: str | None = None
    read_cache_enabled: bool | None = None
    writeback_cache_enabled: bool | None = None
    trim_supported: bool | None = None
    transport_protocol: str | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    negotiated_link_rate: str | None = None
    message: str | None = None


class SmartBatchRequest(BaseModel):
    slots: list[int] = Field(default_factory=list)


class SmartBatchItem(BaseModel):
    slot: int
    summary: SmartSummaryView


class SmartBatchResponse(BaseModel):
    summaries: list[SmartBatchItem] = Field(default_factory=list)


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
    smart_device_names: list[str] = Field(default_factory=list)
    serial: str | None = None
    model: str | None = None
    size_bytes: int | None = None
    size_human: str | None = None
    gptid: str | None = None
    persistent_id_label: str | None = None
    pool_name: str | None = None
    vdev_name: str | None = None
    vdev_class: str | None = None
    topology_label: str | None = None
    health: str | None = None
    multipath: MultipathView | None = None
    temperature_c: int | None = None
    last_smart_test_type: str | None = None
    last_smart_test_status: str | None = None
    last_smart_test_lifetime_hours: int | None = None
    logical_block_size: int | None = None
    physical_block_size: int | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
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
    operator_context: dict[str, Any] = Field(default_factory=dict)
    raw_status: dict[str, Any] = Field(default_factory=dict)


class SourceStatus(BaseModel):
    enabled: bool
    ok: bool
    message: str | None = None


class SystemOption(BaseModel):
    id: str
    label: str
    platform: str | None = None


class EnclosureOption(BaseModel):
    id: str
    label: str
    name: str | None = None
    profile_id: str | None = None
    rows: int | None = None
    columns: int | None = None
    slot_count: int | None = None
    slot_layout: list[list[int]] | None = None


class EnclosureProfileView(BaseModel):
    id: str
    label: str
    eyebrow: str | None = None
    summary: str | None = None
    panel_title: str | None = None
    edge_label: str | None = None
    face_style: str = "generic"
    latch_edge: str = "bottom"
    bay_size: str | None = None
    rows: int
    columns: int
    slot_layout: list[list[int]] = Field(default_factory=list)
    row_groups: list[int] = Field(default_factory=list)
    slot_hints: dict[int, list[str]] = Field(default_factory=dict)


class InventorySummary(BaseModel):
    disk_count: int = 0
    pool_count: int = 0
    enclosure_count: int = 0
    mapped_slot_count: int = 0
    manual_mapping_count: int = 0
    ssh_slot_hint_count: int = 0


class InventorySnapshot(BaseModel):
    slots: list[SlotView]
    layout_rows: list[list[int]] = Field(default_factory=list)
    layout_slot_count: int = 0
    layout_columns: int = 0
    last_updated: datetime = Field(default_factory=utcnow)
    generated_at: datetime = Field(default_factory=utcnow)
    refresh_interval_seconds: int
    selected_system_id: str | None = None
    selected_system_label: str | None = None
    selected_system_platform: str | None = None
    selected_enclosure_id: str | None = None
    selected_enclosure_label: str | None = None
    selected_enclosure_name: str | None = None
    selected_profile: EnclosureProfileView | None = None
    systems: list[SystemOption] = Field(default_factory=list)
    enclosures: list[EnclosureOption] = Field(default_factory=list)
    platform_context: dict[str, Any] = Field(default_factory=dict)
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


class MappingBundle(BaseModel):
    schema_version: int = 1
    app_version: str | None = None
    exported_at: datetime = Field(default_factory=utcnow)
    system_id: str | None = None
    enclosure_id: str | None = None
    mappings: list[ManualMapping] = Field(default_factory=list)
