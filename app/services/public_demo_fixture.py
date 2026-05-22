from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from starlette.datastructures import URLPath
from starlette.requests import Request

from app.config import Settings, StorageViewConfig, SystemConfig, get_settings
from app.main import templates
from app.models.domain import (
    EnclosureOption,
    EnclosureProfileView,
    InventorySnapshot,
    InventorySummary,
    SlotState,
    SlotView,
    SourceStatus,
    StorageViewRuntimePayload,
    StorageViewRuntimeSlot,
    StorageViewRuntimeView,
    SystemOption,
)
from app.services.profile_registry import CORE_CSE_946_PROFILE_ID, ProfileRegistry
from app.services.snapshot_export import SnapshotExportService
from app.services.storage_view_templates import get_storage_view_template
from app.services.storage_views import (
    build_storage_view_rows,
    ordered_storage_view_slot_indices,
    storage_view_slot_label,
    storage_view_slot_size,
)


PUBLIC_DEMO_GENERATED_AT = datetime(2026, 5, 15, 23, 46, 42, 854163, tzinfo=timezone.utc)
PUBLIC_DEMO_SYSTEM_ID = "tn-core"
PUBLIC_DEMO_SYSTEM_LABEL = "TN Core"
PUBLIC_DEMO_ENCLOSURE_ID = "tn-core-cse-946-top-loader"
PUBLIC_DEMO_ENCLOSURE_LABEL = "CSE-946 Top Loader"
PUBLIC_DEMO_HISTORY_WINDOW_HOURS = 7 * 24

PRIMARY_SCOPE_KEY = PUBLIC_DEMO_ENCLOSURE_ID
STORAGE_VIEW_SOURCE_SLOT_OFFSET = 10_000
LIVE_HISTORY_LOOKBACK_PADDING_HOURS = 6

PARTIAL_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?:0x)?(?:naa\.)?5[0-9a-fA-F]{15,}(?![0-9A-Za-z])")
GPTID_PATTERN = re.compile(r"\bgptid/[0-9a-fA-F-]{16,}\b")
_LIVE_DEMO_SOURCE_CACHE: LiveDemoSource | None = None


@dataclass(frozen=True)
class DemoIdentifiers:
    serial: str
    gptid: str
    logical_unit_id: str
    sas_address: str
    attached_sas_address: str
    enclosure_identifier: str


@dataclass(frozen=True)
class LiveDemoSource:
    system_id: str
    system_label: str
    platform: str
    enclosure_key: str
    enclosure_label: str
    slot_rows: dict[int, dict[str, Any]]
    slot_details: dict[str, dict[str, Any]]
    latest_metrics: dict[str, dict[int, dict[str, Any]]]
    storage_views: list[StorageViewConfig]


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

    async def get_status(self) -> dict[str, Any]:
        with _connect_history_db() as connection:
            source = _load_live_demo_source(get_settings(), connection=connection)
            scopes = [source.enclosure_key, *(f"storage-view:{view.id}" for view in source.storage_views)]
            placeholders = ",".join("?" for _ in scopes)
            metric_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM metric_samples
                WHERE system_id = ? AND enclosure_key IN ({placeholders})
                """,
                [source.system_id, *scopes],
            ).fetchone()[0]
            slot_count = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM slot_state_current
                WHERE system_id = ? AND enclosure_key IN ({placeholders})
                """,
                [source.system_id, *scopes],
            ).fetchone()[0]

        return {
            "configured": True,
            "available": True,
            "detail": None,
            "counts": {"tracked_slots": slot_count, "metric_samples": metric_count},
            "collector": {"last_completed_at": PUBLIC_DEMO_GENERATED_AT.isoformat()},
            "scopes": [
                {
                    "system_id": PUBLIC_DEMO_SYSTEM_ID,
                    "enclosure_id": PRIMARY_SCOPE_KEY,
                    "label": PUBLIC_DEMO_ENCLOSURE_LABEL,
                },
                *(
                    {
                        "system_id": PUBLIC_DEMO_SYSTEM_ID,
                        "enclosure_id": f"storage-view:{view.id}",
                        "label": view.label,
                    }
                    for view in source.storage_views
                ),
            ],
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, Any]]:
        if not slots:
            return {}
        with _connect_history_db() as connection:
            source = _load_live_demo_source(get_settings(), connection=connection)
            source_scope = _source_scope_for_public_enclosure(source, enclosure_id)
            return {
                slot: _history_payload_for_slot(
                    connection,
                    source=source,
                    public_system_id=system_id,
                    public_enclosure_id=enclosure_id,
                    source_scope=source_scope,
                    slot=slot,
                    window_hours=window_hours,
                )
                for slot in slots
            }


def build_public_demo_snapshot_bundle(*, settings: Settings | None = None) -> PublicDemoSnapshotBundle:
    resolved_settings = settings or get_settings()
    with _connect_history_db() as connection:
        source = _load_live_demo_source(resolved_settings, connection=connection)

    profile = _core_profile(resolved_settings)
    systems = [SystemOption(id=PUBLIC_DEMO_SYSTEM_ID, label=PUBLIC_DEMO_SYSTEM_LABEL, platform="core")]
    enclosures = [
        EnclosureOption(
            id=PUBLIC_DEMO_ENCLOSURE_ID,
            label=PUBLIC_DEMO_ENCLOSURE_LABEL,
            name=source.enclosure_label,
            profile_id=profile.id,
            rows=profile.rows,
            columns=profile.columns,
            slot_count=60,
            slot_layout=profile.slot_layout,
        )
    ]
    slots = _core_slots(profile, source)
    snapshot = _build_snapshot(
        profile=profile,
        systems=systems,
        enclosures=enclosures,
        slots=slots,
        source=source,
    )
    smart = _smart_cache_for_slots(
        source,
        scope_key=source.enclosure_key,
        slots=snapshot.slots,
        scope_name="core",
    )
    storage_view_runtime = _storage_view_runtime(source)
    storage_view_smart = _storage_view_smart_summary_cache(source, storage_view_runtime)
    return PublicDemoSnapshotBundle(
        primary_snapshot=snapshot,
        live_enclosure_snapshots={PUBLIC_DEMO_ENCLOSURE_ID: snapshot},
        smart_summary_cache=smart,
        live_enclosure_smart_summary_cache={PUBLIC_DEMO_ENCLOSURE_ID: smart},
        storage_view_runtime=storage_view_runtime,
        storage_view_smart_summary_cache=storage_view_smart,
    )


async def build_public_demo_html(*, settings: Settings | None = None) -> str:
    resolved_settings = settings or get_settings()
    bundle = build_public_demo_snapshot_bundle(settings=resolved_settings)
    exporter = SnapshotExportService(resolved_settings, PublicDemoHistoryBackend(), templates)
    rendered = await exporter.build_enclosure_snapshot_html(
        request=build_static_demo_request(),
        snapshot=bundle.primary_snapshot,
        smart_summary_cache=bundle.smart_summary_cache,
        live_enclosure_snapshots=bundle.live_enclosure_snapshots,
        live_enclosure_smart_summary_cache=bundle.live_enclosure_smart_summary_cache,
        storage_view_runtime=bundle.storage_view_runtime,
        storage_view_smart_summary_cache=bundle.storage_view_smart_summary_cache,
        selected_slot=None,
        history_window_hours=PUBLIC_DEMO_HISTORY_WINDOW_HOURS,
        history_panel_open=True,
        io_chart_mode="total",
        generated_at=PUBLIC_DEMO_GENERATED_AT,
        identifier_policy_label="Scrambled IDs",
        identifier_policy_note=(
            "Live-derived CORE data with serial, SAS, NAA, GPTID, and similar disk identifiers scrambled."
        ),
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@contextmanager
def _connect_history_db() -> Iterator[sqlite3.Connection]:
    db_path = _repo_root() / "history" / "history.db"
    if not db_path.exists():
        raise RuntimeError(
            "Public demo release generation requires local ignored history/history.db so the demo can mirror the live CORE system."
        )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _load_live_demo_source(settings: Settings, *, connection: sqlite3.Connection) -> LiveDemoSource:
    global _LIVE_DEMO_SOURCE_CACHE
    if _LIVE_DEMO_SOURCE_CACHE is not None:
        return _LIVE_DEMO_SOURCE_CACHE

    source_system_id = _find_source_system_id(settings, connection)
    source_system = _settings_system(settings, source_system_id)
    source_system_label = source_system.label if source_system and source_system.label else "TrueNAS CORE"
    source_platform = source_system.truenas.platform if source_system else "core"
    enclosure = _find_source_enclosure(connection, source_system_id)
    slot_rows = {
        int(row["slot"]): dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM slot_state_current
            WHERE system_id = ? AND enclosure_key = ?
            ORDER BY slot
            """,
            (source_system_id, enclosure["enclosure_key"]),
        )
    }
    storage_views = _source_storage_views(source_system, connection, source_system_id)
    scope_keys = [enclosure["enclosure_key"], *(f"storage-view:{view.id}" for view in storage_views)]
    _LIVE_DEMO_SOURCE_CACHE = LiveDemoSource(
        system_id=source_system_id,
        system_label=source_system_label,
        platform=source_platform,
        enclosure_key=enclosure["enclosure_key"],
        enclosure_label=enclosure["enclosure_label"] or PUBLIC_DEMO_ENCLOSURE_LABEL,
        slot_rows=slot_rows,
        slot_details=_load_slot_detail_cache(settings),
        latest_metrics=_load_latest_metrics(connection, source_system_id, scope_keys),
        storage_views=storage_views,
    )
    return _LIVE_DEMO_SOURCE_CACHE


def _settings_system(settings: Settings, system_id: str | None) -> SystemConfig | None:
    for system in settings.systems:
        if system.id == system_id:
            return system
    return None


def _find_source_system_id(settings: Settings, connection: sqlite3.Connection) -> str:
    preferred_ids = [
        settings.default_system_id,
        *(system.id for system in settings.systems if system.truenas.platform == "core"),
    ]
    for system_id in preferred_ids:
        if not system_id:
            continue
        row = connection.execute(
            """
            SELECT 1
            FROM slot_state_current
            WHERE system_id = ? AND enclosure_key NOT LIKE 'storage-view:%'
            GROUP BY enclosure_key
            HAVING COUNT(*) >= 60
            LIMIT 1
            """,
            (system_id,),
        ).fetchone()
        if row:
            return system_id

    row = connection.execute(
        """
        SELECT system_id
        FROM slot_state_current
        WHERE enclosure_key NOT LIKE 'storage-view:%'
        GROUP BY system_id, enclosure_key
        HAVING COUNT(*) >= 60
        ORDER BY COUNT(*) DESC, system_id
        LIMIT 1
        """
    ).fetchone()
    if row:
        return str(row["system_id"])
    raise RuntimeError("Public demo generation could not find a 60-bay CORE source in history/history.db.")


def _find_source_enclosure(connection: sqlite3.Connection, source_system_id: str) -> dict[str, str | None]:
    row = connection.execute(
        """
        SELECT enclosure_key, enclosure_id, enclosure_label, COUNT(*) AS slot_count
        FROM slot_state_current
        WHERE system_id = ? AND enclosure_key NOT LIKE 'storage-view:%'
        GROUP BY enclosure_key, enclosure_id, enclosure_label
        HAVING COUNT(*) >= 60
        ORDER BY CASE WHEN COUNT(*) = 60 THEN 0 ELSE 1 END, COUNT(*) DESC, enclosure_label
        LIMIT 1
        """,
        (source_system_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Public demo generation could not find a 60-bay enclosure for {source_system_id}.")
    return dict(row)


def _source_storage_views(
    source_system: SystemConfig | None,
    connection: sqlite3.Connection,
    source_system_id: str,
) -> list[StorageViewConfig]:
    views: list[StorageViewConfig] = []
    for view in sorted(source_system.storage_views if source_system else [], key=lambda item: (item.order, item.label, item.id)):
        if not view.enabled:
            continue
        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM slot_state_current
            WHERE system_id = ? AND enclosure_key = ?
            """,
            (source_system_id, f"storage-view:{view.id}"),
        ).fetchone()[0]
        if count:
            views.append(view)
    return views


def _load_slot_detail_cache(settings: Settings) -> dict[str, dict[str, Any]]:
    path = Path(settings.paths.slot_detail_cache_file)
    if not path.is_absolute():
        path = _repo_root() / path
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    details = payload.get("slot_details")
    return details if isinstance(details, dict) else {}


def _load_latest_metrics(
    connection: sqlite3.Connection,
    source_system_id: str,
    scope_keys: list[str],
) -> dict[str, dict[int, dict[str, Any]]]:
    if not scope_keys:
        return {}
    placeholders = ",".join("?" for _ in scope_keys)
    latest: dict[str, dict[int, dict[str, Any]]] = {}
    for row in connection.execute(
        f"""
        SELECT enclosure_key, slot, metric_name, value_integer, value_real, observed_at
        FROM metric_samples
        WHERE system_id = ? AND enclosure_key IN ({placeholders}) AND observed_at <= ?
        ORDER BY enclosure_key, slot, metric_name, observed_at
        """,
        [source_system_id, *scope_keys, PUBLIC_DEMO_GENERATED_AT.isoformat()],
    ):
        scope_metrics = latest.setdefault(str(row["enclosure_key"]), {})
        slot_metrics = scope_metrics.setdefault(int(row["slot"]), {})
        slot_metrics[str(row["metric_name"])] = _history_sample_value(dict(row))
    return latest


def _core_profile(settings: Settings) -> EnclosureProfileView:
    profile = ProfileRegistry(settings).get(CORE_CSE_946_PROFILE_ID)
    if profile is None:
        raise RuntimeError(f"Built-in public demo profile is missing: {CORE_CSE_946_PROFILE_ID}")
    return profile.model_copy(
        update={
            "eyebrow": "TrueNAS CORE / Supermicro CSE-946 Public Demo",
            "summary": "Live-derived CORE 60-bay sample with only critical disk identifiers scrambled.",
            "panel_title": PUBLIC_DEMO_ENCLOSURE_LABEL,
        }
    )


def _build_snapshot(
    *,
    profile: EnclosureProfileView,
    systems: list[SystemOption],
    enclosures: list[EnclosureOption],
    slots: list[SlotView],
    source: LiveDemoSource,
) -> InventorySnapshot:
    populated_count = sum(1 for slot in slots if slot.device_name or slot.model)
    pool_count = len({slot.pool_name for slot in slots if slot.pool_name})
    mapped_count = sum(1 for slot in slots if slot.mapping_source and slot.mapping_source != "empty")
    return InventorySnapshot(
        slots=slots,
        layout_rows=profile.slot_layout,
        layout_slot_count=sum(1 for row in profile.slot_layout for slot in row if slot is not None),
        layout_columns=profile.columns,
        last_updated=PUBLIC_DEMO_GENERATED_AT,
        generated_at=PUBLIC_DEMO_GENERATED_AT,
        refresh_interval_seconds=30,
        selected_system_id=PUBLIC_DEMO_SYSTEM_ID,
        selected_system_label=PUBLIC_DEMO_SYSTEM_LABEL,
        selected_system_platform="core",
        selected_enclosure_id=PUBLIC_DEMO_ENCLOSURE_ID,
        selected_enclosure_label=PUBLIC_DEMO_ENCLOSURE_LABEL,
        selected_enclosure_name=source.enclosure_label,
        selected_profile=profile,
        systems=systems,
        enclosures=enclosures,
        platform_context={
            "platform": "TrueNAS CORE",
            "chassis": "Supermicro CSE-946",
            "demo_fixture": True,
            "demo_source": "live-derived",
            "identifier_policy": "Serial, SAS, NAA, GPTID, and similar disk identifiers are scrambled.",
        },
        sources={
            "api": SourceStatus(enabled=False, ok=True, message="Static public demo fixture; no live CORE API is configured."),
            "ssh": SourceStatus(enabled=False, ok=True, message="Static public demo fixture; no SSH collection is configured."),
        },
        summary=InventorySummary(
            disk_count=populated_count,
            pool_count=pool_count,
            enclosure_count=len(enclosures),
            mapped_slot_count=mapped_count,
            manual_mapping_count=sum(len(view.binding.device_names) + len(view.binding.serials) for view in source.storage_views),
            ssh_slot_hint_count=60,
        ),
        warnings=[
            "Public demo is built from a live CORE-derived snapshot. Critical disk identifiers are scrambled.",
            "Live appliances, credentials, and LED actions are not connected in this static artifact.",
        ],
    )


def _core_slots(profile: EnclosureProfileView, source: LiveDemoSource) -> list[SlotView]:
    slots: list[SlotView] = []
    for row_index, row in enumerate(profile.slot_layout):
        for column_index, slot_number in enumerate(row):
            if slot_number is None:
                continue
            source_row = source.slot_rows.get(slot_number, {})
            details = _slot_detail(source, source.enclosure_key, slot_number)
            slot_fields = details.get("slot_fields") if isinstance(details.get("slot_fields"), dict) else {}
            state = _slot_state(source_row)
            occupied = state != SlotState.empty and bool(source_row.get("device_name") or slot_fields.get("device_name"))
            ids = _ids_for_slot("core", slot_number)
            persistent_id_label = _optional_text(source_row.get("persistent_id_label") or slot_fields.get("persistent_id_label"))
            logical_unit_id = ids.logical_unit_id if occupied and _has_source_identifier(source_row, slot_fields, "logical_unit_id") else None
            sas_address = ids.sas_address if occupied and _has_source_identifier(source_row, slot_fields, "sas_address") else None
            slots.append(
                SlotView(
                    slot=slot_number,
                    slot_label=_optional_text(source_row.get("slot_label")) or f"{slot_number:02d}",
                    row_index=row_index,
                    column_index=column_index,
                    enclosure_id=PUBLIC_DEMO_ENCLOSURE_ID,
                    enclosure_label=PUBLIC_DEMO_ENCLOSURE_LABEL,
                    enclosure_name=source.enclosure_label,
                    present=bool(source_row.get("present")),
                    state=state,
                    identify_active=bool(source_row.get("identify_active")),
                    device_name=_optional_text(slot_fields.get("device_name") or source_row.get("device_name")) if occupied else None,
                    smart_device_names=list(slot_fields.get("smart_device_names") or []) if occupied else [],
                    serial=ids.serial if occupied and _has_source_identifier(source_row, slot_fields, "serial") else None,
                    model=_optional_text(slot_fields.get("model") or source_row.get("model")) if occupied else None,
                    size_bytes=_optional_int(slot_fields.get("size_bytes")) if occupied else None,
                    size_human=_optional_text(slot_fields.get("size_human")) if occupied else None,
                    gptid=_persistent_id_for_label(persistent_id_label, ids) if occupied and persistent_id_label else None,
                    persistent_id_label=persistent_id_label if occupied else None,
                    pool_name=_optional_text(source_row.get("pool_name")) if occupied else None,
                    vdev_name=_optional_text(source_row.get("vdev_name")) if occupied else None,
                    vdev_class=_vdev_class(source_row),
                    topology_label=_optional_text(source_row.get("topology_label")) if occupied else None,
                    health=_optional_text(source_row.get("health")),
                    temperature_c=_metric_value(source, source.enclosure_key, slot_number, "temperature_c") if occupied else None,
                    logical_unit_id=logical_unit_id,
                    sas_address=sas_address,
                    enclosure_identifier=ids.enclosure_identifier if occupied else None,
                    led_supported=False,
                    led_reason="Live LED actions are disabled in the static public demo.",
                    mapping_source="live-derived-core-demo" if occupied else "empty",
                    raw_status={
                        "identifier_policy": "scrambled",
                        "source_enclosure_label": source.enclosure_label,
                        "attached_sas_address": ids.attached_sas_address if occupied and sas_address else None,
                    },
                )
            )
    return sorted(slots, key=lambda slot: slot.slot)


def _slot_state(source_row: dict[str, Any]) -> SlotState:
    raw_state = _optional_text(source_row.get("state"))
    try:
        return SlotState(raw_state or SlotState.unknown)
    except ValueError:
        return SlotState.unknown


def _vdev_class(source_row: dict[str, Any]) -> str | None:
    topology_label = _optional_text(source_row.get("topology_label"))
    if topology_label and ">" in topology_label:
        return topology_label.rsplit(">", 1)[-1].strip() or None
    vdev_name = _optional_text(source_row.get("vdev_name")) or ""
    if vdev_name.startswith("spare"):
        return "spare"
    if vdev_name.startswith("mirror"):
        return "special"
    if vdev_name:
        return "data"
    return None


def _storage_view_runtime(source: LiveDemoSource) -> StorageViewRuntimePayload:
    return StorageViewRuntimePayload(
        system_id=PUBLIC_DEMO_SYSTEM_ID,
        system_label=PUBLIC_DEMO_SYSTEM_LABEL,
        views=[_storage_view(source, view) for view in source.storage_views],
    )


def _storage_view(source: LiveDemoSource, storage_view: StorageViewConfig) -> StorageViewRuntimeView:
    source_scope = f"storage-view:{storage_view.id}"
    source_rows = _state_rows_for_scope(source, source_scope)
    layout_rows = build_storage_view_rows(storage_view)
    template = get_storage_view_template(storage_view.template_id)
    runtime_slots: list[StorageViewRuntimeSlot] = []
    for assignment_rank, slot_index in enumerate(ordered_storage_view_slot_indices(storage_view), start=1):
        source_row = source_rows.get(slot_index)
        label = storage_view_slot_label(storage_view, slot_index)
        runtime_slots.append(
            _storage_view_slot(
                source,
                storage_view,
                source_scope=source_scope,
                source_row=source_row,
                slot_index=slot_index,
                slot_label=label,
                assignment_rank=assignment_rank,
            )
        )
    return StorageViewRuntimeView(
        id=storage_view.id,
        label=storage_view.label,
        kind=storage_view.kind,
        template_id=storage_view.template_id,
        profile_id=storage_view.profile_id,
        profile_label=None,
        panel_title=storage_view.label,
        edge_label=template.summary if template else None,
        face_style="generic",
        latch_edge="bottom",
        enabled=storage_view.enabled,
        render=storage_view.render.model_dump(mode="json"),
        binding=_scrub_storage_view_binding(storage_view).model_dump(mode="json", exclude_none=True),
        order=storage_view.order,
        template_label=template.label if template else storage_view.template_id,
        slot_layout=layout_rows,
        source="inventory_binding",
        backing_enclosure_id=PUBLIC_DEMO_ENCLOSURE_ID,
        backing_enclosure_label=PUBLIC_DEMO_ENCLOSURE_LABEL,
        notes=[
            "Live-derived saved storage view from the TN Core public demo source; critical disk identifiers are scrambled.",
        ],
        matched_count=sum(1 for slot in runtime_slots if slot.occupied),
        slot_count=sum(1 for row in layout_rows for value in row if isinstance(value, int)),
        slots=runtime_slots,
    )


def _storage_view_slot(
    source: LiveDemoSource,
    storage_view: StorageViewConfig,
    *,
    source_scope: str,
    source_row: dict[str, Any] | None,
    slot_index: int,
    slot_label: str,
    assignment_rank: int,
) -> StorageViewRuntimeSlot:
    if not source_row:
        return StorageViewRuntimeSlot(
            slot_index=slot_index,
            slot_label=slot_label,
            target_system_id=PUBLIC_DEMO_SYSTEM_ID,
            target_system_label=PUBLIC_DEMO_SYSTEM_LABEL,
            occupied=False,
            state="empty",
            source="placeholder",
            assignment_rank=assignment_rank,
            placement_key=f"layout slot {assignment_rank}",
            slot_size=storage_view_slot_size(storage_view, slot_index),
        )

    detail = _slot_detail(source, source_scope, STORAGE_VIEW_SOURCE_SLOT_OFFSET + slot_index)
    slot_fields = detail.get("slot_fields") if isinstance(detail.get("slot_fields"), dict) else {}
    smart_fields = detail.get("smart_fields") if isinstance(detail.get("smart_fields"), dict) else {}
    ids = _ids_for_slot(_identifier_scope_for_storage_view(storage_view.id), slot_index)
    persistent_id_label = _optional_text(source_row.get("persistent_id_label") or slot_fields.get("persistent_id_label"))
    device_name = _optional_text(slot_fields.get("device_name") or source_row.get("device_name"))
    model = _optional_text(slot_fields.get("model") or source_row.get("model"))
    occupied = bool(source_row.get("present")) and bool(device_name or model)
    return StorageViewRuntimeSlot(
        slot_index=slot_index,
        slot_label=slot_label,
        target_system_id=PUBLIC_DEMO_SYSTEM_ID,
        target_system_label=PUBLIC_DEMO_SYSTEM_LABEL,
        occupied=occupied,
        state=_optional_text(source_row.get("state")) or ("matched" if occupied else "empty"),
        source="inventory_candidate" if occupied else "placeholder",
        match_reasons=_storage_view_match_reasons(storage_view, device_name),
        placement_key=device_name or f"layout slot {assignment_rank}",
        assignment_rank=assignment_rank,
        device_name=device_name,
        smart_device_names=list(slot_fields.get("smart_device_names") or []),
        smart_device_type=_optional_text(smart_fields.get("smart_device_type")),
        serial=ids.serial if occupied and _has_source_identifier(source_row, slot_fields, "serial") else None,
        pool_name=_optional_text(source_row.get("pool_name")),
        model=model,
        size_bytes=_optional_int(slot_fields.get("size_bytes")),
        size_human=_optional_text(slot_fields.get("size_human")),
        gptid=_persistent_id_for_label(persistent_id_label, ids) if occupied and persistent_id_label else None,
        persistent_id_label=persistent_id_label,
        health=_optional_text(source_row.get("health")),
        temperature_c=_metric_value(source, source_scope, slot_index, "temperature_c"),
        logical_block_size=_optional_int(smart_fields.get("logical_block_size")),
        physical_block_size=_optional_int(smart_fields.get("physical_block_size")),
        logical_unit_id=ids.logical_unit_id if occupied and _has_source_identifier(source_row, slot_fields, "logical_unit_id") else None,
        sas_address=ids.sas_address if occupied and _has_source_identifier(source_row, slot_fields, "sas_address") else None,
        transport_address=_optional_text(source_row.get("device_name")),
        description=_optional_text(source_row.get("topology_label")),
        slot_size=storage_view_slot_size(storage_view, slot_index),
    )


def _state_rows_for_scope(source: LiveDemoSource, source_scope: str) -> dict[int, dict[str, Any]]:
    with _connect_history_db() as connection:
        return {
            int(row["slot"]): dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM slot_state_current
                WHERE system_id = ? AND enclosure_key = ?
                ORDER BY slot
                """,
                (source.system_id, source_scope),
            )
        }


def _scrub_storage_view_binding(storage_view: StorageViewConfig) -> StorageViewConfig:
    binding = storage_view.binding.model_copy(
        update={
            "serials": [
                _ids_for_slot(_identifier_scope_for_storage_view(storage_view.id), index).serial
                for index, _ in enumerate(storage_view.binding.serials)
            ],
        }
    )
    return storage_view.model_copy(update={"binding": binding})


def _storage_view_match_reasons(storage_view: StorageViewConfig, device_name: str | None) -> list[str]:
    if device_name and device_name in storage_view.binding.device_names:
        return ["device"]
    if storage_view.binding.serials:
        return ["serial"]
    return ["live storage-view"]


def _smart_cache_for_slots(
    source: LiveDemoSource,
    *,
    scope_key: str,
    slots: list[SlotView],
    scope_name: str,
) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for slot in slots:
        if not slot.device_name and not slot.model:
            continue
        cache[str(slot.slot)] = _smart_summary_for_slot(
            source,
            scope_key=scope_key,
            display_slot=slot.slot,
            source_detail_slot=slot.slot,
            scope_name=scope_name,
            device_name=slot.device_name,
            model=slot.model,
            size_human=slot.size_human,
            serial=slot.serial,
            logical_unit_id=slot.logical_unit_id,
            sas_address=slot.sas_address,
        )
    return cache


def _storage_view_smart_summary_cache(
    source: LiveDemoSource,
    runtime: StorageViewRuntimePayload,
) -> dict[str, dict[str, dict[str, Any]]]:
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    for view in runtime.views:
        source_scope = f"storage-view:{view.id}"
        slot_cache: dict[str, dict[str, Any]] = {}
        for slot in view.slots:
            if not slot.occupied:
                continue
            slot_cache[str(slot.slot_index)] = _smart_summary_for_slot(
                source,
                scope_key=source_scope,
                display_slot=slot.slot_index,
                source_detail_slot=STORAGE_VIEW_SOURCE_SLOT_OFFSET + slot.slot_index,
                scope_name=_identifier_scope_for_storage_view(view.id),
                device_name=slot.device_name,
                model=slot.model,
                size_human=slot.size_human,
                serial=slot.serial,
                logical_unit_id=slot.logical_unit_id,
                sas_address=slot.sas_address,
            )
        cache[view.id] = slot_cache
    return cache


def _smart_summary_for_slot(
    source: LiveDemoSource,
    *,
    scope_key: str,
    display_slot: int,
    source_detail_slot: int,
    scope_name: str,
    device_name: str | None,
    model: str | None,
    size_human: str | None,
    serial: str | None,
    logical_unit_id: str | None,
    sas_address: str | None,
) -> dict[str, Any]:
    detail = _slot_detail(source, scope_key, source_detail_slot)
    smart_fields = detail.get("smart_fields") if isinstance(detail.get("smart_fields"), dict) else {}
    ids = _ids_for_slot(scope_name, display_slot)
    summary = {
        key: _scrub_value(value, replacements={})
        for key, value in smart_fields.items()
        if key not in {"serial", "serial_number", "wwn", "logical_unit_id", "sas_address", "gptid"}
    }
    summary.update(
        {
            "available": True,
            "temperature_c": _metric_value(source, scope_key, display_slot, "temperature_c"),
            "power_on_hours": _metric_value(source, scope_key, display_slot, "power_on_hours") or smart_fields.get("power_on_hours"),
            "power_on_days": (
                int((_metric_value(source, scope_key, display_slot, "power_on_hours") or smart_fields.get("power_on_hours") or 0) / 24)
                or smart_fields.get("power_on_days")
            ),
            "device_name": device_name,
            "device_model": model,
            "model_name": model,
            "size_human": size_human,
            "serial_number": serial or ids.serial,
            "wwn": logical_unit_id or ids.logical_unit_id,
            "logical_unit_id": logical_unit_id or ids.logical_unit_id,
            "sas_address": sas_address,
            "smart_health_status": smart_fields.get("smart_health_status") or "PASSED",
            "bytes_read": _metric_value(source, scope_key, display_slot, "bytes_read"),
            "bytes_written": _metric_value(source, scope_key, display_slot, "bytes_written"),
            "annualized_bytes_read": _metric_value(source, scope_key, display_slot, "annualized_bytes_read"),
            "annualized_bytes_written": _metric_value(source, scope_key, display_slot, "annualized_bytes_written"),
        }
    )
    if "rotation_rate_rpm" not in summary:
        summary["rotation_rate_rpm"] = 0 if model and ("SSD" in model.upper() or "SAMSUNG MZILT" in model.upper()) else 7200
    return {key: value for key, value in summary.items() if value is not None}


def _history_payload_for_slot(
    connection: sqlite3.Connection,
    *,
    source: LiveDemoSource,
    public_system_id: str | None,
    public_enclosure_id: str | None,
    source_scope: str,
    slot: int,
    window_hours: int | None,
) -> dict[str, Any]:
    metrics = _history_metrics_for_slot(
        connection,
        source_system_id=source.system_id,
        source_scope=source_scope,
        slot=slot,
        window_hours=window_hours,
    )
    replacements = _identifier_replacements_for_slot(connection, source=source, source_scope=source_scope, slot=slot)
    events = _history_events_for_slot(
        connection,
        source_system_id=source.system_id,
        source_scope=source_scope,
        slot=slot,
        replacements=replacements,
        window_hours=window_hours,
    )
    return {
        "configured": True,
        "available": True,
        "detail": None,
        "slot": slot,
        "system_id": public_system_id,
        "enclosure_id": public_enclosure_id,
        "metrics": metrics,
        "events": events,
        "sample_counts": {name: len(values) for name, values in metrics.items()},
        "latest_values": {name: values[-1]["value"] if values else None for name, values in metrics.items()},
        "disk_history": {},
    }


def _history_metrics_for_slot(
    connection: sqlite3.Connection,
    *,
    source_system_id: str,
    source_scope: str,
    slot: int,
    window_hours: int | None,
) -> dict[str, list[dict[str, Any]]]:
    cutoff = _history_query_cutoff(window_hours)
    params: list[Any] = [source_system_id, source_scope, slot, PUBLIC_DEMO_GENERATED_AT.isoformat()]
    where = "system_id = ? AND enclosure_key = ? AND slot = ? AND observed_at <= ?"
    if cutoff:
        where += " AND observed_at >= ?"
        params.append(cutoff)
    metrics: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        f"""
        SELECT metric_name, observed_at, value_integer, value_real
        FROM metric_samples
        WHERE {where}
        ORDER BY metric_name, observed_at
        """,
        params,
    ):
        value = _history_sample_value(dict(row))
        if value is None:
            continue
        metrics.setdefault(str(row["metric_name"]), []).append(
            {
                "observed_at": row["observed_at"],
                "value": value,
            }
        )
    return metrics


def _history_events_for_slot(
    connection: sqlite3.Connection,
    *,
    source_system_id: str,
    source_scope: str,
    slot: int,
    replacements: dict[str, str],
    window_hours: int | None,
) -> list[dict[str, Any]]:
    cutoff = _history_query_cutoff(window_hours)
    params: list[Any] = [source_system_id, source_scope, slot, PUBLIC_DEMO_GENERATED_AT.isoformat()]
    where = "system_id = ? AND enclosure_key = ? AND slot = ? AND observed_at <= ?"
    if cutoff:
        where += " AND observed_at >= ?"
        params.append(cutoff)
    events = []
    for row in connection.execute(
        f"""
        SELECT observed_at, event_type, previous_value, current_value, details_json
        FROM slot_events
        WHERE {where}
        ORDER BY observed_at
        """,
        params,
    ):
        events.append(
            {
                "observed_at": row["observed_at"],
                "event_type": row["event_type"],
                "previous_value": _scrub_sensitive_text(row["previous_value"], replacements),
                "current_value": _scrub_sensitive_text(row["current_value"], replacements),
                "details_json": _scrub_sensitive_text(row["details_json"], replacements) or "{}",
            }
        )
    return events


def _history_query_cutoff(window_hours: int | None) -> str | None:
    if not isinstance(window_hours, int) or window_hours < 1:
        return None
    return (
        PUBLIC_DEMO_GENERATED_AT
        - timedelta(hours=window_hours + LIVE_HISTORY_LOOKBACK_PADDING_HOURS)
    ).isoformat()


def _source_scope_for_public_enclosure(source: LiveDemoSource, enclosure_id: str | None) -> str:
    if enclosure_id == PUBLIC_DEMO_ENCLOSURE_ID or not enclosure_id:
        return source.enclosure_key
    if str(enclosure_id).startswith("storage-view:"):
        return str(enclosure_id)
    return source.enclosure_key


def _slot_detail(source: LiveDemoSource, scope_key: str, slot: int) -> dict[str, Any]:
    key = f"{source.system_id}:{scope_key}:{slot}"
    value = source.slot_details.get(key)
    return value if isinstance(value, dict) else {}


def _metric_value(source: LiveDemoSource, scope_key: str, slot: int, metric_name: str) -> Any:
    return source.latest_metrics.get(scope_key, {}).get(slot, {}).get(metric_name)


def _history_sample_value(row: dict[str, Any]) -> int | float | None:
    if row.get("value_integer") is not None:
        return int(row["value_integer"])
    if row.get("value_real") is not None:
        return float(row["value_real"])
    return None


def _identifier_replacements_for_slot(
    connection: sqlite3.Connection,
    *,
    source: LiveDemoSource,
    source_scope: str,
    slot: int,
) -> dict[str, str]:
    scope_name = "core" if source_scope == source.enclosure_key else _identifier_scope_for_storage_view(source_scope.removeprefix("storage-view:"))
    ids = _ids_for_slot(scope_name, slot)
    replacements: dict[str, str] = {}
    row = connection.execute(
        """
        SELECT serial, gptid, logical_unit_id, sas_address, disk_identity_key
        FROM slot_state_current
        WHERE system_id = ? AND enclosure_key = ? AND slot = ?
        """,
        (source.system_id, source_scope, slot),
    ).fetchone()
    if row:
        _add_identifier_replacement(replacements, row["serial"], ids.serial)
        _add_identifier_replacement(replacements, row["gptid"], ids.gptid)
        _add_identifier_replacement(replacements, row["logical_unit_id"], ids.logical_unit_id)
        _add_identifier_replacement(replacements, row["sas_address"], ids.sas_address)
        _add_identifier_replacement(replacements, row["disk_identity_key"], ids.logical_unit_id)

    detail_slot = STORAGE_VIEW_SOURCE_SLOT_OFFSET + slot if source_scope.startswith("storage-view:") else slot
    detail = _slot_detail(source, source_scope, detail_slot)
    slot_fields = detail.get("slot_fields") if isinstance(detail.get("slot_fields"), dict) else {}
    _add_identifier_replacement(replacements, slot_fields.get("serial"), ids.serial)
    _add_identifier_replacement(replacements, slot_fields.get("gptid"), ids.gptid)
    _add_identifier_replacement(replacements, slot_fields.get("logical_unit_id"), ids.logical_unit_id)
    _add_identifier_replacement(replacements, slot_fields.get("sas_address"), ids.sas_address)
    return replacements


def _add_identifier_replacement(replacements: dict[str, str], raw: Any, replacement: str) -> None:
    if raw is None:
        return
    raw_text = str(raw).strip()
    if raw_text:
        replacements[raw_text] = replacement


def _has_source_identifier(
    source_row: dict[str, Any],
    slot_fields: dict[str, Any],
    field_name: str,
) -> bool:
    return bool(source_row.get(field_name) or slot_fields.get(field_name))


def _persistent_id_for_label(label: str | None, ids: DemoIdentifiers) -> str:
    normalized = (label or "").lower()
    if "serial" in normalized:
        return ids.serial
    if "lun" in normalized or "naa" in normalized:
        return ids.logical_unit_id
    return ids.gptid


def _identifier_scope_for_storage_view(view_id: str) -> str:
    if "boot" in view_id:
        return "boot"
    if "nvme" in view_id or "carrier" in view_id:
        return "nvme"
    return "view"


def _ids_for_slot(scope: str, index: int) -> DemoIdentifiers:
    scope_id = {
        "core": 0x10,
        "boot": 0x20,
        "nvme": 0x30,
        "view": 0x40,
    }.get(scope, 0x50)
    serial = f"DEMO-SN-{scope.upper()}-{index:04d}"
    return DemoIdentifiers(
        serial=serial,
        gptid=f"demo-gptid-{scope}-{index:04d}",
        logical_unit_id=_naa(0xDE, scope_id, index),
        sas_address=_naa(0xDA, scope_id, index),
        attached_sas_address=_naa(0xAD, scope_id, index),
        enclosure_identifier=_naa(0xEE, scope_id, index),
    )


def _naa(prefix: int, scope_id: int, index: int) -> str:
    return f"5000{prefix:02x}{scope_id:02x}{index:08x}"


def _scrub_value(value: Any, *, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_value(item, replacements=replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, replacements=replacements) for item in value]
    if isinstance(value, str):
        return _scrub_sensitive_text(value, replacements)
    return value


def _scrub_sensitive_text(value: Any, replacements: dict[str, str]) -> str | None:
    if value is None:
        return None
    text = str(value)
    for raw, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(raw, replacement)
    text = GPTID_PATTERN.sub("demo-gptid-scrubbed", text)
    text = PARTIAL_ID_PATTERN.sub("5000deff00000000", text)
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
