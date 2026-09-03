from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.config import Settings
from app.main import templates
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    InventorySummary,
    MultipathMember,
    MultipathView,
    SlotState,
    SlotView,
    SourceStatus,
    SystemOption,
)
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)
from history_service.domain import MetricSample, SlotEvent, SlotStateRecord
from history_service.store import HistoryStore, SlotStateUpdate
from starlette.datastructures import URLPath
from starlette.requests import Request


MODELED_SLOT_COUNTS = (60, 347)
FIXTURE_VERSION = 1
FIXTURE_GENERATED_AT = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
HISTORY_METRIC_NAMES = (
    "temperature_c",
    "bytes_read",
    "bytes_written",
    "annualized_bytes_read",
    "annualized_bytes_written",
    "power_on_hours",
)
MODELED_THRESHOLDS = {
    60: {
        "inventory_response_bytes": 98_304,
        "scope_history_response_bytes": 655_360,
        "scope_history_select_statements": 20,
        "export_html_bytes": 8_388_608,
        "logical_retained_bytes": 20_971_520,
    },
    347: {
        "inventory_response_bytes": 524_288,
        "scope_history_response_bytes": 3_145_728,
        "scope_history_select_statements": 20,
        "export_html_bytes": 12_582_912,
        "logical_retained_bytes": 33_554_432,
    },
}


def _validate_slot_count(slot_count: int) -> None:
    if slot_count not in MODELED_SLOT_COUNTS:
        raise ValueError(f"Unsupported modeled slot count: {slot_count}")


def _fixture_ids(slot_count: int) -> tuple[str, str, str, str]:
    suffix = f"{slot_count:03d}"
    return (
        f"model-{suffix}",
        f"Model {slot_count}",
        f"enc-{suffix}",
        f"Model enclosure {slot_count}",
    )


def _slot_identifiers(slot_count: int, slot: int) -> dict[str, str]:
    system_id, _, enclosure_id, _ = _fixture_ids(slot_count)
    return {
        "system_id": system_id,
        "enclosure_id": enclosure_id,
        "device_name": f"disk{slot:03d}",
        "serial": f"M{slot_count:03d}{slot:05d}",
        "gptid": f"gptid/m{slot_count:03d}-{slot:05d}",
        "logical_unit_id": f"5{slot_count:03x}{slot:012x}",
        "sas_address": f"5{slot_count:03x}{slot + 1:012x}",
    }


def build_modeled_inventory_snapshot(slot_count: int) -> InventorySnapshot:
    _validate_slot_count(slot_count)
    system_id, system_label, enclosure_id, enclosure_label = _fixture_ids(slot_count)
    columns = 12 if slot_count == 60 else 17
    layout_rows: list[list[int | None]] = [
        list(range(row_start, min(slot_count, row_start + columns)))
        for row_start in range(0, slot_count, columns)
    ]
    slots: list[SlotView] = []
    present_count = 0
    for slot in range(slot_count):
        identifiers = _slot_identifiers(slot_count, slot)
        present = slot % 11 != 10
        detailed_sample = slot % 16 == 0
        present_count += int(present)
        state = (
            SlotState.empty
            if not present
            else SlotState.fault
            if slot % 29 == 0
            else SlotState.identify
            if slot % 31 == 0
            else SlotState.healthy
        )
        health = None if not present else "FAULTED" if state == SlotState.fault else "ONLINE"
        multipath = None
        if present and detailed_sample:
            multipath = MultipathView(
                name=f"modeled-mpath-{slot:03d}",
                device_name=f"multipath/disk{slot:03d}",
                uuid=f"modeled-mpath-uuid-{slot_count:03d}-{slot:05d}",
                mode="Active/Active",
                state="OPTIMAL" if state != SlotState.fault else "DEGRADED",
                provider_state="READY",
                path_device_name=f"path{slot:03d}a",
                alternate_path_device=f"path{slot:03d}b",
                lunid=identifiers["logical_unit_id"],
                bus=f"bus-{slot % 8}",
                members=[
                    MultipathMember(
                        device_name=f"path{slot:03d}a",
                        state="ACTIVE",
                        mode="Active",
                        controller_label="Controller A",
                    ),
                    MultipathMember(
                        device_name=f"path{slot:03d}b",
                        state="ACTIVE",
                        mode="Active",
                        controller_label="Controller B",
                    ),
                ],
            )
        slots.append(
            SlotView(
                slot=slot,
                slot_label=f"Bay {slot + 1:03d}",
                row_index=slot // columns,
                column_index=slot % columns,
                enclosure_id=enclosure_id,
                enclosure_label=enclosure_label,
                enclosure_name=f"MODELED-ENC-{slot_count:03d}",
                present=present,
                state=state,
                identify_active=state == SlotState.identify,
                device_name=identifiers["device_name"] if present else None,
                smart_device_names=[identifiers["device_name"]] if present else [],
                smart_device_type="scsi" if present else None,
                serial=identifiers["serial"] if present else None,
                model="Modeled SAS HDD" if present else None,
                size_bytes=12_000_000_000_000 + (slot * 1_000_000) if present else None,
                size_human="12 TB" if present else None,
                gptid=identifiers["gptid"] if present else None,
                persistent_id_label="GPTID" if present else None,
                pool_name=f"modeled-pool-{slot % 4}" if present else None,
                vdev_name=f"raidz2-{slot // 12}" if present else None,
                vdev_class="data" if present else None,
                topology_label=f"data / raidz2-{slot // 12}" if present else None,
                health=health,
                multipath=multipath,
                temperature_c=31 + (slot % 8) if present else None,
                last_smart_test_type="SHORT" if present else None,
                last_smart_test_status="SUCCESS" if present else None,
                last_smart_test_lifetime_hours=20_000 + slot if present else None,
                logical_block_size=4096 if present else None,
                physical_block_size=4096 if present else None,
                logical_unit_id=identifiers["logical_unit_id"] if present else None,
                sas_address=identifiers["sas_address"] if present else None,
                attached_sas_address=f"5{slot_count:03x}{slot + 2:012x}" if present else None,
                transport_protocol="SAS" if present else None,
                sg_device=f"/dev/sg{slot}" if present else None,
                scsi_hctl=f"0:0:{slot // 256}:{slot % 256}" if present else None,
                phy_identifier=str(slot % 8) if present else None,
                target_port_protocol="SAS" if present else None,
                enclosure_identifier=f"modeled-eid-{slot_count:03d}",
                led_supported=True,
                led_backend="modeled-ses",
                led_reason="Modeled fixture",
                ssh_ses_device=f"/dev/sg{slot_count}" if present else None,
                ssh_ses_element_id=slot if present else None,
                mapping_source="modeled",
                search_text=(
                    f"Bay {slot + 1:03d} {identifiers['serial']}"
                    if present
                    else f"Bay {slot + 1:03d} empty"
                ),
                operator_context=(
                    {"fixture": "modeled", "slot_group": slot // columns}
                    if detailed_sample
                    else {}
                ),
                raw_status=(
                    {"modeled": True, "status_code": int(state == SlotState.fault)}
                    if detailed_sample
                    else {}
                ),
            )
        )

    return InventorySnapshot(
        slots=slots,
        layout_rows=layout_rows,
        layout_slot_count=slot_count,
        layout_columns=columns,
        last_updated=FIXTURE_GENERATED_AT,
        generated_at=FIXTURE_GENERATED_AT,
        refresh_interval_seconds=30,
        selected_system_id=system_id,
        selected_system_label=system_label,
        selected_system_platform="modeled",
        selected_enclosure_id=enclosure_id,
        selected_enclosure_label=enclosure_label,
        selected_enclosure_name=f"MODELED-ENC-{slot_count:03d}",
        systems=[SystemOption(id=system_id, label=system_label, platform="modeled")],
        enclosures=[
            EnclosureOption(
                id=enclosure_id,
                label=enclosure_label,
                name=f"MODELED-ENC-{slot_count:03d}",
                rows=len(layout_rows),
                columns=columns,
                slot_count=slot_count,
                slot_layout=layout_rows,
            )
        ],
        platform_context={"fixture_version": FIXTURE_VERSION, "modeled": True},
        sources={
            "modeled": SourceStatus(
                enabled=True,
                ok=True,
                message="Generated deterministic fixture data",
            )
        },
        summary=InventorySummary(
            disk_count=present_count,
            pool_count=4,
            enclosure_count=1,
            mapped_slot_count=present_count,
            manual_mapping_count=0,
            ssh_slot_hint_count=present_count,
        ),
    )


def _metric_value(metric_name: str, slot: int, sample_index: int) -> int | float:
    if metric_name == "temperature_c":
        return 31 + (slot % 8) + sample_index
    if metric_name == "bytes_read":
        return 1_000_000_000_000 + (slot * 10_000_000) + (sample_index * 1_000_000)
    if metric_name == "bytes_written":
        return 250_000_000_000 + (slot * 5_000_000) + (sample_index * 500_000)
    if metric_name == "annualized_bytes_read":
        return 12_000_000_000_000 + (slot * 1_000_000) + (sample_index * 100_000)
    if metric_name == "annualized_bytes_written":
        return 8_000_000_000_000 + (slot * 1_000_000) + (sample_index * 100_000)
    return 20_000 + slot + sample_index


def build_modeled_scope_history(slot_count: int) -> dict[int, dict[str, Any]]:
    _validate_slot_count(slot_count)
    system_id, system_label, enclosure_id, enclosure_label = _fixture_ids(slot_count)
    histories: dict[int, dict[str, Any]] = {}
    for slot in range(slot_count):
        identifiers = _slot_identifiers(slot_count, slot)
        metrics: dict[str, list[dict[str, Any]]] = {}
        for metric_name in HISTORY_METRIC_NAMES:
            samples: list[dict[str, Any]] = []
            for sample_index in range(2):
                value = _metric_value(metric_name, slot, sample_index)
                observed_at = (FIXTURE_GENERATED_AT - timedelta(minutes=5 * (1 - sample_index))).isoformat()
                samples.append(
                    {
                        "id": (slot * 12) + (HISTORY_METRIC_NAMES.index(metric_name) * 2) + sample_index + 1,
                        "observed_at": observed_at,
                        "system_id": system_id,
                        "system_label": system_label,
                        "enclosure_key": enclosure_id,
                        "enclosure_id": enclosure_id,
                        "enclosure_label": enclosure_label,
                        "slot": slot,
                        "slot_label": f"Bay {slot + 1:03d}",
                        "metric_name": metric_name,
                        "value_integer": value if isinstance(value, int) else None,
                        "value_real": value if isinstance(value, float) else None,
                        "device_name": identifiers["device_name"],
                        "serial": identifiers["serial"],
                        "model": "Modeled SAS HDD",
                        "state": "healthy",
                        "gptid": identifiers["gptid"],
                        "persistent_id_label": "GPTID",
                        "disk_identity_key": (
                            f"{identifiers['serial'].casefold()}|gptid|{identifiers['gptid'].casefold()}"
                        ),
                        "logical_unit_id": identifiers["logical_unit_id"],
                        "sas_address": identifiers["sas_address"],
                        "value": value,
                    }
                )
            metrics[metric_name] = samples
        histories[slot] = {
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "events": [
                {
                    "id": slot + 1,
                    "observed_at": (FIXTURE_GENERATED_AT - timedelta(minutes=10)).isoformat(),
                    "system_id": system_id,
                    "system_label": system_label,
                    "enclosure_key": enclosure_id,
                    "enclosure_id": enclosure_id,
                    "enclosure_label": enclosure_label,
                    "slot": slot,
                    "slot_label": f"Bay {slot + 1:03d}",
                    "event_type": "slot_state_changed",
                    "previous_value": "empty / steady",
                    "current_value": "healthy / steady",
                    "device_name": identifiers["device_name"],
                    "serial": identifiers["serial"],
                    "details_json": "{\"modeled\":true}",
                    "gptid": identifiers["gptid"],
                    "persistent_id_label": "GPTID",
                    "disk_identity_key": (
                        f"{identifiers['serial'].casefold()}|gptid|{identifiers['gptid'].casefold()}"
                    ),
                    "logical_unit_id": identifiers["logical_unit_id"],
                    "sas_address": identifiers["sas_address"],
                }
            ],
            "metrics": metrics,
            "sample_counts": {metric_name: 2 for metric_name in HISTORY_METRIC_NAMES},
            "latest_values": {
                metric_name: metrics[metric_name][-1]["value"]
                for metric_name in HISTORY_METRIC_NAMES
            },
        }
    return histories


def populate_modeled_history_store(store: HistoryStore, slot_count: int) -> None:
    snapshot = build_modeled_inventory_snapshot(slot_count)
    snapshot_payload = snapshot.model_dump(mode="json")
    histories = build_modeled_scope_history(slot_count)
    observed_at = FIXTURE_GENERATED_AT.isoformat()
    updates: list[SlotStateUpdate] = []
    samples: list[MetricSample] = []

    for slot_view in snapshot.slots:
        slot = slot_view.slot
        history = histories[slot]
        record = SlotStateRecord.from_snapshot_slot(
            snapshot_payload,
            slot_view.model_dump(mode="json"),
        )
        events = [
            SlotEvent(**{key: value for key, value in event.items() if key != "id"})
            for event in history["events"]
        ]
        updates.append(SlotStateUpdate(record=record, observed_at=observed_at, events=events))
        for metric_name in HISTORY_METRIC_NAMES:
            for sample in history["metrics"][metric_name]:
                samples.append(
                    MetricSample(
                        **{
                            key: value
                            for key, value in sample.items()
                            if key not in {"id", "value"}
                        }
                    )
                )

    store.record_slot_updates(updates)
    store.insert_metric_samples(samples)


class ModeledHistoryBackend:
    configured = True

    def __init__(self, slot_count: int) -> None:
        _validate_slot_count(slot_count)
        self.histories = build_modeled_scope_history(slot_count)
        self.status_calls = 0
        self.scope_history_calls = 0
        self.slot_history_calls = 0

    async def get_status(self) -> dict[str, Any]:
        self.status_calls += 1
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "counts": {},
            "collector": {},
            "scopes": [],
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, Any]]:
        self.scope_history_calls += 1
        return {
            slot: {
                "configured": True,
                "available": True,
                "detail": None,
                **self.histories[slot],
                "system_id": system_id,
                "enclosure_id": enclosure_id,
            }
            for slot in slots
        }

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
        window_hours: int | None = None,
    ) -> dict[str, Any]:
        self.slot_history_calls += 1
        raise AssertionError("Modeled export must use one batched scope-history call")


def build_modeled_request() -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("example.invalid", 1234),
            "server": ("example.invalid", 80),
            "root_path": "",
            "app": None,
        }
    )
    request.scope["app"] = type(
        "ModeledApp",
        (),
        {"url_path_for": lambda _, name, **params: URLPath(f"/static/{params['path']}")},
    )()
    return request


def _compact_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _route_scope_payload(
    histories: dict[int, dict[str, Any]],
    *,
    system_id: str,
    enclosure_id: str | None,
) -> dict[str, object]:
    return {
        "histories": {
            str(slot): {
                "slot": slot,
                "system_id": system_id,
                "enclosure_id": enclosure_id,
                "events": payload.get("events", []),
                "metrics": payload.get("metrics", {}),
                "sample_counts": payload.get("sample_counts", {}),
                "latest_values": payload.get("latest_values", {}),
            }
            for slot, payload in histories.items()
        }
    }


def measure_modeled_perf_case(slot_count: int) -> dict[str, Any]:
    _validate_slot_count(slot_count)
    snapshot = build_modeled_inventory_snapshot(slot_count)
    system_id = snapshot.selected_system_id or ""
    enclosure_id = snapshot.selected_enclosure_id
    inventory_response_bytes = len(_compact_json_bytes(snapshot.model_dump(mode="json")))

    with tempfile.TemporaryDirectory(prefix="modeled-perf-history-") as temp_dir:
        store = HistoryStore(str(Path(temp_dir) / "history.db"))
        populate_modeled_history_store(store, slot_count)
        connection_count = 0
        select_statements: list[str] = []
        original_connect = store._connect

        def traced_connect():
            nonlocal connection_count
            connection_count += 1
            connection = original_connect()
            connection.set_trace_callback(
                lambda statement: select_statements.append(statement)
                if statement.lstrip().upper().startswith("SELECT")
                else None
            )
            return connection

        with patch.object(store, "_connect", side_effect=traced_connect):
            scope_history = store.list_scope_history(
                system_id,
                enclosure_id,
                slots=list(range(slot_count)),
                event_limit=12,
                metric_limits={metric_name: 2 for metric_name in HISTORY_METRIC_NAMES},
            )
        scope_history_response_bytes = len(
            _compact_json_bytes(
                _route_scope_payload(
                    scope_history,
                    system_id=system_id,
                    enclosure_id=enclosure_id,
                )
            )
        )

    EXPORT_HISTORY_CACHE.clear()
    EXPORT_RENDER_CACHE.clear()
    EXPORT_ZIP_CACHE.clear()
    history_backend = ModeledHistoryBackend(slot_count)
    exporter = SnapshotExportService(Settings(), history_backend, templates)
    render_calls = 0
    zip_build_calls = 0
    export_html_document_bytes = 0
    inlined_static_asset_bytes = 0
    original_render = exporter._render_template_with_assets
    original_zip_builder = exporter._build_zip_archive
    original_inline_assets = exporter._inline_static_assets

    def counting_render(*args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return original_render(*args, **kwargs)

    def counting_zip_builder(*args, **kwargs):
        nonlocal zip_build_calls
        zip_build_calls += 1
        return original_zip_builder(*args, **kwargs)

    def measuring_inline_assets(request, html):
        nonlocal export_html_document_bytes, inlined_static_asset_bytes
        export_html_document_bytes = len(html.encode("utf-8"))
        rendered = original_inline_assets(request, html)
        inlined_static_asset_bytes = len(rendered.encode("utf-8")) - export_html_document_bytes
        return rendered

    exporter._render_template_with_assets = counting_render  # type: ignore[method-assign]
    exporter._build_zip_archive = counting_zip_builder  # type: ignore[method-assign]
    exporter._inline_static_assets = measuring_inline_assets  # type: ignore[method-assign]
    arguments = {
        "request": build_modeled_request(),
        "snapshot": snapshot,
        "smart_summary_cache": {},
        "selected_slot": 0,
        "history_window_hours": 24,
        "history_panel_open": False,
        "io_chart_mode": "total",
        "generated_at": FIXTURE_GENERATED_AT,
    }
    first = asyncio.run(exporter.build_enclosure_snapshot_html(**arguments))
    second = asyncio.run(exporter.build_enclosure_snapshot_html(**arguments))
    if first is not second:
        raise AssertionError("Modeled warm export did not reuse the render cache")
    html_bytes = first.html.encode("utf-8")
    history_cache_bytes = sum(entry.size_bytes for entry in EXPORT_HISTORY_CACHE.values())
    render_cache_bytes = sum(entry.size_bytes for entry in EXPORT_RENDER_CACHE.values())
    zip_cache_bytes = sum(entry.size_bytes for entry in EXPORT_ZIP_CACHE.values())
    export_cache_total_bytes = history_cache_bytes + render_cache_bytes + zip_cache_bytes

    return {
        "slot_count": slot_count,
        "inventory_response_bytes": inventory_response_bytes,
        "scope_history_response_bytes": scope_history_response_bytes,
        "scope_history_connections": connection_count,
        "scope_history_select_statements": len(select_statements),
        "history_status_calls": history_backend.status_calls,
        "history_scope_calls": history_backend.scope_history_calls,
        "history_slot_calls": history_backend.slot_history_calls,
        "render_calls": render_calls,
        "zip_build_calls": zip_build_calls,
        "history_cache_entries": len(EXPORT_HISTORY_CACHE),
        "history_cache_bytes": history_cache_bytes,
        "render_cache_entries": len(EXPORT_RENDER_CACHE),
        "render_cache_bytes": render_cache_bytes,
        "zip_cache_entries": len(EXPORT_ZIP_CACHE),
        "zip_cache_bytes": zip_cache_bytes,
        "export_cache_total_bytes": export_cache_total_bytes,
        "export_cache_max_bytes": exporter.settings.app.export_cache_max_bytes,
        "export_html_bytes": len(html_bytes),
        "export_html_document_bytes": export_html_document_bytes,
        "inlined_static_asset_bytes": inlined_static_asset_bytes,
        "logical_retained_bytes": export_cache_total_bytes,
        "thresholds": dict(MODELED_THRESHOLDS[slot_count]),
    }
