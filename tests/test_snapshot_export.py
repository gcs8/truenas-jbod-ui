from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from starlette.datastructures import URLPath
from starlette.requests import Request

from app.config import Settings
from app.main import templates
from app.models.domain import (
    EnclosureOption,
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
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)


class FakeHistoryBackend:
    configured = True

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, object]:
        base_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        samples = [
            {"observed_at": (base_time - timedelta(minutes=10)).isoformat(), "value": 36},
            {"observed_at": (base_time - timedelta(minutes=5)).isoformat(), "value": 37},
        ]
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {
                "temperature_c": samples,
                "bytes_read": [],
                "bytes_written": [],
                "annualized_bytes_read": [],
                "annualized_bytes_written": [],
                "power_on_hours": [],
            },
            "events": [],
            "sample_counts": {
                "temperature_c": 2,
                "bytes_read": 0,
                "bytes_written": 0,
                "annualized_bytes_read": 0,
                "annualized_bytes_written": 0,
                "power_on_hours": 0,
            },
            "latest_values": {
                "temperature_c": 37,
                "bytes_read": None,
                "bytes_written": None,
                "annualized_bytes_read": None,
                "annualized_bytes_written": None,
                "power_on_hours": None,
            },
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, object]]:
        return {
            slot: await self.get_slot_history(slot, system_id, enclosure_id)
            for slot in slots
        }


class CountingHistoryBackend(FakeHistoryBackend):
    def __init__(self) -> None:
        self.status_calls = 0
        self.scope_history_calls = 0
        self.last_window_hours: int | None = None

    async def get_status(self) -> dict[str, object]:
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
    ) -> dict[int, dict[str, object]]:
        self.scope_history_calls += 1
        self.last_window_hours = window_hours
        return await super().get_scope_history(
            system_id=system_id,
            enclosure_id=enclosure_id,
            slots=slots,
            window_hours=window_hours,
        )


class DenseHistoryBackend:
    configured = True

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, object]:
        base_read = 1_000_000_000_000
        base_write = 250_000_000_000
        base_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        samples = []
        for index in range(288):
            observed_at = (base_time - timedelta(minutes=(287 - index) * 5)).isoformat()
            samples.append(
                {
                    "observed_at": observed_at,
                    "value": 30 + (index % 7),
                }
            )

        bytes_read_samples = [
            {
                "observed_at": sample["observed_at"],
                "value": base_read + (idx * 10_000_000),
            }
            for idx, sample in enumerate(samples)
        ]
        bytes_written_samples = [
            {
                "observed_at": sample["observed_at"],
                "value": base_write + (idx * 5_000_000),
            }
            for idx, sample in enumerate(samples)
        ]
        annualized_samples = [
            {
                "observed_at": sample["observed_at"],
                "value": 8_000_000_000_000 + (idx * 1_000_000),
            }
            for idx, sample in enumerate(samples)
        ]
        annualized_read_samples = [
            {
                "observed_at": sample["observed_at"],
                "value": 12_000_000_000_000 + (idx * 1_000_000),
            }
            for idx, sample in enumerate(samples)
        ]
        power_on_samples = [
            {
                "observed_at": sample["observed_at"],
                "value": 30_000 + (idx // 12),
            }
            for idx, sample in enumerate(samples)
        ]
        events = [
            {
                "observed_at": sample["observed_at"],
                "event_type": "Slot State Change",
                "summary": f"Change {idx}",
            }
            for idx, sample in enumerate(samples[:80])
        ]
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {
                "temperature_c": samples,
                "bytes_read": bytes_read_samples,
                "bytes_written": bytes_written_samples,
                "annualized_bytes_read": annualized_read_samples,
                "annualized_bytes_written": annualized_samples,
                "power_on_hours": power_on_samples,
            },
            "events": events,
            "sample_counts": {
                "temperature_c": len(samples),
                "bytes_read": len(bytes_read_samples),
                "bytes_written": len(bytes_written_samples),
                "annualized_bytes_read": len(annualized_read_samples),
                "annualized_bytes_written": len(annualized_samples),
                "power_on_hours": len(power_on_samples),
            },
            "latest_values": {
                "temperature_c": samples[-1]["value"],
                "bytes_read": bytes_read_samples[-1]["value"],
                "bytes_written": bytes_written_samples[-1]["value"],
                "annualized_bytes_read": annualized_read_samples[-1]["value"],
                "annualized_bytes_written": annualized_samples[-1]["value"],
                "power_on_hours": power_on_samples[-1]["value"],
            },
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, object]]:
        return {
            slot: await self.get_slot_history(slot, system_id, enclosure_id)
            for slot in slots
        }


class UnavailableHistoryBackend:
    configured = True

    async def get_status(self) -> dict[str, object]:
        return {
            "configured": True,
            "available": False,
            "detail": "History backend request failed: connection refused",
            "counts": {},
            "collector": {},
            "scopes": [],
        }

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, object]:
        return {
            "configured": True,
            "available": False,
            "detail": "History backend request failed: connection refused",
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
        }

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, object]]:
        return {
            slot: await self.get_slot_history(slot, system_id, enclosure_id)
            for slot in slots
        }


class StatusUnavailableHistoryBackend:
    configured = True

    async def get_status(self) -> dict[str, object]:
        return {
            "configured": True,
            "available": False,
            "detail": "History backend request failed: connection refused",
            "counts": {},
            "collector": {},
            "scopes": [],
        }

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, object]:
        raise AssertionError("Per-slot history fetch should be skipped when status is unavailable")

    async def get_scope_history(
        self,
        *,
        system_id: str | None,
        enclosure_id: str | None,
        slots: list[int],
        window_hours: int | None = None,
    ) -> dict[int, dict[str, object]]:
        raise AssertionError("Scope history fetch should be skipped when status is unavailable")


def build_smart_summary_cache() -> dict[str, dict[str, object]]:
    return {
        "0": {
            "available": True,
            "power_on_hours": 33105,
            "power_on_days": 1379,
            "logical_block_size": 512,
            "physical_block_size": 4096,
            "rotation_rate_rpm": 7200,
            "form_factor": "3.5 inches",
            "read_cache_enabled": True,
            "writeback_cache_enabled": True,
            "transport_protocol": "SCSI",
            "logical_unit_id": "5000c500c2a7f220",
            "sas_address": "5000c500c2a7f220",
            "attached_sas_address": "500304801f5a00bf",
            "negotiated_link_rate": "12 Gbps",
        }
    }


def build_storage_view_runtime() -> StorageViewRuntimePayload:
    return StorageViewRuntimePayload(
        system_id="archive-core",
        system_label="Archive CORE",
        views=[
            StorageViewRuntimeView(
                id="boot-doms",
                label="Boot SATADOMs",
                kind="boot_devices",
                template_id="boot-devices-2",
                template_label="Boot Devices",
                slot_layout=[[0, 1]],
                source="inventory_binding",
                backing_enclosure_id="front",
                backing_enclosure_label="Front Shelf",
                matched_count=1,
                slot_count=2,
                slots=[
                    StorageViewRuntimeSlot(
                        slot_index=0,
                        slot_label="Boot A",
                        target_system_id="archive-core",
                        target_system_label="Archive CORE",
                        occupied=True,
                        state="matched",
                        source="inventory_candidate",
                        match_reasons=["serial"],
                        placement_key="boot bay a",
                        assignment_rank=1,
                        device_name="ada0",
                        smart_device_names=["/dev/ada0"],
                        serial="SATADOM123456",
                        pool_name="freenas-boot",
                        model="SATADOM",
                        size_human="64 GB",
                        gptid="gptid/boot-a",
                        persistent_id_label="GPTID",
                        temperature_c=41,
                    ),
                    StorageViewRuntimeSlot(
                        slot_index=1,
                        slot_label="Boot B",
                        occupied=False,
                        state="empty",
                        source="placeholder",
                        assignment_rank=2,
                    ),
                ],
            )
        ],
    )


def build_storage_view_smart_summary_cache() -> dict[str, dict[str, dict[str, object]]]:
    return {
        "boot-doms": {
            "0": {
                "available": True,
                "temperature_c": 41,
                "power_on_hours": 12800,
                "logical_unit_id": "5000c500boot1234",
                "sas_address": "5000c500boot1235",
                "bytes_read": 8_000_000_000_000,
                "bytes_written": 2_000_000_000_000,
                "annualized_bytes_read": 600_000_000_000,
                "annualized_bytes_written": 150_000_000_000,
            }
        }
    }


def build_snapshot() -> InventorySnapshot:
    return InventorySnapshot(
        slots=[
            SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="front",
                enclosure_label="Front Shelf",
                present=True,
                state=SlotState.healthy,
                device_name="da0",
                serial="ABC123456",
                model="Disk Model",
                size_human="1 TB",
                pool_name="tank",
                vdev_name="raidz2-0",
                health="ONLINE",
            )
        ],
        layout_rows=[[0]],
        layout_slot_count=1,
        layout_columns=1,
        refresh_interval_seconds=30,
        selected_system_id="archive-core",
        selected_system_label="Archive CORE",
        selected_enclosure_id="front",
        selected_enclosure_label="Front Shelf",
        systems=[SystemOption(id="archive-core", label="Archive CORE", platform="core")],
        enclosures=[EnclosureOption(id="front", label="Front Shelf", rows=1, columns=1, slot_count=1, slot_layout=[[0]])],
        sources={
            "api": SourceStatus(enabled=True, ok=True, message="API healthy on Archive CORE"),
            "ssh": SourceStatus(enabled=False, ok=True, message="SSH disabled for 192.168.1.174"),
        },
        summary=InventorySummary(
            disk_count=1,
            pool_count=1,
            enclosure_count=1,
            mapped_slot_count=1,
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
        warnings=["SSH timed out for 192.168.1.174 on Archive CORE."],
    )


def build_snapshot_with_rear_option() -> InventorySnapshot:
    snapshot = build_snapshot().model_copy(deep=True)
    snapshot.enclosures = [
        EnclosureOption(id="front", label="Front Shelf", rows=1, columns=1, slot_count=1, slot_layout=[[0]]),
        EnclosureOption(id="rear", label="Rear Shelf", rows=1, columns=1, slot_count=1, slot_layout=[[0]]),
    ]
    snapshot.summary.enclosure_count = 2
    return snapshot


def build_rear_snapshot() -> InventorySnapshot:
    return InventorySnapshot(
        slots=[
            SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="rear",
                enclosure_label="Rear Shelf",
                present=True,
                state=SlotState.healthy,
                device_name="da24",
                serial="REAR123456",
                model="Rear Disk Model",
                size_human="2 TB",
                pool_name="rear-tank",
                vdev_name="mirror-1",
                health="ONLINE",
            )
        ],
        layout_rows=[[0]],
        layout_slot_count=1,
        layout_columns=1,
        refresh_interval_seconds=30,
        selected_system_id="archive-core",
        selected_system_label="Archive CORE",
        selected_enclosure_id="rear",
        selected_enclosure_label="Rear Shelf",
        systems=[SystemOption(id="archive-core", label="Archive CORE", platform="core")],
        enclosures=[
            EnclosureOption(id="front", label="Front Shelf", rows=1, columns=1, slot_count=1, slot_layout=[[0]]),
            EnclosureOption(id="rear", label="Rear Shelf", rows=1, columns=1, slot_count=1, slot_layout=[[0]]),
        ],
        sources={
            "api": SourceStatus(enabled=True, ok=True, message="API healthy on Archive CORE"),
            "ssh": SourceStatus(enabled=False, ok=True, message="SSH disabled for 192.168.1.175"),
        },
        summary=InventorySummary(
            disk_count=1,
            pool_count=1,
            enclosure_count=2,
            mapped_slot_count=1,
            manual_mapping_count=0,
            ssh_slot_hint_count=0,
        ),
        warnings=["SSH timed out for 192.168.1.175 on Archive CORE rear shelf."],
    )


def build_rear_smart_summary_cache() -> dict[str, dict[str, object]]:
    return {
        "0": {
            "available": True,
            "temperature_c": 34,
            "power_on_hours": 21000,
            "logical_unit_id": "5000c500rear1224",
            "sas_address": "5000c500rear1225",
            "bytes_read": 4_000_000_000_000,
            "bytes_written": 1_000_000_000_000,
            "annualized_bytes_read": 300_000_000_000,
            "annualized_bytes_written": 90_000_000_000,
        }
    }


def build_request() -> Request:
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
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "root_path": "",
            "app": None,
        }
    )
    request.scope["app"] = type(
        "FakeApp",
        (),
        {"url_path_for": lambda _, name, **params: URLPath(f"/static/{params['path']}")},
    )()
    return request


class SnapshotExportServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        EXPORT_HISTORY_CACHE.clear()
        EXPORT_RENDER_CACHE.clear()
        EXPORT_ZIP_CACHE.clear()

    async def test_service_builds_self_contained_html_snapshot(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        request = build_request()

        rendered = await exporter.build_enclosure_snapshot_html(
            request=request,
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )

        self.assertGreater(rendered.size_bytes, 0)
        self.assertTrue(rendered.filename.endswith(".html"))
        self.assertIn("<style>", rendered.html)
        self.assertIn("<script>", rendered.html)
        self.assertIn("Offline Snapshot", rendered.html)
        self.assertIn("snapshotMode: true", rendered.html)
        self.assertIn("preloadedHistoryBySlot", rendered.html)
        self.assertIn("preloadedSmartSummariesBySlot", rendered.html)
        self.assertIn("33105", rendered.html)
        self.assertIn("Frozen Offline Artifact", rendered.html)
        self.assertIn("App v", rendered.html)
        self.assertIn("metric samples", rendered.html)
        self.assertIn("SMART summaries", rendered.html)
        self.assertIn("events", rendered.html)
        self.assertIn("Downsampling None", rendered.html)
        self.assertIn("None", rendered.export_meta["redaction_label"])
        self.assertEqual(rendered.export_meta["redaction"], "none")
        self.assertEqual(rendered.export_meta["event_count"], 0)
        self.assertNotIn('src="/static/app.js"', rendered.html)
        self.assertNotIn('href="/static/style.css"', rendered.html)
        self.assertNotIn("/static/images/hyper-m2-gen3-card.png", rendered.html)
        self.assertIn("data:image/png;base64", rendered.html)
        self.assertNotIn("Export Snapshot", rendered.html)

    async def test_service_redacts_sensitive_values_with_stable_aliases(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            redact_sensitive=True,
        )

        self.assertIn("host-01", rendered.html)
        self.assertIn("enc-01", rendered.html)
        self.assertIn("...3456", rendered.html)
        self.assertIn("x.x.x.174", rendered.html)
        self.assertNotIn("host-02", rendered.html)
        self.assertNotIn("enc-02", rendered.html)
        self.assertNotIn("Archive CORE", rendered.html)
        self.assertNotIn("Front Shelf", rendered.html)
        self.assertNotIn("ABC123456", rendered.html)
        self.assertNotIn("192.168.1.174", rendered.html)
        self.assertNotIn("5000c500c2a7f220", rendered.html)
        self.assertEqual(rendered.export_meta["redaction"], "partial")
        self.assertEqual(rendered.export_meta["redaction_label"], "Partial")
        redacted_cache_key = exporter._build_history_cache_key(
            rendered.snapshot.selected_system_id,
            rendered.snapshot.selected_enclosure_id,
            0,
        )
        self.assertIn(redacted_cache_key, rendered.history_cache)
        self.assertTrue(rendered.history_cache[redacted_cache_key]["available"])
        self.assertEqual(rendered.history_cache[redacted_cache_key]["sample_counts"]["temperature_c"], 2)

    async def test_auto_packaging_falls_back_to_zip_when_html_exceeds_limit(self) -> None:
        snapshot = build_snapshot()
        reference_exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        request = build_request()

        html_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=request,
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="html",
            allow_oversize=True,
        )
        zip_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=request,
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="zip",
            allow_oversize=True,
        )

        self.assertGreater(html_artifact.size_bytes, zip_artifact.size_bytes)
        size_limit_bytes = zip_artifact.size_bytes + ((html_artifact.size_bytes - zip_artifact.size_bytes) // 2)
        exporter = SnapshotExportService(
            Settings(),
            FakeHistoryBackend(),
            templates,
            size_limit_bytes=size_limit_bytes,
        )

        auto_artifact = await exporter.build_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )

        self.assertEqual(auto_artifact.packaging, "zip")
        self.assertTrue(auto_artifact.filename.endswith(".zip"))

    async def test_estimate_allows_snapshot_to_keep_smart_details_and_oversize_override(self) -> None:
        snapshot = build_snapshot()
        smart_summary_cache = build_smart_summary_cache()
        reference_exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        html_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="html",
            allow_oversize=True,
        )
        zip_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="zip",
            allow_oversize=True,
        )
        size_limit_bytes = max(1, min(html_artifact.size_bytes, zip_artifact.size_bytes) // 2)
        exporter = SnapshotExportService(
            Settings(),
            FakeHistoryBackend(),
            templates,
            size_limit_bytes=size_limit_bytes,
        )

        without_override = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
            allow_oversize=False,
        )
        with_override = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
            allow_oversize=True,
        )

        self.assertGreater(html_artifact.size_bytes, 0)
        self.assertEqual(without_override["auto_packaging"], "oversize")
        self.assertIsNone(without_override["effective_packaging"])
        self.assertFalse(without_override["selected_allowed"])
        self.assertEqual(with_override["effective_packaging"], "zip")
        self.assertEqual(with_override["selected_size_bytes"], with_override["zip_size_bytes"])
        self.assertFalse(with_override["selected_within_limit"])
        self.assertTrue(with_override["selected_allowed"])

    async def test_service_downsamples_dense_history_when_target_is_tight(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(
            Settings(),
            DenseHistoryBackend(),
            templates,
            size_limit_bytes=1024,
        )

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )

        self.assertNotEqual(rendered.export_meta["downsampling_label"], "None")
        self.assertIn("rollups", rendered.export_meta["downsampling_note"])
        self.assertLess(rendered.export_meta["metric_sample_count"], 288 * 5)
        self.assertLessEqual(rendered.export_meta["event_count"], 10)

    async def test_history_drawer_only_opens_when_exported_open(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        closed_render = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=False,
            io_chart_mode="total",
        )
        open_render = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn("initialHistoryPanelOpen: false", closed_render.html)
        self.assertIn("initialHistoryPanelOpen: true", open_render.html)

    async def test_estimate_and_export_reuse_cached_render_and_zip_artifacts(self) -> None:
        snapshot = build_snapshot()
        history_backend = CountingHistoryBackend()
        exporter = SnapshotExportService(Settings(), history_backend, templates)
        zip_build_calls = 0
        original_build_zip_archive = exporter._build_zip_archive

        def counting_build_zip_archive(html_filename: str, html_content: bytes) -> bytes:
            nonlocal zip_build_calls
            zip_build_calls += 1
            return original_build_zip_archive(html_filename, html_content)

        exporter._build_zip_archive = counting_build_zip_archive  # type: ignore[method-assign]

        estimate = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )
        artifact = await exporter.build_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )

        self.assertTrue(estimate["ok"])
        self.assertGreater(artifact.size_bytes, 0)
        self.assertEqual(history_backend.scope_history_calls, 1)
        self.assertEqual(history_backend.last_window_hours, 24)
        self.assertEqual(zip_build_calls, 1)

    async def test_render_option_changes_reuse_cached_scope_history(self) -> None:
        snapshot = build_snapshot()
        history_backend = CountingHistoryBackend()
        exporter = SnapshotExportService(Settings(), history_backend, templates)

        narrow_window = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )
        average_chart = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="average",
            packaging="auto",
        )

        self.assertTrue(narrow_window["ok"])
        self.assertTrue(average_chart["ok"])
        self.assertEqual(history_backend.scope_history_calls, 1)

    async def test_snapshot_export_omits_history_when_backend_is_unavailable(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), UnavailableHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertFalse(rendered.history_available)
        self.assertEqual(rendered.export_meta["tracked_slots"], 0)
        self.assertEqual(rendered.export_meta["metric_sample_count"], 0)
        self.assertEqual(rendered.export_meta["event_count"], 0)
        self.assertIn("initialHistoryPanelOpen: false", rendered.html)

    async def test_snapshot_export_short_circuits_when_status_reports_history_unavailable(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), StatusUnavailableHistoryBackend(), templates)

        estimate = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
            packaging="auto",
        )

        self.assertTrue(estimate["ok"])
        self.assertEqual(estimate["metric_sample_count"], 0)
        self.assertEqual(estimate["event_count"], 0)

    async def test_service_embeds_storage_view_runtime_smart_and_history(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn("Boot SATADOMs", rendered.html)
        self.assertIn("preloadedStorageViewSmartSummaries", rendered.html)
        self.assertIn("SATADOM123456", rendered.html)
        self.assertIn("5000c500boot1234", rendered.html)
        self.assertEqual(rendered.export_meta["storage_view_count"], 1)
        self.assertEqual(rendered.export_meta["smart_summary_count"], 2)
        self.assertGreaterEqual(rendered.export_meta["metric_sample_count"], 4)
        self.assertIn("archive-core|storage-view:boot-doms|0", rendered.history_cache)
        self.assertTrue(rendered.history_cache["archive-core|storage-view:boot-doms|0"]["available"])

    async def test_service_embeds_live_enclosure_snapshots_smart_and_history(self) -> None:
        snapshot = build_snapshot_with_rear_option()
        rear_snapshot = build_rear_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            live_enclosure_snapshots={
                "front": snapshot,
                "rear": rear_snapshot,
            },
            live_enclosure_smart_summary_cache={
                "front": build_smart_summary_cache(),
                "rear": build_rear_smart_summary_cache(),
            },
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn("preloadedSnapshotsByEnclosure", rendered.html)
        self.assertIn("preloadedSnapshotSmartSummaries", rendered.html)
        self.assertIn("Rear Shelf", rendered.html)
        self.assertIn("REAR123456", rendered.html)
        self.assertEqual(rendered.export_meta["scope_kind"], "system")
        self.assertEqual(rendered.export_meta["enclosure_count"], 2)
        self.assertEqual(rendered.export_meta["storage_view_count"], 1)
        self.assertEqual(rendered.export_meta["visible_bay_count"], 2)
        self.assertEqual(rendered.export_meta["smart_summary_count"], 3)
        self.assertGreaterEqual(rendered.export_meta["metric_sample_count"], 6)
        self.assertIn("archive-core|front|0", rendered.history_cache)
        self.assertIn("archive-core|rear|0", rendered.history_cache)
        self.assertIn("archive-core|storage-view:boot-doms|0", rendered.history_cache)

    async def test_storage_view_export_redaction_covers_view_payloads(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
            redact_sensitive=True,
        )

        self.assertEqual(rendered.export_meta["redaction"], "partial")
        self.assertIn("Boot SATADOMs", rendered.html)
        self.assertIn("...3456", rendered.html)
        self.assertNotIn("SATADOM123456", rendered.html)
        self.assertNotIn("5000c500boot1234", rendered.html)
        self.assertNotIn("Archive CORE", rendered.html)
        self.assertIn("host-01", rendered.html)
        self.assertEqual(rendered.export_meta["storage_view_count"], 1)

    async def test_live_enclosure_export_redaction_covers_extra_snapshots(self) -> None:
        snapshot = build_snapshot_with_rear_option()
        rear_snapshot = build_rear_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            live_enclosure_snapshots={
                "front": snapshot,
                "rear": rear_snapshot,
            },
            live_enclosure_smart_summary_cache={
                "front": build_smart_summary_cache(),
                "rear": build_rear_smart_summary_cache(),
            },
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
            redact_sensitive=True,
        )

        self.assertEqual(rendered.export_meta["redaction"], "partial")
        self.assertEqual(rendered.export_meta["enclosure_count"], 2)
        self.assertIn("enc-02", rendered.html)
        self.assertNotIn("Rear Shelf", rendered.html)
        self.assertNotIn("REAR123456", rendered.html)
        self.assertNotIn("5000c500rear1224", rendered.html)
        self.assertIn("host-01|enc-02|0", rendered.history_cache)

    async def test_dense_storage_view_history_is_downsampled_with_live_history(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(
            Settings(),
            DenseHistoryBackend(),
            templates,
            size_limit_bytes=1024,
        )

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="average",
        )

        self.assertEqual(rendered.export_meta["storage_view_count"], 1)
        self.assertNotEqual(rendered.export_meta["downsampling_label"], "None")
        self.assertIn("rollups", rendered.export_meta["downsampling_note"])
        self.assertLess(rendered.export_meta["metric_sample_count"], 288 * 5 * 2)
        self.assertLess(rendered.export_meta["event_count"], 80 * 2)
        self.assertIn("archive-core|storage-view:boot-doms|0", rendered.history_cache)

    async def test_dense_live_enclosure_history_is_downsampled_with_storage_views(self) -> None:
        snapshot = build_snapshot_with_rear_option()
        rear_snapshot = build_rear_snapshot()
        exporter = SnapshotExportService(
            Settings(),
            DenseHistoryBackend(),
            templates,
            size_limit_bytes=1024,
        )

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            live_enclosure_snapshots={
                "front": snapshot,
                "rear": rear_snapshot,
            },
            live_enclosure_smart_summary_cache={
                "front": build_smart_summary_cache(),
                "rear": build_rear_smart_summary_cache(),
            },
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="average",
        )

        self.assertEqual(rendered.export_meta["enclosure_count"], 2)
        self.assertEqual(rendered.export_meta["storage_view_count"], 1)
        self.assertNotEqual(rendered.export_meta["downsampling_label"], "None")
        self.assertIn("rollups", rendered.export_meta["downsampling_note"])
        self.assertLess(rendered.export_meta["metric_sample_count"], 288 * 5 * 3)
        self.assertLess(rendered.export_meta["event_count"], 80 * 3)
        self.assertIn("archive-core|rear|0", rendered.history_cache)
        self.assertIn("archive-core|storage-view:boot-doms|0", rendered.history_cache)


if __name__ == "__main__":
    unittest.main()
