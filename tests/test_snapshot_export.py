from __future__ import annotations

import unittest

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
    SystemOption,
)
from app.services.snapshot_export import SnapshotExportService


class FakeHistoryBackend:
    configured = True

    async def get_slot_history(
        self,
        slot: int,
        system_id: str | None,
        enclosure_id: str | None,
    ) -> dict[str, object]:
        return {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": slot,
            "system_id": system_id,
            "enclosure_id": enclosure_id,
            "metrics": {
                "temperature_c": [
                    {"observed_at": "2026-04-17T00:30:00+00:00", "value": 36},
                    {"observed_at": "2026-04-17T00:35:00+00:00", "value": 37},
                ],
                "bytes_read": [],
                "bytes_written": [],
                "annualized_bytes_written": [],
                "power_on_hours": [],
            },
            "events": [],
            "sample_counts": {
                "temperature_c": 2,
                "bytes_read": 0,
                "bytes_written": 0,
                "annualized_bytes_written": 0,
                "power_on_hours": 0,
            },
            "latest_values": {
                "temperature_c": 37,
                "bytes_read": None,
                "bytes_written": None,
                "annualized_bytes_written": None,
                "power_on_hours": None,
            },
        }


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
        samples = []
        for index in range(288):
            hour = index // 12
            minute = (index % 12) * 5
            observed_at = f"2026-04-16T{hour:02d}:{minute:02d}:00+00:00"
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
                "annualized_bytes_written": annualized_samples,
                "power_on_hours": power_on_samples,
            },
            "events": events,
            "sample_counts": {
                "temperature_c": len(samples),
                "bytes_read": len(bytes_read_samples),
                "bytes_written": len(bytes_written_samples),
                "annualized_bytes_written": len(annualized_samples),
                "power_on_hours": len(power_on_samples),
            },
            "latest_values": {
                "temperature_c": samples[-1]["value"],
                "bytes_read": bytes_read_samples[-1]["value"],
                "bytes_written": bytes_written_samples[-1]["value"],
                "annualized_bytes_written": annualized_samples[-1]["value"],
                "power_on_hours": power_on_samples[-1]["value"],
            },
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


if __name__ == "__main__":
    unittest.main()
