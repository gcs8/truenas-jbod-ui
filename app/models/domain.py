from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def trim_optional_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:max_length] if cleaned else None


def preserve_optional_secret(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    return str(value)[:max_length]


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
    power_cycle_count: int | None = None
    power_on_resets: int | None = None
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
    read_commands: int | None = None
    write_commands: int | None = None
    read_error_count: int | None = None
    write_error_count: int | None = None
    media_errors: int | None = None
    predictive_errors: int | None = None
    non_medium_errors: int | None = None
    uncorrected_read_errors: int | None = None
    uncorrected_write_errors: int | None = None
    unsafe_shutdowns: int | None = None
    hardware_resets: int | None = None
    interface_crc_errors: int | None = None
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
    max_concurrency: int | None = None

    @field_validator("slots")
    @classmethod
    def validate_slots(cls, value: list[int]) -> list[int]:
        return [int(slot) for slot in value]

    @field_validator("max_concurrency")
    @classmethod
    def validate_max_concurrency(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(1, min(int(value), 128))


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
    smart_device_type: str | None = None
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
    slot_layout: list[list[int | None]] | None = None


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
    slot_layout: list[list[int | None]] = Field(default_factory=list)
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
    layout_rows: list[list[int | None]] = Field(default_factory=list)
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


class SystemLocatorRequest(BaseModel):
    active: bool


class SystemLocatorStatusView(BaseModel):
    supported: bool = False
    active: bool = False
    backend: str | None = None
    reason: str | None = None


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


class SnapshotExportRequest(BaseModel):
    selected_slot: int | None = None
    history_window_hours: int | None = 24
    history_panel_open: bool = False
    io_chart_mode: str = "total"
    redact_sensitive: bool = False
    packaging: Literal["auto", "html", "zip"] = "auto"
    allow_oversize: bool = False


class SystemBackupExportRequest(BaseModel):
    encrypt: bool = False
    passphrase: str | None = None
    packaging: Literal["tar.zst", "zip", "tar.gz", "7z"] = "tar.zst"
    included_paths: list[str] = Field(default_factory=list)

    @field_validator("passphrase")
    @classmethod
    def sanitize_passphrase(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=512)

    @field_validator("included_paths")
    @classmethod
    def sanitize_included_paths(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = trim_optional_text(item, max_length=128)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            cleaned_items.append(cleaned)
        return cleaned_items

    @model_validator(mode="after")
    def validate_encryption_requirements(self) -> "SystemBackupExportRequest":
        if self.encrypt and not self.passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        return self


class DebugBundleExportRequest(BaseModel):
    encrypt: bool = False
    passphrase: str | None = None
    packaging: Literal["tar.zst", "zip", "tar.gz", "7z"] = "tar.zst"
    included_paths: list[str] = Field(default_factory=list)
    scrub_secrets: bool = True
    scrub_disk_identifiers: bool = True
    scrub_sensitive: bool | None = None

    @field_validator("passphrase")
    @classmethod
    def sanitize_passphrase(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=512)

    @field_validator("included_paths")
    @classmethod
    def sanitize_included_paths(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = trim_optional_text(item, max_length=128)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            cleaned_items.append(cleaned)
        return cleaned_items

    @model_validator(mode="after")
    def validate_encryption_requirements(self) -> "DebugBundleExportRequest":
        if self.scrub_sensitive is not None:
            if "scrub_secrets" not in self.model_fields_set:
                self.scrub_secrets = self.scrub_sensitive
            if "scrub_disk_identifiers" not in self.model_fields_set:
                self.scrub_disk_identifiers = self.scrub_sensitive
        if self.encrypt and not self.passphrase:
            raise ValueError("A passphrase is required when encryption is enabled.")
        return self


class DemoSystemRequest(BaseModel):
    system_id: str | None = None
    label: str = "Demo Builder Lab"
    make_default: bool = False
    replace_existing: bool = False

    @field_validator("system_id", "label")
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=256)


class HistoryAdoptRequest(BaseModel):
    source_system_id: str
    target_system_id: str


class EnclosureProfileRequest(BaseModel):
    source_profile_id: str | None = None
    id: str | None = None
    label: str
    eyebrow: str | None = None
    summary: str | None = None
    panel_title: str | None = None
    edge_label: str | None = None
    face_style: str = "front-drive"
    latch_edge: Literal["top", "bottom", "left", "right"] = "bottom"
    bay_size: Literal["3.5", "2.5"] | None = None
    rows: int = 1
    columns: int = 1
    slot_count: int | None = None
    row_groups: list[int] = Field(default_factory=list)
    slot_layout: list[list[int | None]] | None = None
    slot_hints: dict[int, list[str]] = Field(default_factory=dict)

    @field_validator(
        "source_profile_id",
        "id",
        "label",
        "eyebrow",
        "summary",
        "panel_title",
        "edge_label",
        "face_style",
    )
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=256)

    @field_validator("rows", "columns")
    @classmethod
    def sanitize_dimensions(cls, value: int) -> int:
        return max(1, min(int(value), 256))

    @field_validator("slot_count")
    @classmethod
    def sanitize_slot_count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(1, min(int(value), 4096))

    @field_validator("row_groups")
    @classmethod
    def sanitize_row_groups(cls, value: list[int]) -> list[int]:
        return [int(group) for group in value if int(group) > 0]

    @field_validator("slot_layout", mode="before")
    @classmethod
    def sanitize_slot_layout(cls, value: Any) -> list[list[int | None]] | None:
        if value is None or value == "":
            return None
        if not isinstance(value, list):
            raise ValueError("slot_layout must be a list of rows.")
        normalized_rows: list[list[int | None]] = []
        seen_slots: set[int] = set()
        for row in value:
            if not isinstance(row, list):
                raise ValueError("slot_layout rows must be lists.")
            normalized_row: list[int | None] = []
            for raw_slot in row:
                if raw_slot is None or raw_slot == "":
                    normalized_row.append(None)
                    continue
                slot_number = int(raw_slot)
                if slot_number < 0:
                    raise ValueError("slot_layout values must be non-negative integers or null.")
                if slot_number in seen_slots:
                    raise ValueError("slot_layout slot numbers must be unique.")
                seen_slots.add(slot_number)
                normalized_row.append(slot_number)
            normalized_rows.append(normalized_row)
        return normalized_rows

    @field_validator("slot_hints", mode="before")
    @classmethod
    def sanitize_slot_hints(cls, value: Any) -> dict[int, list[str]]:
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("slot_hints must be a mapping of slot numbers to hint lists.")
        cleaned: dict[int, list[str]] = {}
        for raw_key, raw_values in value.items():
            slot_number = int(raw_key)
            if slot_number < 0:
                continue
            if not isinstance(raw_values, list):
                raise ValueError("slot_hints values must be lists.")
            hints: list[str] = []
            seen: set[str] = set()
            for raw_hint in raw_values:
                normalized = trim_optional_text(str(raw_hint) if raw_hint is not None else None, max_length=256)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    hints.append(normalized)
            if hints:
                cleaned[slot_number] = hints
        return cleaned

    @model_validator(mode="after")
    def validate_profile_builder_request(self) -> "EnclosureProfileRequest":
        if not self.label:
            raise ValueError("A profile label is required.")
        if self.slot_layout is not None:
            if len(self.slot_layout) != self.rows:
                raise ValueError("slot_layout row count must match rows.")
            widest_row = max((len(row) for row in self.slot_layout), default=0)
            if widest_row > self.columns:
                raise ValueError("slot_layout rows cannot be wider than columns.")
            if self.slot_count is not None:
                layout_slot_count = sum(
                    1
                    for row in self.slot_layout
                    for slot in row
                    if isinstance(slot, int)
                )
                if layout_slot_count != self.slot_count:
                    raise ValueError("slot_layout must contain exactly slot_count visible slots.")
        if self.slot_count is not None and self.slot_layout is None and self.slot_count > (self.rows * self.columns):
            raise ValueError("slot_count cannot exceed rows x columns for the rectangular builder.")
        if self.row_groups and sum(self.row_groups) != self.columns:
            raise ValueError("row_groups must add up to the column count.")
        return self


StorageViewKind = Literal["ses_enclosure", "nvme_carrier", "boot_devices", "manual"]
StorageViewBindingMode = Literal["auto", "pool", "serial", "hybrid"]


class StorageViewRenderRequest(BaseModel):
    show_in_main_ui: bool = True
    show_in_admin_ui: bool = True
    default_collapsed: bool = False


class StorageViewBindingRequest(BaseModel):
    mode: StorageViewBindingMode = "auto"
    target_system_id: str | None = None
    enclosure_ids: list[str] = Field(default_factory=list)
    pool_names: list[str] = Field(default_factory=list)
    serials: list[str] = Field(default_factory=list)
    pcie_addresses: list[str] = Field(default_factory=list)
    device_names: list[str] = Field(default_factory=list)

    @field_validator("target_system_id")
    @classmethod
    def sanitize_target_system_id(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=256)

    @field_validator("enclosure_ids", "pool_names", "serials", "pcie_addresses", "device_names")
    @classmethod
    def sanitize_lists(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = trim_optional_text(str(item), max_length=256)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                cleaned_items.append(cleaned)
        return cleaned_items


class StorageViewLayoutOverridesRequest(BaseModel):
    slot_labels: dict[int, str] = Field(default_factory=dict)
    slot_sizes: dict[int, str] = Field(default_factory=dict)

    @field_validator("slot_labels", mode="before")
    @classmethod
    def sanitize_slot_labels(cls, value: Any) -> dict[int, str]:
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("slot_labels must be a mapping.")
        cleaned: dict[int, str] = {}
        for raw_key, raw_label in value.items():
            try:
                slot_number = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise ValueError("slot label keys must be integers.") from exc
            if slot_number < 0:
                continue
            label = trim_optional_text(str(raw_label) if raw_label is not None else None, max_length=128)
            if label:
                cleaned[slot_number] = label
        return cleaned

    @field_validator("slot_sizes", mode="before")
    @classmethod
    def sanitize_slot_sizes(cls, value: Any) -> dict[int, str]:
        if value is None or value == "":
            return {}
        if not isinstance(value, dict):
            raise ValueError("slot_sizes must be a mapping.")
        allowed_sizes = {"2230", "2242", "2260", "2280", "22110"}
        cleaned: dict[int, str] = {}
        for raw_key, raw_size in value.items():
            try:
                slot_number = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise ValueError("slot size keys must be integers.") from exc
            if slot_number < 0:
                continue
            size_label = trim_optional_text(str(raw_size) if raw_size is not None else None, max_length=16)
            if size_label in allowed_sizes:
                cleaned[slot_number] = size_label
        return cleaned


class StorageViewRequest(BaseModel):
    id: str | None = None
    label: str
    kind: StorageViewKind
    template_id: str
    profile_id: str | None = None
    enabled: bool = True
    order: int = 10
    render: StorageViewRenderRequest = Field(default_factory=StorageViewRenderRequest)
    binding: StorageViewBindingRequest = Field(default_factory=StorageViewBindingRequest)
    layout_overrides: StorageViewLayoutOverridesRequest | None = None

    @field_validator("id", "label", "template_id", "profile_id")
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=256)

    @field_validator("order")
    @classmethod
    def sanitize_order(cls, value: int) -> int:
        return max(0, min(int(value), 100000))

    @model_validator(mode="after")
    def validate_storage_view(self) -> "StorageViewRequest":
        if not self.label:
            raise ValueError("A storage view label is required.")
        if not self.template_id:
            raise ValueError("A storage view template is required.")
        return self


class StorageViewRuntimeSlot(BaseModel):
    slot_index: int
    slot_label: str
    candidate_id: str | None = None
    target_system_id: str | None = None
    target_system_label: str | None = None
    occupied: bool = False
    state: str = "empty"
    source: Literal["snapshot_slot", "inventory_candidate", "placeholder"] = "placeholder"
    match_reasons: list[str] = Field(default_factory=list)
    placement_key: str | None = None
    assignment_rank: int | None = None
    snapshot_slot: int | None = None
    device_name: str | None = None
    smart_device_names: list[str] = Field(default_factory=list)
    smart_device_type: str | None = None
    serial: str | None = None
    pool_name: str | None = None
    model: str | None = None
    size_bytes: int | None = None
    bus: str | None = None
    size_human: str | None = None
    gptid: str | None = None
    persistent_id_label: str | None = None
    health: str | None = None
    temperature_c: int | None = None
    last_smart_test_type: str | None = None
    last_smart_test_status: str | None = None
    last_smart_test_lifetime_hours: int | None = None
    logical_block_size: int | None = None
    physical_block_size: int | None = None
    logical_unit_id: str | None = None
    sas_address: str | None = None
    attached_sas_address: str | None = None
    transport_address: str | None = None
    description: str | None = None
    led_supported: bool = False
    slot_size: str | None = None


class StorageViewRuntimeView(BaseModel):
    id: str
    label: str
    kind: StorageViewKind
    template_id: str
    profile_id: str | None = None
    profile_label: str | None = None
    eyebrow: str | None = None
    summary: str | None = None
    panel_title: str | None = None
    edge_label: str | None = None
    face_style: str = "generic"
    latch_edge: str = "bottom"
    bay_size: str | None = None
    row_groups: list[int] = Field(default_factory=list)
    enabled: bool = True
    render: StorageViewRenderRequest = Field(default_factory=StorageViewRenderRequest)
    binding: StorageViewBindingRequest = Field(default_factory=StorageViewBindingRequest)
    order: int = 10
    template_label: str | None = None
    slot_layout: list[list[int | None]] = Field(default_factory=list)
    source: Literal["selected_enclosure_snapshot", "inventory_binding"] = "inventory_binding"
    backing_enclosure_id: str | None = None
    backing_enclosure_label: str | None = None
    notes: list[str] = Field(default_factory=list)
    matched_count: int = 0
    slot_count: int = 0
    slots: list[StorageViewRuntimeSlot] = Field(default_factory=list)


class StorageViewRuntimePayload(BaseModel):
    system_id: str | None = None
    system_label: str | None = None
    views: list[StorageViewRuntimeView] = Field(default_factory=list)


class HANodeRequest(BaseModel):
    system_id: str | None = None
    label: str | None = None
    host: str | None = None

    @field_validator("system_id", "label", "host")
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=256)


class SystemSetupRequest(BaseModel):
    system_id: str | None = None
    label: str
    platform: Literal["core", "scale", "linux", "quantastor", "esxi", "ipmi"] = "core"
    truenas_host: str
    api_key: str | None = None
    api_user: str | None = None
    api_password: str | None = None
    verify_ssl: bool = True
    tls_ca_bundle_path: str | None = None
    tls_server_name: str | None = None
    enclosure_filter: str | None = None
    timeout_seconds: int = 15
    ssh_enabled: bool = False
    ssh_host: str | None = None
    ssh_extra_hosts: list[str] = Field(default_factory=list)
    ha_enabled: bool = False
    ha_nodes: list[HANodeRequest] = Field(default_factory=list)
    ssh_port: int = 22
    ssh_user: str | None = None
    ssh_key_path: str | None = "/run/ssh/id_truenas"
    ssh_password: str | None = None
    ssh_sudo_password: str | None = None
    ssh_known_hosts_path: str | None = "/app/data/known_hosts"
    ssh_strict_host_key_checking: bool = True
    ssh_timeout_seconds: int = 15
    ssh_commands: list[str] = Field(default_factory=list)
    bmc_enabled: bool = False
    bmc_host: str | None = None
    bmc_username: str | None = None
    bmc_password: str | None = None
    bmc_verify_ssl: bool = True
    bmc_timeout_seconds: int = 15
    default_profile_id: str | None = None
    storage_views: list[StorageViewRequest] | None = None
    replace_existing: bool = False
    make_default: bool = False

    @field_validator(
        "system_id",
        "label",
        "truenas_host",
        "api_key",
        "api_user",
        "enclosure_filter",
        "tls_ca_bundle_path",
        "tls_server_name",
        "ssh_host",
        "ssh_user",
        "ssh_key_path",
        "ssh_known_hosts_path",
        "bmc_host",
        "bmc_username",
        "default_profile_id",
    )
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=1024)

    @field_validator("api_password", "ssh_password", "ssh_sudo_password", "bmc_password")
    @classmethod
    def sanitize_secret_fields(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=1024)

    @field_validator("ssh_extra_hosts", "ssh_commands")
    @classmethod
    def sanitize_string_lists(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned:
                cleaned_items.append(cleaned[:1024])
        return cleaned_items

    @field_validator("ha_nodes", mode="after")
    @classmethod
    def sanitize_ha_nodes(cls, value: list[HANodeRequest]) -> list[HANodeRequest]:
        cleaned: list[HANodeRequest] = []
        seen: set[tuple[str | None, str | None]] = set()
        for node in value[:3]:
            if not (node.system_id or node.label or node.host):
                continue
            dedupe_key = (node.system_id, node.host)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            cleaned.append(node)
        return cleaned

    @field_validator("timeout_seconds", "ssh_timeout_seconds", "bmc_timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @field_validator("ssh_port")
    @classmethod
    def clamp_ssh_port(cls, value: int) -> int:
        return max(1, min(int(value), 65535))

    @model_validator(mode="after")
    def validate_required_fields(self) -> "SystemSetupRequest":
        ssh_only_host_platform = self.platform in {"linux", "esxi"}
        bmc_only_host_platform = self.platform == "ipmi"
        primary_host = (
            self.truenas_host
            or (self.ssh_host if ssh_only_host_platform else None)
            or (self.bmc_host if bmc_only_host_platform else None)
        )
        if not self.label:
            raise ValueError("A system label is required.")
        if not primary_host:
            raise ValueError("A host is required.")
        if bmc_only_host_platform and not self.bmc_enabled:
            raise ValueError("Enable BMC access for IPMI / BMC Only systems.")
        if ssh_only_host_platform and not self.truenas_host:
            self.truenas_host = primary_host
        if bmc_only_host_platform and not self.truenas_host:
            self.truenas_host = primary_host
        if self.ssh_enabled and not self.ssh_host:
            self.ssh_host = primary_host
        if self.ssh_enabled and not self.ssh_user:
            raise ValueError("An SSH user is required when SSH enrichment is enabled.")
        if self.bmc_enabled and not self.bmc_host:
            self.bmc_host = primary_host
        if self.bmc_enabled and not self.bmc_username:
            raise ValueError("A BMC username is required when BMC access is enabled.")
        if self.bmc_enabled and not self.bmc_password:
            raise ValueError("A BMC password is required when BMC access is enabled.")
        return self


class TLSCertificateInspectRequest(BaseModel):
    host: str
    timeout_seconds: int = 10
    tls_server_name: str | None = None

    @field_validator("host", "tls_server_name")
    @classmethod
    def sanitize_host(cls, value: str | None) -> str:
        return trim_optional_text(value, max_length=1024) or ""

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @model_validator(mode="after")
    def validate_host(self) -> "TLSCertificateInspectRequest":
        if not self.host:
            raise ValueError("A TLS host is required.")
        return self


class TLSCertificateImportRequest(BaseModel):
    pem_text: str
    bundle_name: str | None = None
    system_id: str | None = None
    host: str | None = None
    tls_server_name: str | None = None

    @field_validator("bundle_name", "system_id", "host", "tls_server_name")
    @classmethod
    def sanitize_optional_text(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=1024)

    @field_validator("pem_text")
    @classmethod
    def sanitize_pem_text(cls, value: str) -> str:
        return str(value or "")[:262144]

    @model_validator(mode="after")
    def validate_payload(self) -> "TLSCertificateImportRequest":
        if not self.pem_text.strip():
            raise ValueError("PEM certificate text is required.")
        return self


class TLSRemoteCertificateTrustRequest(BaseModel):
    host: str
    timeout_seconds: int = 10
    bundle_name: str | None = None
    system_id: str | None = None
    tls_server_name: str | None = None

    @field_validator("host", "bundle_name", "system_id", "tls_server_name")
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=1024)

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @model_validator(mode="after")
    def validate_host(self) -> "TLSRemoteCertificateTrustRequest":
        if not self.host:
            raise ValueError("A TLS host is required.")
        return self


class QuantastorNodeDiscoveryRequest(BaseModel):
    truenas_host: str
    api_user: str
    api_password: str
    verify_ssl: bool = True
    tls_ca_bundle_path: str | None = None
    tls_server_name: str | None = None
    timeout_seconds: int = 15

    @field_validator(
        "truenas_host",
        "api_user",
        "tls_ca_bundle_path",
        "tls_server_name",
    )
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=1024)

    @field_validator("api_password")
    @classmethod
    def sanitize_secret_fields(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=1024)

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @model_validator(mode="after")
    def validate_required_fields(self) -> "QuantastorNodeDiscoveryRequest":
        if not self.truenas_host:
            raise ValueError("A host is required.")
        if not self.api_user:
            raise ValueError("An API user is required.")
        if not self.api_password:
            raise ValueError("An API password is required.")
        return self


class SSHKeyGenerateRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, value: str) -> str:
        cleaned = value.strip()
        return cleaned[:128] if cleaned else ""

    @model_validator(mode="after")
    def validate_name(self) -> "SSHKeyGenerateRequest":
        if not self.name:
            raise ValueError("A key name is required.")
        return self


class SystemSetupBootstrapRequest(BaseModel):
    platform: Literal["core", "scale", "linux", "quantastor", "esxi", "ipmi"] = "core"
    host: str
    port: int = 22
    bootstrap_user: str
    bootstrap_password: str | None = None
    bootstrap_sudo_password: str | None = None
    bootstrap_key_path: str | None = None
    bootstrap_known_hosts_path: str | None = "/app/data/known_hosts"
    bootstrap_strict_host_key_checking: bool = True
    timeout_seconds: int = 15
    service_user: str = "jbodmap"
    service_shell: str = "/bin/sh"
    service_key_name: str | None = None
    service_key_path: str | None = None
    service_public_key: str | None = None
    install_sudo_rules: bool = True
    sudo_commands: list[str] = Field(default_factory=list)

    @field_validator(
        "host",
        "bootstrap_user",
        "bootstrap_key_path",
        "bootstrap_known_hosts_path",
        "service_user",
        "service_shell",
        "service_key_name",
        "service_key_path",
        "service_public_key",
    )
    @classmethod
    def sanitize_bootstrap_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=4096)

    @field_validator("bootstrap_password", "bootstrap_sudo_password")
    @classmethod
    def sanitize_bootstrap_secret_fields(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=4096)

    @field_validator("sudo_commands")
    @classmethod
    def sanitize_bootstrap_command_list(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned:
                cleaned_items.append(cleaned[:1024])
        return cleaned_items

    @field_validator("port")
    @classmethod
    def clamp_port(cls, value: int) -> int:
        return max(1, min(int(value), 65535))

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @model_validator(mode="after")
    def validate_bootstrap_requirements(self) -> "SystemSetupBootstrapRequest":
        if not self.host:
            raise ValueError("A bootstrap host is required.")
        if not self.bootstrap_user:
            raise ValueError("A one-time bootstrap username is required.")
        if not self.bootstrap_password and not self.bootstrap_key_path:
            raise ValueError("Provide either a bootstrap password or a bootstrap key path.")
        if not self.service_user:
            raise ValueError("A target service-account user is required.")
        if not self.service_key_name and not self.service_key_path and not self.service_public_key:
            raise ValueError("Select or provide the SSH key that should be installed for the service account.")
        return self


class ESXiHostPrepInstallRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    key_path: str | None = None
    password: str | None = None
    known_hosts_path: str | None = "/app/data/known_hosts"
    strict_host_key_checking: bool = True
    timeout_seconds: int = 15
    upload_token: str

    @field_validator("host", "user", "key_path", "known_hosts_path", "upload_token")
    @classmethod
    def sanitize_text_fields(cls, value: str | None) -> str | None:
        return trim_optional_text(value, max_length=4096)

    @field_validator("password")
    @classmethod
    def sanitize_secret_field(cls, value: str | None) -> str | None:
        return preserve_optional_secret(value, max_length=4096)

    @field_validator("port")
    @classmethod
    def clamp_port(cls, value: int) -> int:
        return max(1, min(int(value), 65535))

    @field_validator("timeout_seconds")
    @classmethod
    def clamp_timeout(cls, value: int) -> int:
        return max(1, min(int(value), 300))

    @model_validator(mode="after")
    def validate_requirements(self) -> "ESXiHostPrepInstallRequest":
        if not self.host:
            raise ValueError("An ESXi SSH host is required.")
        if not self.user:
            raise ValueError("An ESXi SSH user is required.")
        if not self.password and not self.key_path:
            raise ValueError("Provide either an ESXi SSH password or key path.")
        if not self.upload_token:
            raise ValueError("Choose a staged ESXi package before installing it.")
        return self


class SystemSetupSudoPreviewRequest(BaseModel):
    platform: Literal["core", "scale", "linux", "quantastor", "esxi", "ipmi"] = "core"
    service_user: str = "jbodmap"
    install_sudo_rules: bool = True
    sudo_commands: list[str] = Field(default_factory=list)

    @field_validator("service_user")
    @classmethod
    def sanitize_service_user(cls, value: str | None) -> str:
        return trim_optional_text(value, max_length=256) or "jbodmap"

    @field_validator("sudo_commands")
    @classmethod
    def sanitize_preview_command_list(cls, value: list[str]) -> list[str]:
        cleaned_items: list[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned:
                cleaned_items.append(cleaned[:1024])
        return cleaned_items
