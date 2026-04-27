from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings, SystemConfig, TrueNASConfig
from app.metrics import install_metrics
from app.metrics import observe_history_collection_run
from app.metrics import set_history_collector_running
from app.models.domain import InventorySnapshot, SlotView, SmartSummaryView
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
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


class HistoryMetricsTests(unittest.TestCase):
    def test_history_metrics_publish_collector_state(self) -> None:
        app = FastAPI()
        scrape_service_name = "test-metrics-scrape"
        history_service_name = "test-history-metrics"

        with patch.dict("os.environ", {"METRICS_ENABLED": "true", "METRICS_PATH": "/metrics"}, clear=False):
            install_metrics(app, service_name=scrape_service_name, version="0.0.0-test")
            set_history_collector_running(history_service_name, True)
            observe_history_collection_run(
                service_name=history_service_name,
                result="success",
                duration_seconds=1.25,
                status={
                    "last_scope_count": 3,
                    "last_error": None,
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
                cache_key = "metrics-smart-system|da7"
                service._smart_cache[cache_key] = SmartSummaryView(available=True, power_on_hours=1200)
                service._smart_cache_until[cache_key] = datetime.now(timezone.utc) + timedelta(minutes=5)

                summary = await service._get_slot_smart_summary_for_slot_view(slot_view)

        self.assertTrue(summary.available)
        metrics_messages = await invoke_asgi(app, "/metrics")
        metrics_text = response_body(metrics_messages)

        self.assertIn("truenas_jbod_ui_smart_summary_requests_total", metrics_text)
        self.assertIn('system_id="metrics-smart-system"', metrics_text)
        self.assertIn('cache_state="hit"', metrics_text)
