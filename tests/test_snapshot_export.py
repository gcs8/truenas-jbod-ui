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
        self.assertNotIn('src="/static/app.js"', rendered.html)
        self.assertNotIn('href="/static/style.css"', rendered.html)
        self.assertNotIn("Export Snapshot", rendered.html)

    async def test_service_redacts_sensitive_values_with_stable_aliases(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
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

    async def test_auto_packaging_falls_back_to_zip_when_html_exceeds_limit(self) -> None:
        snapshot = build_snapshot()
        reference_exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        request = build_request()

        html_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=request,
            snapshot=snapshot,
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="html",
            allow_oversize=True,
        )
        zip_artifact = await reference_exporter.build_enclosure_snapshot_export(
            request=request,
            snapshot=snapshot,
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
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )

        self.assertEqual(auto_artifact.packaging, "zip")
        self.assertTrue(auto_artifact.filename.endswith(".zip"))


if __name__ == "__main__":
    unittest.main()
