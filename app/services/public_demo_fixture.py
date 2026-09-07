from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any, Literal

from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.datastructures import URLPath
from starlette.requests import Request

from app.config import (
    Settings,
    StorageViewBindingConfig,
    StorageViewConfig,
    StorageViewLayoutOverridesConfig,
    StorageViewRenderConfig,
)
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    InventorySummary,
    SlotState,
    SlotView,
    SmartSummaryView,
    SourceStatus,
    StorageViewRuntimePayload,
    StorageViewRuntimeSlot,
    StorageViewRuntimeView,
    SystemOption,
)
from app.script_json import register_script_json_filters
from app.services.profile_registry import ProfileRegistry
from app.services.snapshot_export import SnapshotExportService
from app.services.storage_view_templates import get_storage_view_template
from app.services.storage_views import (
    build_storage_view_rows,
    ordered_storage_view_slot_indices,
    storage_view_slot_label,
    storage_view_slot_size,
)


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DEMO_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "public_demo" / "public_demo.json"
PUBLIC_DEMO_HISTORY_WINDOW_HOURS = 7 * 24
SYNTHETIC_ID_PATTERN = re.compile(r"^(?:demo-[a-z0-9-]+|boot-doms|nvme-carrier-x4)$")
SYNTHETIC_SERIAL_PATTERN = re.compile(r"^DEMO-SN-[A-Z0-9-]+$")
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private IPv4 address",
        re.compile(r"(?<![0-9])(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.(?:[0-9]{1,3}\.)?[0-9]{1,3}(?![0-9])"),
    ),
    ("credential field or value", re.compile(r"(?i)\b(?:password|passwd|api[_-]?key|secret|token|private[_-]?key)\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----")),
    ("WWN or NAA identifier", re.compile(r"(?i)\b(?:wwn|naa)\.[0-9a-f]{16,}\b")),
)


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicDemoSystem(FixtureModel):
    id: str = Field(pattern=r"^demo-[a-z0-9-]+$", max_length=64)
    label: str = Field(min_length=1, max_length=64)
    platform: Literal["core"]


class PublicDemoEnclosure(FixtureModel):
    id: str = Field(pattern=r"^demo-[a-z0-9-]+$", max_length=96)
    label: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=96)
    profile_id: Literal["supermicro-cse-946-top-60"]


class PublicDemoEvent(FixtureModel):
    hours_before_capture: int = Field(ge=0, le=168)
    event_type: Literal["health_change", "temperature_notice"]
    previous_value: str = Field(max_length=64)
    current_value: str = Field(max_length=64)
    detail: str = Field(max_length=160)


class PublicDemoSlot(FixtureModel):
    slot: int = Field(ge=0, le=59)
    state: SlotState
    device_name: str | None = Field(default=None, pattern=r"^demo-disk-[0-9]{2}$", max_length=32)
    serial: str | None = Field(default=None, pattern=r"^DEMO-SN-CORE-[0-9]{4}$", max_length=32)
    model: str | None = Field(default=None, pattern=r"^Demo [A-Za-z0-9 -]+$", max_length=64)
    size_bytes: int | None = Field(default=None, ge=1)
    size_human: str | None = Field(default=None, max_length=32)
    pool_name: str | None = Field(default=None, pattern=r"^demo-[a-z0-9-]+$", max_length=64)
    vdev_name: str | None = Field(default=None, pattern=r"^(?:raidz2|mirror|spare)-[0-9]+$", max_length=32)
    vdev_class: Literal["data", "special", "spare"] | None = None
    health: Literal["ONLINE", "AVAILABLE"] | None = None
    temperature_c: int | None = Field(default=None, ge=15, le=60)
    power_on_hours: int | None = Field(default=None, ge=0, le=200_000)
    bytes_read: int | None = Field(default=None, ge=0)
    bytes_written: int | None = Field(default=None, ge=0)
    event: PublicDemoEvent | None = None

    @model_validator(mode="after")
    def validate_presence_shape(self) -> "PublicDemoSlot":
        populated = self.state != SlotState.empty
        values = (self.device_name, self.serial, self.model, self.size_bytes, self.size_human)
        if populated and any(value is None for value in values):
            raise ValueError("populated synthetic slots require device, serial, model, and size fields")
        if not populated and any(value is not None for value in values):
            raise ValueError("empty synthetic slots cannot carry disk identity fields")
        return self


class PublicDemoStorageSlot(FixtureModel):
    slot_index: int = Field(ge=0, le=15)
    occupied: bool
    device_name: str | None = Field(default=None, pattern=r"^demo-(?:boot|nvme)-[0-9]{2}$", max_length=32)
    serial: str | None = Field(default=None, pattern=r"^DEMO-SN-(?:BOOT|NVME)-[0-9]{4}$", max_length=32)
    model: str | None = Field(default=None, pattern=r"^Demo [A-Za-z0-9 -]+$", max_length=64)
    size_bytes: int | None = Field(default=None, ge=1)
    size_human: str | None = Field(default=None, max_length=32)
    pool_name: str | None = Field(default=None, pattern=r"^demo-[a-z0-9-]+$", max_length=64)
    health: Literal["ONLINE"] | None = None
    temperature_c: int | None = Field(default=None, ge=15, le=70)
    power_on_hours: int | None = Field(default=None, ge=0, le=200_000)
    bytes_read: int | None = Field(default=None, ge=0)
    bytes_written: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_occupied_shape(self) -> "PublicDemoStorageSlot":
        values = (self.device_name, self.serial, self.model, self.size_bytes, self.size_human)
        if self.occupied and any(value is None for value in values):
            raise ValueError("occupied synthetic storage slots require identity and size fields")
        if not self.occupied and any(value is not None for value in values):
            raise ValueError("empty synthetic storage slots cannot carry disk identity fields")
        return self


class PublicDemoStorageView(FixtureModel):
    id: Literal["boot-doms", "nvme-carrier-x4"]
    label: Literal["Demo Boot Modules", "Demo 4x NVMe Carrier"]
    kind: Literal["boot_devices", "nvme_carrier"]
    template_id: Literal["satadom-pair-2", "nvme-carrier-4"]
    order: int = Field(ge=0, le=100)
    slots: list[PublicDemoStorageSlot] = Field(min_length=1, max_length=16)


class PublicDemoFixture(FixtureModel):
    schema_version: Literal[1]
    provenance: Literal["synthetic"]
    generated_at: datetime
    history_window_hours: Literal[168]
    history_sample_offsets_hours: list[int] = Field(min_length=2, max_length=24)
    system: PublicDemoSystem
    enclosure: PublicDemoEnclosure
    slots: list[PublicDemoSlot] = Field(min_length=60, max_length=60)
    storage_views: list[PublicDemoStorageView] = Field(min_length=2, max_length=2)

    @field_validator("generated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("generated_at must be timezone-aware UTC")
        return value

    @field_validator("history_sample_offsets_hours")
    @classmethod
    def validate_offsets(cls, value: list[int]) -> list[int]:
        if value != sorted(set(value), reverse=True) or value[-1] != 0:
            raise ValueError("history offsets must be unique, descending, and end at zero")
        if any(offset < 0 or offset > PUBLIC_DEMO_HISTORY_WINDOW_HOURS for offset in value):
            raise ValueError("history offsets must stay inside the fixture window")
        return value

    @model_validator(mode="after")
    def validate_complete_fixture(self) -> "PublicDemoFixture":
        if [slot.slot for slot in self.slots] != list(range(60)):
            raise ValueError("public demo fixture must declare slots 0 through 59 in order")
        if {view.id for view in self.storage_views} != {"boot-doms", "nvme-carrier-x4"}:
            raise ValueError("public demo fixture must declare both synthetic storage views")
        for view in self.storage_views:
            slot_indices = {slot.slot_index for slot in view.slots}
            if len(slot_indices) != len(view.slots):
                raise ValueError(f"storage view {view.id} has duplicate slot indices")
            template = get_storage_view_template(view.template_id)
            if template is None:
                raise ValueError(f"storage view {view.id} references an unknown template")
            template_slot_indices = {
                slot_index
                for row in template.slot_layout
                for slot_index in row
                if slot_index is not None
            }
            if slot_indices != template_slot_indices:
                expected = ", ".join(str(slot_index) for slot_index in sorted(template_slot_indices))
                raise ValueError(
                    f"storage view {view.id} must declare exactly template slots {expected}"
                )
        return self


@dataclass(frozen=True)
class PublicDemoSnapshotBundle:
    primary_snapshot: InventorySnapshot
    live_enclosure_snapshots: dict[str, InventorySnapshot]
    smart_summary_cache: dict[str, dict[str, Any]]
    live_enclosure_smart_summary_cache: dict[str, dict[str, dict[str, Any]]]
    storage_view_runtime: StorageViewRuntimePayload
    storage_view_smart_summary_cache: dict[str, dict[str, dict[str, Any]]]


class PublicDemoHistoryBackend:
    configured = True

    def __init__(self, fixture: PublicDemoFixture) -> None:
        self.fixture = fixture

    async def get_status(self) -> dict[str, Any]:
        tracked = len(self.fixture.slots) + sum(len(view.slots) for view in self.fixture.storage_views)
        samples = tracked * len(self.fixture.history_sample_offsets_hours) * 4
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "counts": {"tracked_slots": tracked, "metric_samples": samples},
            "collector": {"last_completed_at": self.fixture.generated_at.isoformat()},
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
        **_: Any,
    ) -> dict[int, dict[str, Any]]:
        source_slots = _history_source_slots(self.fixture, enclosure_id)
        return {
            slot: _history_payload(
                self.fixture,
                source_slots.get(slot),
                slot=slot,
                system_id=system_id,
                enclosure_id=enclosure_id,
            )
            for slot in slots
        }


def load_public_demo_fixture(path: Path = PUBLIC_DEMO_FIXTURE_PATH) -> PublicDemoFixture:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load deterministic public demo fixture: {path}") from exc
    fixture = PublicDemoFixture.model_validate(payload)
    validate_public_demo_fixture_privacy(fixture)
    return fixture


def validate_public_demo_fixture_privacy(fixture: PublicDemoFixture) -> None:
    fixture_text = fixture.model_dump_json()
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(fixture_text):
            raise ValueError(f"public demo fixture contains forbidden {label}")
    identifiers = [fixture.system.id, fixture.enclosure.id, *(view.id for view in fixture.storage_views)]
    if any(not SYNTHETIC_ID_PATTERN.fullmatch(value) for value in identifiers):
        raise ValueError("public demo fixture contains a non-synthetic identifier")
    serials = [
        slot.serial
        for slot in [*fixture.slots, *(item for view in fixture.storage_views for item in view.slots)]
        if slot.serial
    ]
    if any(not SYNTHETIC_SERIAL_PATTERN.fullmatch(value) for value in serials):
        raise ValueError("public demo fixture contains a non-synthetic serial")


def build_public_demo_snapshot_bundle(
    *,
    fixture: PublicDemoFixture | None = None,
    settings: Settings | None = None,
) -> PublicDemoSnapshotBundle:
    source = fixture or load_public_demo_fixture()
    resolved_settings = settings or Settings()
    profile = ProfileRegistry(resolved_settings).get(source.enclosure.profile_id)
    if profile is None:
        raise RuntimeError(f"Built-in public demo profile is missing: {source.enclosure.profile_id}")
    profile = profile.model_copy(
        update={
            "eyebrow": "Synthetic public demo / 60-bay top-loader",
            "summary": "Invented deterministic disks demonstrate enclosure, pool, history, and saved-view workflows.",
            "panel_title": source.enclosure.label,
        }
    )
    systems = [SystemOption(id=source.system.id, label=source.system.label, platform=source.system.platform)]
    enclosures = [
        EnclosureOption(
            id=source.enclosure.id,
            label=source.enclosure.label,
            name=source.enclosure.name,
            profile_id=profile.id,
            rows=profile.rows,
            columns=profile.columns,
            slot_count=profile.slot_count,
            slot_layout=profile.slot_layout,
        )
    ]
    slots = [_slot_view(source, fixture_slot, profile.slot_layout) for fixture_slot in source.slots]
    snapshot = InventorySnapshot(
        slots=slots,
        layout_rows=profile.slot_layout,
        layout_slot_count=len(slots),
        layout_columns=profile.columns,
        last_updated=source.generated_at,
        generated_at=source.generated_at,
        refresh_interval_seconds=30,
        selected_system_id=source.system.id,
        selected_system_label=source.system.label,
        selected_system_platform=source.system.platform,
        selected_enclosure_id=source.enclosure.id,
        selected_enclosure_label=source.enclosure.label,
        selected_enclosure_name=source.enclosure.name,
        selected_profile=profile,
        systems=systems,
        enclosures=enclosures,
        platform_context={
            "platform": "Synthetic CORE-compatible demo",
            "demo_fixture": True,
            "demo_source": "synthetic",
            "identifier_policy": "Every identifier is invented and uses a DEMO prefix.",
        },
        sources={
            "api": SourceStatus(enabled=False, ok=True, message="Static synthetic fixture; no API is configured."),
            "ssh": SourceStatus(enabled=False, ok=True, message="Static synthetic fixture; no SSH is configured."),
        },
        summary=InventorySummary(
            disk_count=sum(slot.state != SlotState.empty for slot in slots),
            pool_count=len({slot.pool_name for slot in slots if slot.pool_name}),
            enclosure_count=1,
            mapped_slot_count=sum(slot.state != SlotState.empty for slot in slots),
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
        warnings=[
            "All displayed systems, disks, metrics, events, and identifiers are deterministic synthetic demo data.",
            "No appliance, credentials, local history, or LED actions are connected to this static artifact.",
        ],
    )
    smart = {
        str(slot.slot): _smart_summary(slot).model_dump(mode="json", exclude_none=True)
        for slot in source.slots
        if slot.state != SlotState.empty
    }
    runtime = _storage_view_runtime(source)
    storage_smart = {
        view.id: {
            str(slot.slot_index): _smart_summary(slot).model_dump(mode="json", exclude_none=True)
            for slot in view.slots
            if slot.occupied
        }
        for view in source.storage_views
    }
    return PublicDemoSnapshotBundle(
        primary_snapshot=snapshot,
        live_enclosure_snapshots={source.enclosure.id: snapshot},
        smart_summary_cache=smart,
        live_enclosure_smart_summary_cache={source.enclosure.id: smart},
        storage_view_runtime=runtime,
        storage_view_smart_summary_cache=storage_smart,
    )


async def build_public_demo_html(
    *,
    fixture_path: Path = PUBLIC_DEMO_FIXTURE_PATH,
    settings: Settings | None = None,
) -> str:
    fixture = load_public_demo_fixture(fixture_path)
    resolved_settings = settings or Settings()
    templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
    register_script_json_filters(templates.env)
    bundle = build_public_demo_snapshot_bundle(fixture=fixture, settings=resolved_settings)
    exporter = SnapshotExportService(
        resolved_settings,
        PublicDemoHistoryBackend(fixture),
        templates,
    )
    rendered = await exporter.build_enclosure_snapshot_html(
        request=build_static_demo_request(),
        snapshot=bundle.primary_snapshot,
        smart_summary_cache=bundle.smart_summary_cache,
        live_enclosure_snapshots=bundle.live_enclosure_snapshots,
        live_enclosure_smart_summary_cache=bundle.live_enclosure_smart_summary_cache,
        storage_view_runtime=bundle.storage_view_runtime,
        storage_view_smart_summary_cache=bundle.storage_view_smart_summary_cache,
        selected_slot=None,
        history_window_hours=fixture.history_window_hours,
        history_panel_open=True,
        io_chart_mode="total",
        generated_at=fixture.generated_at,
        identifier_policy_label="Synthetic IDs",
        identifier_policy_note="Generated only from schema-validated, deterministic, checked-in synthetic values.",
    )
    return rendered.html


def build_static_demo_request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("public-demo.invalid", 443),
            "root_path": "",
            "app": None,
        }
    )
    request.scope["app"] = type(
        "StaticDemoApp",
        (),
        {"url_path_for": lambda _, name, **params: URLPath(f"/static/{params['path']}")},
    )()
    return request


def _slot_view(
    fixture: PublicDemoFixture,
    source: PublicDemoSlot,
    layout: list[list[int | None]],
) -> SlotView:
    coordinates = {
        int(slot): (row_index, column_index)
        for row_index, row in enumerate(layout)
        for column_index, slot in enumerate(row)
        if slot is not None
    }
    row_index, column_index = coordinates[source.slot]
    occupied = source.state != SlotState.empty
    return SlotView(
        slot=source.slot,
        slot_label=f"{source.slot:02d}",
        row_index=row_index,
        column_index=column_index,
        enclosure_id=fixture.enclosure.id,
        enclosure_label=fixture.enclosure.label,
        enclosure_name=fixture.enclosure.name,
        present=True,
        state=source.state,
        identify_active=False,
        device_name=source.device_name,
        smart_device_names=[source.device_name] if source.device_name else [],
        serial=source.serial,
        model=source.model,
        size_bytes=source.size_bytes,
        size_human=source.size_human,
        gptid=f"demo-gptid-core-{source.slot:04d}" if occupied else None,
        persistent_id_label="Synthetic persistent ID" if occupied else None,
        pool_name=source.pool_name,
        vdev_name=source.vdev_name,
        vdev_class=source.vdev_class,
        topology_label=(f"{source.pool_name} > {source.vdev_name} > demo disk" if source.pool_name else None),
        health=source.health,
        temperature_c=source.temperature_c,
        logical_unit_id=f"demo-lun-core-{source.slot:04d}" if occupied else None,
        sas_address=f"demo-sas-core-{source.slot:04d}" if occupied else None,
        enclosure_identifier="demo-enclosure-address-0001" if occupied else None,
        led_supported=False,
        led_reason="LED actions are disabled in the static synthetic public demo.",
        mapping_source="synthetic_fixture" if occupied else "empty",
        search_text=f"demo slot {source.slot} {source.model or 'empty'} {source.pool_name or ''}",
        raw_status={"fixture_provenance": "synthetic"},
    )


def _storage_view_runtime(fixture: PublicDemoFixture) -> StorageViewRuntimePayload:
    views: list[StorageViewRuntimeView] = []
    for source_view in fixture.storage_views:
        config = StorageViewConfig(
            id=source_view.id,
            label=source_view.label,
            kind=source_view.kind,
            template_id=source_view.template_id,
            enabled=True,
            order=source_view.order,
            render=StorageViewRenderConfig(show_in_main_ui=True, show_in_admin_ui=True),
            binding=StorageViewBindingConfig(mode="auto", target_system_id=fixture.system.id),
            layout_overrides=StorageViewLayoutOverridesConfig(),
        )
        source_slots = {slot.slot_index: slot for slot in source_view.slots}
        layout = build_storage_view_rows(config)
        template = get_storage_view_template(source_view.template_id)
        runtime_slots = [
            _storage_runtime_slot(
                fixture,
                config,
                source_slots[slot_index],
                assignment_rank=rank,
            )
            for rank, slot_index in enumerate(ordered_storage_view_slot_indices(config), start=1)
        ]
        views.append(
            StorageViewRuntimeView(
                id=source_view.id,
                label=source_view.label,
                kind=source_view.kind,
                template_id=source_view.template_id,
                panel_title=source_view.label,
                edge_label=template.summary if template else None,
                face_style="nvme-carrier" if source_view.kind == "nvme_carrier" else "generic",
                latch_edge="bottom",
                enabled=True,
                render=config.render.model_dump(mode="json"),
                binding=config.binding.model_dump(mode="json"),
                order=source_view.order,
                template_label=template.label if template else source_view.template_id,
                slot_layout=layout,
                source="inventory_binding",
                backing_enclosure_id=fixture.enclosure.id,
                backing_enclosure_label=fixture.enclosure.label,
                notes=["Deterministic synthetic saved view; no live device binding is present."],
                matched_count=sum(slot.occupied for slot in runtime_slots),
                slot_count=len(runtime_slots),
                slots=runtime_slots,
            )
        )
    return StorageViewRuntimePayload(
        system_id=fixture.system.id,
        system_label=fixture.system.label,
        views=views,
    )


def _storage_runtime_slot(
    fixture: PublicDemoFixture,
    config: StorageViewConfig,
    source: PublicDemoStorageSlot,
    *,
    assignment_rank: int,
) -> StorageViewRuntimeSlot:
    return StorageViewRuntimeSlot(
        slot_index=source.slot_index,
        slot_label=storage_view_slot_label(config, source.slot_index),
        target_system_id=fixture.system.id,
        target_system_label=fixture.system.label,
        occupied=source.occupied,
        state="healthy" if source.occupied else "empty",
        source="inventory_candidate" if source.occupied else "placeholder",
        match_reasons=["synthetic fixture"] if source.occupied else [],
        placement_key=source.device_name or f"layout slot {assignment_rank}",
        assignment_rank=assignment_rank,
        device_name=source.device_name,
        smart_device_names=[source.device_name] if source.device_name else [],
        serial=source.serial,
        pool_name=source.pool_name,
        model=source.model,
        size_bytes=source.size_bytes,
        size_human=source.size_human,
        gptid=f"demo-gptid-{config.id}-{source.slot_index:04d}" if source.occupied else None,
        persistent_id_label="Synthetic persistent ID" if source.occupied else None,
        health=source.health,
        temperature_c=source.temperature_c,
        logical_block_size=512 if source.occupied else None,
        physical_block_size=4096 if source.occupied else None,
        logical_unit_id=f"demo-lun-{config.id}-{source.slot_index:04d}" if source.occupied else None,
        transport_address=source.device_name,
        description="Synthetic fixed storage view slot" if source.occupied else None,
        led_supported=False,
        slot_size=storage_view_slot_size(config, source.slot_index),
    )


def _smart_summary(source: PublicDemoSlot | PublicDemoStorageSlot) -> SmartSummaryView:
    return SmartSummaryView(
        available=bool(getattr(source, "state", None) != SlotState.empty and getattr(source, "occupied", True)),
        temperature_c=source.temperature_c,
        smart_health_status=source.health,
        power_on_hours=source.power_on_hours,
        power_on_days=int((source.power_on_hours or 0) / 24),
        logical_block_size=512,
        physical_block_size=4096,
        bytes_read=source.bytes_read,
        bytes_written=source.bytes_written,
        annualized_bytes_read=(source.bytes_read or 0) * 4,
        annualized_bytes_written=(source.bytes_written or 0) * 4,
        rotation_rate_rpm=0 if source.model and "Flash" in source.model else 7200,
        form_factor="2.5 inches" if source.model and "Flash" in source.model else "3.5 inches",
        firmware_version="DEMO-1.0",
        transport_protocol="Synthetic",
        logical_unit_id=(
            f"demo-lun-{getattr(source, 'slot', getattr(source, 'slot_index', 0)):04d}"
            if source.model
            else None
        ),
        message="Synthetic SMART summary generated from the checked-in public fixture.",
    )


def _history_source_slots(
    fixture: PublicDemoFixture,
    enclosure_id: str | None,
) -> dict[int, PublicDemoSlot | PublicDemoStorageSlot]:
    if enclosure_id == fixture.enclosure.id or not enclosure_id:
        return {slot.slot: slot for slot in fixture.slots}
    if enclosure_id and enclosure_id.startswith("storage-view:"):
        view_id = enclosure_id.removeprefix("storage-view:")
        view = next((item for item in fixture.storage_views if item.id == view_id), None)
        if view is not None:
            return {slot.slot_index: slot for slot in view.slots}
    return {}


def _history_payload(
    fixture: PublicDemoFixture,
    source: PublicDemoSlot | PublicDemoStorageSlot | None,
    *,
    slot: int,
    system_id: str | None,
    enclosure_id: str | None,
) -> dict[str, Any]:
    available = source is not None and (
        getattr(source, "state", None) != SlotState.empty and getattr(source, "occupied", True)
    )
    metrics: dict[str, list[dict[str, Any]]] = {}
    if available and source is not None:
        offsets = fixture.history_sample_offsets_hours
        bases = {
            "temperature_c": source.temperature_c or 30,
            "power_on_hours": source.power_on_hours or 0,
            "bytes_read": source.bytes_read or 0,
            "bytes_written": source.bytes_written or 0,
            "annualized_bytes_read": (source.bytes_read or 0) * 4,
            "annualized_bytes_written": (source.bytes_written or 0) * 4,
        }
        for metric_name, base in bases.items():
            samples: list[dict[str, Any]] = []
            for index, offset in enumerate(offsets):
                value = base
                if metric_name == "temperature_c":
                    value = base if offset == 0 else base + (-1, 0, 1, 0)[index % 4]
                elif metric_name == "power_on_hours":
                    value = max(0, base - offset)
                elif metric_name in {"bytes_read", "bytes_written"}:
                    value = max(0, base - (offset * 1_000_000))
                samples.append(
                    {
                        "observed_at": (fixture.generated_at - timedelta(hours=offset)).isoformat(),
                        "value": value,
                    }
                )
            metrics[metric_name] = samples
    events = []
    event = getattr(source, "event", None) if source is not None else None
    if event is not None:
        events.append(
            {
                "observed_at": (fixture.generated_at - timedelta(hours=event.hours_before_capture)).isoformat(),
                "event_type": event.event_type,
                "previous_value": event.previous_value,
                "current_value": event.current_value,
                "details_json": json.dumps({"detail": event.detail}, sort_keys=True, separators=(",", ":")),
            }
        )
    return {
        "configured": True,
        "available": available,
        "detail": None if available else "No synthetic history is assigned to this empty slot.",
        "slot": slot,
        "system_id": system_id,
        "enclosure_id": enclosure_id,
        "metrics": metrics,
        "events": events,
        "sample_counts": {name: len(values) for name, values in metrics.items()},
        "latest_values": {name: values[-1]["value"] for name, values in metrics.items()},
        "disk_history": {},
    }


PUBLIC_DEMO_GENERATED_AT = load_public_demo_fixture().generated_at
PUBLIC_DEMO_SYSTEM_ID = load_public_demo_fixture().system.id
PUBLIC_DEMO_SYSTEM_LABEL = load_public_demo_fixture().system.label
PUBLIC_DEMO_ENCLOSURE_ID = load_public_demo_fixture().enclosure.id
PUBLIC_DEMO_ENCLOSURE_LABEL = load_public_demo_fixture().enclosure.label
