from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, SystemConfig, TrueNASConfig
from app.metrics import (
    ScheduledBackupStatusCollector,
    install_metrics,
    observe_history_collection_run,
    observe_history_retention_run,
    set_history_collection_schedule_overrun,
    set_history_collector_running,
)
from app.models.domain import InventorySnapshot, SlotView, SmartSummaryView
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
    SnapshotExportService,
)
from app.services.truenas_ws import TrueNASRawData


async def invoke_asgi(app: FastAPI, path: str) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    return messages


def response_body(messages: list[dict[str, object]]) -> str:
    body_chunks = [
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    ]
    return b"".join(body_chunks).decode("utf-8")


def build_inventory_service(
    *,
    temp_dir: str,
    system_id: str = "metrics-system",
    platform: str = "core",
    truenas_client=None,
) -> InventoryService:
    settings = Settings()
    system = SystemConfig(id=system_id, truenas=TrueNASConfig(platform=platform))
    return InventoryService(
        settings,
        system,
        truenas_client or AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(f"{temp_dir}\\slot_mappings.json"),
        ProfileRegistry(settings),
        SlotDetailStore(f"{temp_dir}\\slot_detail_cache.json"),
    )


class MetricsRouteTests(unittest.TestCase):
    def test_install_metrics_mounts_metrics_and_records_http_samples(self) -> None:
        app = FastAPI()
        service_name = "test-metrics-route"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=service_name, version="0.0.0-test")

        @app.get("/ping")
        async def ping() -> JSONResponse:
            return JSONResponse({"ok": True})

        asyncio.run(invoke_asgi(app, "/ping"))
        metrics_messages = asyncio.run(invoke_asgi(app, "/metrics"))
        metrics_text = response_body(metrics_messages)

        self.assertIn('truenas_jbod_ui_http_requests_total', metrics_text)
        self.assertIn(f'service="{service_name}"', metrics_text)
        self.assertIn('route="/ping"', metrics_text)
        self.assertIn('truenas_jbod_ui_build_info', metrics_text)

    def test_install_metrics_can_be_disabled(self) -> None:
        app = FastAPI()

        with patch.dict("os.environ", {"METRICS_ENABLED": "false"}, clear=False):
            install_metrics(app, service_name="test-metrics-disabled", version="0.0.0-test")

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/metrics", paths)


class SnapshotExportMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        EXPORT_HISTORY_CACHE.clear()
        EXPORT_RENDER_CACHE.clear()
        EXPORT_ZIP_CACHE.clear()

    def test_snapshot_export_metrics_publish_bounded_cache_outcomes_and_sizes(self) -> None:
        app = FastAPI()
        service_name = "test-snapshot-export-cache"
        settings = Settings()
        object.__setattr__(settings.app, "export_cache_max_bytes", 5)

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name="test-scrape", version="0.0.0-test")
            exporter = SnapshotExportService(
                settings,
                AsyncMock(),
                AsyncMock(),
                metrics_service_name=service_name,
            )
            exporter._store_cached_value(EXPORT_HISTORY_CACHE, "history-key", b"123")
            self.assertEqual(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "history-key"), b"123")
            self.assertIsNone(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "missing"))
            exporter._store_cached_value(EXPORT_RENDER_CACHE, "render-key", b"1234")
            exporter._store_cached_value(EXPORT_ZIP_CACHE, "zip-key", b"123456")

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_snapshot_export_cache_requests_total", metrics_text)
        self.assertIn(f'service="{service_name}"', metrics_text)
        self.assertIn('cache="history"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn('cache_state="miss"', metrics_text)
        self.assertIn("truenas_jbod_ui_snapshot_export_cache_evictions_total", metrics_text)
        self.assertIn('reason="bytes"', metrics_text)
        self.assertIn("truenas_jbod_ui_snapshot_export_cache_rejections_total", metrics_text)
        self.assertIn('cache="zip"', metrics_text)
        self.assertIn('reason="oversized"', metrics_text)
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_snapshot_export_cache_entries\{{cache="render",service="{service_name}"\}} 1\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_snapshot_export_cache_bytes\{{cache="render",service="{service_name}"\}} 4\.0',
        )
        for forbidden in ("history-key", "render-key", "zip-key", "123456"):
            self.assertNotIn(forbidden, metrics_text)


class HistoryMetricsTests(unittest.TestCase):
    def test_history_metrics_publish_collector_state(self) -> None:
        app = FastAPI()
        scrape_service_name = "test-metrics-scrape"
        history_service_name = "test-history-metrics"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=scrape_service_name, version="0.0.0-test")
            set_history_collector_running(history_service_name, True)
            set_history_collection_schedule_overrun(history_service_name, 2.5)
            observe_history_collection_run(
                service_name=history_service_name,
                result="success",
                duration_seconds=1.25,
                status={
                    "last_scope_count": 3,
                    "last_error": None,
                    "poll_interval_seconds": 300,
                    "background_consecutive_failures": 3,
                    "background_backoff_delay_seconds": 900,
                    "failure_backoff_max_seconds": 900,
                    "last_smart_failure_evidence_disks": 2,
                    "last_max_temperature_celsius": 61,
                    "last_smart_evidence_at": "2026-04-27T16:02:30+00:00",
                    "last_inventory_at": "2026-04-27T16:00:00+00:00",
                    "last_fast_metrics_at": "2026-04-27T16:01:00+00:00",
                    "last_slow_metrics_at": "2026-04-27T16:02:00+00:00",
                    "last_success_at": "2026-04-27T16:03:00+00:00",
                    "last_backup_at": "2026-04-27T16:04:00+00:00",
                },
                counts={
                    "tracked_slots": 24,
                    "event_count": 48,
                    "metric_sample_count": 96,
                },
            )

        metrics_messages = asyncio.run(invoke_asgi(app, "/metrics"))
        metrics_text = response_body(metrics_messages)

        self.assertIn('truenas_jbod_ui_history_collection_runs_total', metrics_text)
        self.assertIn(f'service="{history_service_name}"', metrics_text)
        self.assertIn('result="success"', metrics_text)
        self.assertIn('truenas_jbod_ui_history_last_scope_count', metrics_text)
        self.assertIn('truenas_jbod_ui_history_tracked_slots', metrics_text)
        self.assertIn('truenas_jbod_ui_history_collection_schedule_overrun_seconds', metrics_text)
        self.assertIn(
            f'truenas_jbod_ui_history_collection_schedule_overrun_seconds{{service="{history_service_name}"}} 2.5',
            metrics_text,
        )
        expected_samples = (
            "truenas_jbod_ui_history_collection_interval_seconds",
            "truenas_jbod_ui_history_collection_consecutive_failures",
            "truenas_jbod_ui_history_collection_failure_backoff_seconds",
            "truenas_jbod_ui_history_collection_failure_backoff_max_seconds",
            "truenas_jbod_ui_history_smart_failure_evidence_disks",
            "truenas_jbod_ui_history_max_temperature_celsius",
            "truenas_jbod_ui_history_smart_evidence_timestamp_seconds",
        )
        for metric_name in expected_samples:
            self.assertIn(metric_name, metrics_text)
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_collection_interval_seconds\{{service="{history_service_name}"\}} 300\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_collection_failure_backoff_seconds\{{service="{history_service_name}"\}} 900\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_smart_failure_evidence_disks\{{service="{history_service_name}"\}} 2\.0',
        )
        self.assertRegex(
            metrics_text,
            rf'truenas_jbod_ui_history_max_temperature_celsius\{{service="{history_service_name}"\}} 61\.0',
        )
        for forbidden_label in ("system_id=", "enclosure_id=", "slot=", "serial=", "device_name="):
            matching_lines = [line for line in metrics_text.splitlines() if any(name in line for name in expected_samples)]
            self.assertNotIn(forbidden_label, "\n".join(matching_lines))

    def test_history_metrics_publish_retention_results(self) -> None:
        app = FastAPI()
        scrape_service_name = "test-retention-metrics-scrape"
        history_service_name = "test-history-retention"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=scrape_service_name, version="0.0.0-test")
            observe_history_retention_run(
                service_name=history_service_name,
                result="success",
                duration_seconds=0.75,
                removed_rows={
                    "metric_samples": 12,
                    "events": 3,
                    "hourly_rollups": 2,
                    "daily_rollups": 1,
                },
                completed_at="2026-07-01T00:00:00+00:00",
            )

        metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_history_retention_runs_total", metrics_text)
        self.assertIn('result="success"', metrics_text)
        self.assertIn("truenas_jbod_ui_history_retention_duration_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_history_retention_rows_removed_total", metrics_text)
        self.assertIn('table="metric_samples"', metrics_text)
        self.assertIn("truenas_jbod_ui_history_last_retention_timestamp_seconds", metrics_text)


class ScheduledBackupMetricsTests(unittest.TestCase):
    def test_metrics_module_imports_with_valid_scheduled_backup_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "included_groups": ["history_db"],
                        "success_count": 1,
                        "failure_count": 0,
                        "last_attempt_at": "2030-01-01T03:04:05+00:00",
                        "last_success_at": "2030-01-01T03:04:05+00:00",
                        "last_failure_at": None,
                        "last_size_bytes": 12345,
                        "last_sha256": "a" * 64,
                        "last_artifact_name": (
                            "jbod-scheduled-backup-20300101T030405Z-1234abcd.tar.zst.enc"
                        ),
                        "last_absent_groups": [],
                        "last_retention_removed": 0,
                        "last_error_code": None,
                    }
                ),
                encoding="utf-8",
            )
            status_file.chmod(0o600)
            result = subprocess.run(
                [sys.executable, "-c", "import app.metrics"],
                cwd=Path(__file__).resolve().parents[1],
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "SCHEDULED_BACKUP_STATUS_FILE": str(status_file),
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scheduled_backup_metrics_reject_malformed_durable_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps({"schema_version": 1, "success_count": "not-an-integer"}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"SCHEDULED_BACKUP_STATUS_FILE": str(status_file)},
                clear=False,
            ):
                self.assertIsNone(ScheduledBackupStatusCollector._read_status())

    def test_scheduled_backup_metrics_read_durable_status_without_private_labels(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            status_file = Path(temp_dir) / "scheduled-backup.json"
            status_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "success_count": 4,
                        "failure_count": 2,
                        "last_attempt_at": "2030-01-01T03:04:05+00:00",
                        "last_success_at": (
                            datetime.now(timezone.utc) - timedelta(seconds=90)
                        ).isoformat(),
                        "last_failure_at": "2030-01-01T03:04:05+00:00",
                        "last_size_bytes": 12345,
                        "last_sha256": "a" * 64,
                        "last_error_code": None,
                        "last_artifact_name": (
                            "jbod-scheduled-backup-20300101T030405Z-1234abcd.tar.zst.enc"
                        ),
                        "included_groups": ["private-group"],
                        "last_absent_groups": [],
                        "last_retention_removed": 1,
                    }
                ),
                encoding="utf-8",
            )
            status_file.chmod(0o600)
            with patch.dict(
                "os.environ",
                {
                    "METRICS_ENABLED": "true",
                    "METRICS_PATH": "/metrics",
                    "SCHEDULED_BACKUP_STATUS_FILE": str(status_file),
                },
                clear=False,
            ):
                install_metrics(app, service_name="test-scheduled-scrape", version="0.0.0-test")
                metrics_text = response_body(asyncio.run(invoke_asgi(app, "/metrics")))

        self.assertIn("truenas_jbod_ui_scheduled_backup_runs_total", metrics_text)
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_runs_total{result="success",service="enclosure-backup"} 4.0',
            metrics_text,
        )
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_runs_total{result="error",service="enclosure-backup"} 2.0',
            metrics_text,
        )
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_success_timestamp_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_failure_timestamp_seconds", metrics_text)
        self.assertIn("truenas_jbod_ui_scheduled_backup_last_success_age_seconds", metrics_text)
        self.assertIn(
            'truenas_jbod_ui_scheduled_backup_last_size_bytes{service="enclosure-backup"} 12345.0',
            metrics_text,
        )
        self.assertNotIn("private-name", metrics_text)
        self.assertNotIn("private-group", metrics_text)
        self.assertNotIn(str(status_file), metrics_text)


class InventoryMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_metrics_publish_snapshot_cache_states(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-inventory", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id="metrics-snapshot-system")
                service._build_snapshot = AsyncMock(
                    return_value=InventorySnapshot(
                        slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="da0")],
                        refresh_interval_seconds=30,
                        selected_system_id="metrics-snapshot-system",
                        selected_system_platform="core",
                    )
                )

                await service.get_snapshot(force_refresh=True)
                await service.get_snapshot()

        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_inventory_snapshot_requests_total", metrics_text)
        self.assertIn('system_id="metrics-snapshot-system"', metrics_text)
        self.assertIn('cache_state="forced-refresh"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn("truenas_jbod_ui_inventory_snapshot_cache_entries", metrics_text)

    async def test_inventory_metrics_publish_source_bundle_states(self) -> None:
        class DummyTrueNASClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-bundle", version="0.0.0-test")
                service = build_inventory_service(
                    temp_dir=temp_dir,
                    system_id="metrics-bundle-system",
                    truenas_client=DummyTrueNASClient(),
                )

                await service._get_inventory_source_bundle(force_refresh=True)
                await service._get_inventory_source_bundle()

        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_inventory_source_bundle_requests_total", metrics_text)
        self.assertIn('system_id="metrics-bundle-system"', metrics_text)
        self.assertIn('cache_state="forced-refresh"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
        self.assertIn("truenas_jbod_ui_inventory_source_bundle_build_duration_seconds", metrics_text)

    async def test_inventory_metrics_publish_smart_cache_states(self) -> None:
        app = FastAPI()
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-smart", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id="metrics-smart-system")
                slot_view = SlotView(
                    slot=7,
                    slot_label="07",
                    row_index=0,
                    column_index=7,
                    device_name="da7",
                )
                cache_key = service._smart_cache_key(slot_view)
                service._smart_cache[cache_key] = SmartSummaryView(available=True, power_on_hours=1200)
                service._smart_cache_until[cache_key] = datetime.now(timezone.utc) + timedelta(minutes=5)

                summary = await service._get_slot_smart_summary_for_slot_view(slot_view)

        self.assertTrue(summary.available)
        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_smart_summary_requests_total", metrics_text)
        self.assertIn('system_id="metrics-smart-system"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)

    async def test_smart_cache_retention_eviction_updates_entry_gauge_before_cache_hit_returns(self) -> None:
        app = FastAPI()
        system_id = "metrics-smart-eviction"
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
                install_metrics(app, service_name="test-metrics-scrape-smart-eviction", version="0.0.0-test")
                service = build_inventory_service(temp_dir=temp_dir, system_id=system_id)
                expired_slot = SlotView(
                    slot=6,
                    slot_label="06",
                    row_index=0,
                    column_index=6,
                    device_name="da6",
                )
                fresh_slot = SlotView(
                    slot=7,
                    slot_label="07",
                    row_index=0,
                    column_index=7,
                    device_name="da7",
                )
                expired_key = service._smart_cache_key(expired_slot)
                fresh_key = service._smart_cache_key(fresh_slot)
                service._smart_cache[expired_key] = SmartSummaryView(available=True, power_on_hours=600)
                service._smart_cache[fresh_key] = SmartSummaryView(available=True, power_on_hours=700)
                service._smart_cache_until[expired_key] = (
                    datetime.now(timezone.utc)
                    - service._smart_cache_stale_retention()
                    - timedelta(seconds=1)
                )
                service._smart_cache_until[fresh_key] = datetime.now(timezone.utc) + timedelta(minutes=5)
                service._observe_inventory_cache_metrics()

                summary = await service._get_slot_smart_summary_for_slot_view(fresh_slot)

        self.assertEqual(summary.power_on_hours, 700)
        self.assertEqual(len(service._smart_cache), 1)
        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)
        gauge_line = next(
            line
            for line in metrics_text.splitlines()
            if line.startswith("truenas_jbod_ui_smart_summary_cache_entries{")
            and f'system_id="{system_id}"' in line
        )
        self.assertTrue(gauge_line.endswith(" 1.0"), gauge_line)
