from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.datastructures import URLPath
from starlette.requests import Request

from app.config import BMCConfig, HANodeConfig, SSHConfig, Settings, SystemConfig, TrueNASConfig, get_settings
from app.main import templates
from app.models.domain import (
    EnclosureOption,
    InventorySnapshot,
    InventorySummary,
    SnapshotExportRequest,
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
    SnapshotRedactor,
    collect_configured_hostnames,
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

    @staticmethod
    def cache_settings(*, max_bytes: int, max_entries: int = 8, ttl_seconds: int = 60) -> Settings:
        settings = Settings()
        object.__setattr__(settings.app, "export_cache_max_bytes", max_bytes)
        settings.app.export_cache_max_entries = max_entries
        settings.app.export_cache_ttl_seconds = ttl_seconds
        return settings

    def test_export_cache_default_has_a_shared_byte_budget(self) -> None:
        self.assertEqual(Settings().app.export_cache_max_bytes, 32 * 1024 * 1024)

    def test_export_cache_shared_byte_budget_accepts_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "APP_CONFIG_PATH": str(Path(temp_dir) / "config.yaml"),
                "APP_EXPORT_CACHE_MAX_BYTES": "12345",
            },
            clear=False,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
            finally:
                get_settings.cache_clear()

        self.assertEqual(settings.app.export_cache_max_bytes, 12345)

    def test_export_cache_global_lru_evicts_the_oldest_accessed_payload(self) -> None:
        exporter = SnapshotExportService(
            self.cache_settings(max_bytes=10),
            FakeHistoryBackend(),
            templates,
        )

        self.assertTrue(exporter._store_cached_value(EXPORT_HISTORY_CACHE, "history", b"1111"))
        self.assertTrue(exporter._store_cached_value(EXPORT_RENDER_CACHE, "render", b"2222"))
        self.assertEqual(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "history"), b"1111")
        self.assertTrue(exporter._store_cached_value(EXPORT_ZIP_CACHE, "zip", b"3333"))

        self.assertIn("history", EXPORT_HISTORY_CACHE)
        self.assertNotIn("render", EXPORT_RENDER_CACHE)
        self.assertIn("zip", EXPORT_ZIP_CACHE)
        self.assertEqual(exporter._cache_total_size_bytes(), 8)
        self.assertEqual(EXPORT_HISTORY_CACHE["history"].size_bytes, 4)
        self.assertEqual(EXPORT_ZIP_CACHE["zip"].size_bytes, 4)

    def test_export_cache_entry_limit_remains_per_cache(self) -> None:
        exporter = SnapshotExportService(
            self.cache_settings(max_bytes=100, max_entries=2),
            FakeHistoryBackend(),
            templates,
        )

        exporter._store_cached_value(EXPORT_HISTORY_CACHE, "one", b"1")
        exporter._store_cached_value(EXPORT_HISTORY_CACHE, "two", b"22")
        exporter._store_cached_value(EXPORT_HISTORY_CACHE, "three", b"333")

        self.assertEqual(list(EXPORT_HISTORY_CACHE), ["two", "three"])
        self.assertEqual(exporter._cache_total_size_bytes(), 5)

    def test_export_cache_ttl_eviction_releases_accounted_bytes(self) -> None:
        exporter = SnapshotExportService(
            self.cache_settings(max_bytes=100, ttl_seconds=5),
            FakeHistoryBackend(),
            templates,
        )
        with patch("app.services.snapshot_export.time.monotonic", return_value=100.0):
            exporter._store_cached_value(EXPORT_HISTORY_CACHE, "history", b"12345")

        with patch("app.services.snapshot_export.time.monotonic", return_value=106.0):
            self.assertIsNone(exporter._get_cached_value(EXPORT_HISTORY_CACHE, "history"))

        self.assertEqual(exporter._cache_total_size_bytes(), 0)

    def test_oversized_export_cache_entry_is_returned_but_not_retained(self) -> None:
        exporter = SnapshotExportService(
            self.cache_settings(max_bytes=5),
            FakeHistoryBackend(),
            templates,
        )
        self.assertTrue(exporter._store_cached_value(EXPORT_HISTORY_CACHE, "existing", b"12"))

        self.assertFalse(exporter._store_cached_value(EXPORT_ZIP_CACHE, "oversized", b"123456"))

        self.assertEqual(EXPORT_HISTORY_CACHE["existing"].value, b"12")
        self.assertNotIn("oversized", EXPORT_ZIP_CACHE)
        self.assertEqual(exporter._cache_total_size_bytes(), 2)

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
        self.assertIn("Artifact app v", rendered.html)
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
        self.assertNotIn('id="sas-fabric-view-link"', rendered.html)

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

    async def test_partial_export_redacts_configured_hostnames_from_all_embedded_payloads(self) -> None:
        hostnames = {
            "api": "api206.redact.invalid",
            "ssh": "ssh206.redact.invalid",
            "extra": "extra206.redact.invalid",
            "bmc": "bmc206.redact.invalid",
            "ha": "ha206.redact.invalid",
            "short": "ha1",
        }
        synthetic_credentials = {
            "api": "fixture-api-credential-206",
            "ssh": "fixture-ssh-credential-206",
            "bmc": "fixture-bmc-credential-206",
        }
        source_system = SystemConfig(
            id="archive-core",
            label="Archive CORE",
            truenas=TrueNASConfig(
                host=f"https://{hostnames['api']}:8443/api/v2",
                api_key=synthetic_credentials["api"],
            ),
            ssh=SSHConfig(
                enabled=True,
                host=hostnames["ssh"],
                extra_hosts=[hostnames["extra"], "0", ""],
                ha_enabled=True,
                ha_nodes=[
                    HANodeConfig(host=hostnames["ha"]),
                    HANodeConfig(host=hostnames["short"]),
                    HANodeConfig(host=None),
                ],
                password=synthetic_credentials["ssh"],
            ),
            bmc=BMCConfig(
                enabled=True,
                host=f"https://{hostnames['bmc']}:443/redfish/v1",
                password=synthetic_credentials["bmc"],
            ),
        )
        snapshot = build_snapshot()
        snapshot.warnings = [
            f"API request to https://{hostnames['api']}:8443/api/v2 failed at 2026-09-02T00:01:00Z; "
            f"unrelated {hostnames['api']}.example collapses too; "
            f"prefix{hostnames['api']} stays literal.",
            f"SSH warning from {hostnames['ssh']}.",
        ]
        snapshot.slots[0].raw_status = {
            "quantastor_ssh_hosts_by_system_id": {"archive-core": hostnames["ssh"]},
            "sas_address_hint": "0",
        }
        smart_summary_cache = build_smart_summary_cache()
        smart_summary_cache["0"]["detail"] = (
            f"HA detail from {hostnames['ha']} and short host {hostnames['short']}; "
            f"unrelated x{hostnames['short']} stays literal"
        )
        storage_view_runtime = build_storage_view_runtime()
        storage_view_runtime.views[0].notes = [f"Storage view collected through {hostnames['extra']}"]
        storage_view_smart_summary_cache = build_storage_view_smart_summary_cache()
        storage_view_smart_summary_cache["boot-doms"]["0"]["detail"] = (
            f"BMC detail from {hostnames['bmc']}"
        )

        class ConfiguredHostnameHistoryBackend(FakeHistoryBackend):
            async def get_scope_history(self, **kwargs: Any) -> dict[int, dict[str, object]]:
                histories = await super().get_scope_history(**kwargs)
                for history in histories.values():
                    history["detail"] = f"History cache fetched through {hostnames['extra']}"
                return histories

        exporter = SnapshotExportService(Settings(), ConfiguredHostnameHistoryBackend(), templates)
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=smart_summary_cache,
            storage_view_runtime=storage_view_runtime,
            storage_view_smart_summary_cache=storage_view_smart_summary_cache,
            selected_slot=0,
            history_window_hours=None,
            history_panel_open=True,
            io_chart_mode="total",
            redact_sensitive=True,
            configured_hostnames=collect_configured_hostnames(source_system.model_dump(mode="json")),
        )

        serialized_surfaces = [
            rendered.snapshot.model_dump_json(),
            json.dumps(rendered.history_cache),
            json.dumps(rendered.smart_summary_cache),
            rendered.html,
        ]
        for hostname in hostnames.values():
            with self.subTest(hostname=hostname):
                hostname_pattern = re.compile(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(hostname)}(?![A-Za-z0-9_.-])"
                )
                self.assertFalse(any(hostname_pattern.search(surface) for surface in serialized_surfaces))
        for credential in synthetic_credentials.values():
            self.assertFalse(any(credential in surface for surface in serialized_surfaces))
        self.assertIn("https://host-01:8443/api/v2", rendered.html)
        self.assertIn("SSH warning from host-01.", rendered.html)
        self.assertIn("2026-09-02T00:01:00Z", rendered.html)
        self.assertNotIn(f"{hostnames['api']}.example", rendered.html)
        self.assertIn("unrelated host-01 collapses too;", rendered.html)
        self.assertIn(f"prefix{hostnames['api']} stays literal.", rendered.html)
        self.assertIn(f"x{hostnames['short']}", rendered.html)
        self.assertEqual(rendered.snapshot.slots[0].raw_status["sas_address_hint"], "0")
        self.assertIn("host-01", rendered.html)
        self.assertIn("enc-01", rendered.html)

    def test_configured_hostname_collection_normalizes_a_dns_root_dot(self) -> None:
        configured_hostnames = collect_configured_hostnames(
            {
                "truenas": {
                    "host": "https://NAS206.EXAMPLE.INVALID.:8443/api",
                    "tls_server_name": "TLS206.EXAMPLE.INVALID.",
                }
            }
        )
        redactor = SnapshotRedactor(
            build_snapshot(),
            {},
            {},
            configured_hostnames=configured_hostnames,
        )

        self.assertEqual(
            configured_hostnames,
            ["nas206.example.invalid", "tls206.example.invalid"],
        )
        self.assertEqual(
            redactor.redact_object(
                "collector failed on nas206.example.invalid for tls206.example.invalid"
            ),
            "collector failed on host-01 for host-01",
        )

    def test_redactor_scrubs_enclosure_raw_label_and_operator_alias(self) -> None:
        snapshot = build_snapshot()
        snapshot.enclosures[0].raw_label = "Private Rack Three East"
        snapshot.enclosures[0].alias = "Private Archive Cold"
        redactor = SnapshotRedactor(snapshot, {}, {})

        redacted = redactor.redact_snapshot(snapshot)

        self.assertEqual(redacted.enclosures[0].raw_label, "enc-01")
        self.assertEqual(redacted.enclosures[0].alias, "enc-01")

    async def test_none_export_preserves_configured_hostname_fidelity(self) -> None:
        api_hostname = "api206.none.invalid"
        source_system = SystemConfig(
            id="archive-core",
            label="Archive CORE",
            truenas=TrueNASConfig(host=f"https://{api_hostname}:8443/api/v2"),
        )
        snapshot = build_snapshot()
        original_warning = f"API request to https://{api_hostname}:8443/api/v2 returned full identifiers"
        snapshot.warnings = [original_warning]
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            redact_sensitive=False,
            configured_hostnames=collect_configured_hostnames(source_system.model_dump(mode="json")),
        )

        self.assertEqual(rendered.snapshot.warnings, [original_warning])
        self.assertIn(original_warning, rendered.html)
        self.assertEqual(rendered.export_meta["redaction"], "none")
        self.assertEqual(rendered.export_meta["redaction_label"], "None")

    async def test_partial_export_cache_varies_with_serialized_source_hostnames(self) -> None:
        hostname = "cache206.redact.invalid"
        snapshot = build_snapshot()
        snapshot.warnings = [f"Collector failed on {hostname}"]
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        common_args = {
            "request": build_request(),
            "snapshot": snapshot,
            "smart_summary_cache": build_smart_summary_cache(),
            "selected_slot": 0,
            "history_window_hours": 24,
            "io_chart_mode": "total",
            "redact_sensitive": True,
        }

        unrecognized = await exporter.build_enclosure_snapshot_html(
            **common_args,
            configured_hostnames=collect_configured_hostnames(
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(host="https://other206.redact.invalid"),
                ).model_dump(mode="json")
            ),
        )
        recognized = await exporter.build_enclosure_snapshot_html(
            **common_args,
            configured_hostnames=collect_configured_hostnames(
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(host=f"https://{hostname}"),
                ).model_dump(mode="json")
            ),
        )

        self.assertIn(hostname, unrecognized.html)
        self.assertNotIn(hostname, recognized.html)
        self.assertIn("Collector failed on host-01", recognized.html)

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

    async def test_html_and_fitting_auto_exports_do_not_build_unused_zip(self) -> None:
        snapshot = build_snapshot()
        request = build_request()

        for packaging in ("html", "auto"):
            with self.subTest(packaging=packaging):
                EXPORT_RENDER_CACHE.clear()
                EXPORT_ZIP_CACHE.clear()
                exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
                zip_builder = MagicMock(
                    side_effect=AssertionError("ZIP must stay lazy for HTML output")
                )
                exporter._build_zip_archive = zip_builder  # type: ignore[method-assign]

                artifact = await exporter.build_enclosure_snapshot_export(
                    request=request,
                    snapshot=snapshot,
                    smart_summary_cache=build_smart_summary_cache(),
                    selected_slot=0,
                    history_window_hours=24,
                    io_chart_mode="total",
                    packaging=packaging,
                    allow_oversize=True,
                )

                self.assertEqual(artifact.packaging, "html")
                zip_builder.assert_not_called()

    async def test_zip_compression_does_not_block_the_event_loop(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        original_builder = exporter._build_zip_archive
        loop = asyncio.get_running_loop()
        event_loop_released = threading.Event()

        def blocking_builder(html_filename: str, html_content: bytes) -> bytes:
            loop.call_soon_threadsafe(event_loop_released.set)
            if not event_loop_released.wait(timeout=0.5):
                raise AssertionError("ZIP compression blocked the event loop")
            return original_builder(html_filename, html_content)

        exporter._build_zip_archive = blocking_builder  # type: ignore[method-assign]

        artifact = await exporter.build_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="zip",
            allow_oversize=True,
        )

        self.assertEqual(artifact.packaging, "zip")
        self.assertTrue(event_loop_released.is_set())

    async def test_zip_estimation_does_not_block_the_event_loop(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        original_builder = exporter._build_zip_archive
        loop = asyncio.get_running_loop()
        event_loop_released = threading.Event()

        def blocking_builder(html_filename: str, html_content: bytes) -> bytes:
            loop.call_soon_threadsafe(event_loop_released.set)
            if not event_loop_released.wait(timeout=0.5):
                raise AssertionError("ZIP estimation blocked the event loop")
            return original_builder(html_filename, html_content)

        exporter._build_zip_archive = blocking_builder  # type: ignore[method-assign]

        estimate = await exporter.estimate_enclosure_snapshot_export(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
            packaging="auto",
        )

        self.assertTrue(estimate["ok"])
        self.assertTrue(event_loop_released.is_set())

    async def test_asset_inlining_does_not_block_the_event_loop(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        original_inliner = exporter._inline_static_assets
        loop = asyncio.get_running_loop()
        event_loop_released = threading.Event()

        def blocking_inliner(request, html: str) -> str:
            loop.call_soon_threadsafe(event_loop_released.set)
            if not event_loop_released.wait(timeout=0.5):
                raise AssertionError("Template rendering blocked the event loop")
            return original_inliner(request, html)

        exporter._inline_static_assets = blocking_inliner  # type: ignore[method-assign]

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )

        self.assertGreater(rendered.size_bytes, 0)
        self.assertTrue(event_loop_released.is_set())

    async def test_template_rendering_does_not_block_the_event_loop(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        original_get_template = exporter.templates.env.get_template
        real_template = original_get_template("index.html")
        loop = asyncio.get_running_loop()
        event_loop_released = threading.Event()

        class BlockingTemplate:
            def render(self, context) -> str:
                loop.call_soon_threadsafe(event_loop_released.set)
                if not event_loop_released.wait(timeout=0.5):
                    raise AssertionError("Jinja template rendering blocked the event loop")
                return real_template.render(context)

        with patch.object(
            exporter.templates.env,
            "get_template",
            side_effect=lambda name, *args, **kwargs: (
                BlockingTemplate()
                if name == "index.html"
                else original_get_template(name, *args, **kwargs)
            ),
        ):
            rendered = await exporter.build_enclosure_snapshot_html(
                request=build_request(),
                snapshot=snapshot,
                smart_summary_cache=build_smart_summary_cache(),
                selected_slot=0,
                history_window_hours=24,
                io_chart_mode="total",
            )

        self.assertGreater(rendered.size_bytes, 0)
        self.assertTrue(event_loop_released.is_set())

    async def test_concurrent_zip_requests_share_one_event_loop_owned_cache_build(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=snapshot,
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )
        html_bytes = rendered.html.encode("utf-8")
        original_builder = exporter._build_zip_archive
        started = threading.Event()
        release = threading.Event()
        build_calls = 0

        def controlled_builder(html_filename: str, html_content: bytes) -> bytes:
            nonlocal build_calls
            build_calls += 1
            started.set()
            if not release.wait(timeout=1):
                raise AssertionError("Concurrent ZIP test did not release compression")
            return original_builder(html_filename, html_content)

        exporter._build_zip_archive = controlled_builder  # type: ignore[method-assign]
        first = asyncio.create_task(exporter._build_zip_archive_cached(rendered, html_bytes))
        self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
        second = asyncio.create_task(exporter._build_zip_archive_cached(rendered, html_bytes))
        await asyncio.sleep(0)
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(build_calls, 1)
        self.assertIs(first_result, second_result)
        self.assertEqual(len(exporter._zip_cache), 1)
        self.assertEqual(exporter._zip_build_tasks, {})

    async def test_concurrent_oversized_zip_requests_share_build_without_retaining_bytes(self) -> None:
        settings = self.cache_settings(max_bytes=1)
        exporter = SnapshotExportService(settings, FakeHistoryBackend(), templates)  # type: ignore[arg-type]
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=build_snapshot(),
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            history_window_hours=24,
            io_chart_mode="total",
        )
        html_bytes = rendered.html.encode("utf-8")
        started = threading.Event()
        release = threading.Event()
        build_calls = 0

        def controlled_builder(_html_filename: str, _html_content: bytes) -> bytes:
            nonlocal build_calls
            build_calls += 1
            started.set()
            if not release.wait(timeout=1):
                raise AssertionError("Concurrent oversized ZIP test did not release compression")
            return b"oversized-zip"

        exporter._build_zip_archive = controlled_builder  # type: ignore[method-assign]
        first = asyncio.create_task(exporter._build_zip_archive_cached(rendered, html_bytes))
        self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
        second = asyncio.create_task(exporter._build_zip_archive_cached(rendered, html_bytes))
        await asyncio.sleep(0)
        release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(first_result, b"oversized-zip")
        self.assertIs(first_result, second_result)
        self.assertEqual(build_calls, 1)
        self.assertEqual(len(exporter._zip_cache), 0)
        self.assertEqual(exporter._zip_build_tasks, {})

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

    async def test_redacted_oversized_export_builds_one_redactor_for_all_strategies(self) -> None:
        snapshot = build_snapshot()
        exporter = SnapshotExportService(
            Settings(),
            DenseHistoryBackend(),
            templates,
            size_limit_bytes=1,
        )  # type: ignore[arg-type]
        redactor_init_count = 0

        class CountingSnapshotRedactor(SnapshotRedactor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                nonlocal redactor_init_count
                redactor_init_count += 1
                super().__init__(*args, **kwargs)

        with patch("app.services.snapshot_export.SnapshotRedactor", CountingSnapshotRedactor):
            rendered = await exporter.build_enclosure_snapshot_html(
                request=build_request(),
                snapshot=snapshot,
                smart_summary_cache=build_smart_summary_cache(),
                selected_slot=0,
                history_window_hours=24,
                io_chart_mode="total",
                redact_sensitive=True,
            )

        self.assertGreater(rendered.size_bytes, exporter.size_limit_bytes)
        self.assertEqual(redactor_init_count, 1)
        self.assertIn("...3456", rendered.html)
        self.assertNotIn("ABC123456", rendered.html)
        self.assertNotIn("192.168.1.174", rendered.html)

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

    def test_request_sanitizes_selected_storage_view_id(self) -> None:
        payload = SnapshotExportRequest(selected_storage_view_id="  boot-doms  ")

        self.assertEqual(payload.selected_storage_view_id, "boot-doms")

    async def test_storage_view_selection_is_restored_in_snapshot_bootstrap(self) -> None:
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=build_snapshot(),
            smart_summary_cache=build_smart_summary_cache(),
            storage_view_runtime=build_storage_view_runtime(),
            storage_view_smart_summary_cache=build_storage_view_smart_summary_cache(),
            selected_slot=1,
            selected_storage_view_id="boot-doms",
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn('initialSelectedStorageViewId: "boot-doms"', rendered.html)
        self.assertIn("initialSelectedSlot: 1", rendered.html)
        self.assertEqual(rendered.export_meta["selected_storage_view_id"], "boot-doms")
        self.assertEqual(rendered.export_meta["selected_slot"], 1)

    async def test_missing_selected_storage_view_clears_live_slot_selection(self) -> None:
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=build_snapshot(),
            smart_summary_cache=build_smart_summary_cache(),
            storage_view_runtime=None,
            selected_slot=0,
            selected_storage_view_id="boot-doms",
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn("initialSelectedStorageViewId: null", rendered.html)
        self.assertIn("initialSelectedSlot: null", rendered.html)
        self.assertIn("initialHistoryPanelOpen: false", rendered.html)
        self.assertIsNone(rendered.export_meta["selected_storage_view_id"])
        self.assertIsNone(rendered.export_meta["selected_slot"])

    async def test_live_slot_selection_remains_unchanged_without_storage_view(self) -> None:
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)

        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(),
            snapshot=build_snapshot(),
            smart_summary_cache=build_smart_summary_cache(),
            selected_slot=0,
            selected_storage_view_id=None,
            history_window_hours=24,
            history_panel_open=True,
            io_chart_mode="total",
        )

        self.assertIn("initialSelectedStorageViewId: null", rendered.html)
        self.assertIn("initialSelectedSlot: 0", rendered.html)
        self.assertEqual(rendered.export_meta["selected_slot"], 0)

    async def test_render_cache_separates_live_and_storage_view_selection(self) -> None:
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        common = {
            "request": build_request(),
            "snapshot": build_snapshot(),
            "smart_summary_cache": build_smart_summary_cache(),
            "storage_view_runtime": build_storage_view_runtime(),
            "selected_slot": 0,
            "history_window_hours": 24,
            "history_panel_open": True,
            "io_chart_mode": "total",
        }

        live = await exporter.build_enclosure_snapshot_html(
            **common,
            selected_storage_view_id=None,
        )
        storage_view = await exporter.build_enclosure_snapshot_html(
            **common,
            selected_storage_view_id="boot-doms",
        )

        self.assertNotEqual(live.cache_key, storage_view.cache_key)
        self.assertIn("initialSelectedStorageViewId: null", live.html)
        self.assertIn('initialSelectedStorageViewId: "boot-doms"', storage_view.html)

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



class SnapshotRedactorIdentifierKeyTests(unittest.TestCase):
    def test_partial_redaction_never_reuses_alias_shaped_system_or_enclosure_ids(self) -> None:
        snapshot = build_snapshot()
        snapshot.selected_system_id = "host-01"
        snapshot.selected_system_label = "host-01"
        snapshot.systems = [
            SystemOption(id="host-01", label="host-01"),
            SystemOption(id="host-02", label="host-02"),
        ]
        snapshot.selected_enclosure_id = "enc-01"
        snapshot.selected_enclosure_label = "enc-01"
        snapshot.enclosures = [
            EnclosureOption(id="enc-01", label="enc-01"),
            EnclosureOption(id="enc-02", label="enc-02"),
        ]
        snapshot.slots[0].enclosure_id = "enc-01"
        snapshot.slots[0].enclosure_label = "enc-01"

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        system_aliases = {system.id for system in redacted.systems}
        enclosure_aliases = {enclosure.id for enclosure in redacted.enclosures}
        self.assertTrue(system_aliases.isdisjoint({"host-01", "host-02"}))
        self.assertTrue(enclosure_aliases.isdisjoint({"enc-01", "enc-02"}))
        self.assertEqual(len(system_aliases), 2)
        self.assertEqual(len(enclosure_aliases), 2)

    def test_partial_redaction_reserves_alias_shaped_ids_from_extra_payloads(self) -> None:
        snapshot = build_snapshot()
        extra_payload = {
            "system_id": "host-01",
            "enclosure_id": "enc-01",
            "detail": "moved from host-01 through enc-01",
        }

        redactor = SnapshotRedactor(snapshot, {}, {}, extra_payloads=[extra_payload])
        redacted_snapshot = redactor.redact_snapshot(snapshot)
        redacted_extra = redactor.redact_object(extra_payload)

        self.assertNotEqual(redacted_snapshot.selected_system_id, "host-01")
        self.assertNotEqual(redacted_snapshot.selected_enclosure_id, "enc-01")
        self.assertNotEqual(redacted_extra["system_id"], "host-01")
        self.assertNotEqual(redacted_extra["enclosure_id"], "enc-01")
        self.assertNotIn("host-01", redacted_extra["detail"])
        self.assertNotIn("enc-01", redacted_extra["detail"])

    def test_partial_redaction_reserves_raw_ids_across_alias_namespaces(self) -> None:
        snapshot = build_snapshot()
        snapshot.selected_system_id = "enc-01"
        snapshot.selected_system_label = "System One"
        snapshot.systems = [SystemOption(id="enc-01", label="System One")]
        snapshot.selected_enclosure_id = "host-01"
        snapshot.selected_enclosure_label = "Enclosure One"
        snapshot.enclosures = [EnclosureOption(id="host-01", label="Enclosure One")]
        snapshot.slots[0].enclosure_id = "host-01"
        snapshot.slots[0].enclosure_label = "Enclosure One"

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        self.assertEqual(redacted.selected_system_id, "host-02")
        self.assertEqual(redacted.selected_enclosure_id, "enc-02")

    def test_partial_redaction_reserves_cross_namespace_ids_case_insensitively(self) -> None:
        snapshot = build_snapshot()
        snapshot.selected_system_id = "ENC-01"
        snapshot.selected_system_label = "System One"
        snapshot.systems = [SystemOption(id="ENC-01", label="System One")]
        snapshot.selected_enclosure_id = "HOST-01"
        snapshot.selected_enclosure_label = "Enclosure One"
        snapshot.enclosures = [EnclosureOption(id="HOST-01", label="Enclosure One")]
        snapshot.slots[0].enclosure_id = "HOST-01"
        snapshot.slots[0].enclosure_label = "Enclosure One"

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        self.assertEqual(redacted.selected_system_id, "host-02")
        self.assertEqual(redacted.selected_enclosure_id, "enc-02")

    def test_partial_redaction_dynamic_aliases_share_raw_id_reservations(self) -> None:
        enclosure_first = SnapshotRedactor(build_snapshot(), {}, {})
        enclosure_first.redact_object({"enclosure_id": "HOST-02"})
        redacted_system = enclosure_first.redact_object({"system_id": "late-system"})

        system_first = SnapshotRedactor(build_snapshot(), {}, {})
        system_first.redact_object({"system_id": "ENC-02"})
        redacted_enclosure = system_first.redact_object({"enclosure_id": "late-enclosure"})

        self.assertEqual(redacted_system["system_id"], "host-03")
        self.assertEqual(redacted_enclosure["enclosure_id"], "enc-03")

    def test_alias_shaped_zero_suffix_ids_are_redacted_in_fields_and_free_text(self) -> None:
        history_cache = {
            "0": {
                "system_id": "host-00",
                "enclosure_id": "enc-00",
                "detail": "moved from host-00 through enc-00",
            }
        }
        redactor = SnapshotRedactor(build_snapshot(), history_cache, {})

        redacted = redactor.redact_history_cache(history_cache)
        system_alias = redacted["0"]["system_id"]
        enclosure_alias = redacted["0"]["enclosure_id"]

        self.assertNotEqual(system_alias, "host-00")
        self.assertNotEqual(enclosure_alias, "enc-00")
        self.assertEqual(
            redacted["0"]["detail"],
            f"moved from {system_alias} through {enclosure_alias}",
        )

    def test_actual_numeric_and_address_zero_sentinels_are_preserved(self) -> None:
        zero_forms = (
            "0",
            "0000",
            "0x0000000000000000",
            "00:00:00:00:00:00",
            "00-00-00-00-00-00",
            "0.0.0.0",
            "::",
        )
        redactor = SnapshotRedactor(build_snapshot(), {}, {})

        for zero_form in zero_forms:
            with self.subTest(zero_form=zero_form):
                payload = {
                    "system_id": zero_form,
                    "enclosure_id": zero_form,
                    "serial": zero_form,
                    "sas_address": zero_form,
                }
                self.assertEqual(redactor.redact_object(payload), payload)

    def test_partial_redaction_does_not_replace_identifier_substrings(self) -> None:
        snapshot = build_snapshot()
        matching_timestamp = datetime(2026, 9, 2, 0, 1, tzinfo=timezone.utc)
        snapshot.last_updated = matching_timestamp
        snapshot.generated_at = matching_timestamp
        snapshot.slots[0].raw_status = {
            "sas_address_hint": "00:01",
            "message": "observed 2026-09-02T00:01:00Z",
        }

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        self.assertNotEqual(redacted.slots[0].raw_status["sas_address_hint"], "00:01")
        self.assertEqual(
            redacted.slots[0].raw_status["message"],
            "observed 2026-09-02T00:01:00Z",
        )

    def test_partial_redaction_does_not_collect_low_entropy_sentinel_tokens(self) -> None:
        snapshot = build_snapshot()
        snapshot.slots[0].raw_status = {
            "sas_address_hint": "0",
            "message": "slot 10 observed at 2026-09-02T00:00:00Z",
        }

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        self.assertEqual(redacted.slots[0].raw_status["sas_address_hint"], "0")
        self.assertEqual(
            redacted.slots[0].raw_status["message"],
            "slot 10 observed at 2026-09-02T00:00:00Z",
        )

    def test_partial_redaction_masks_hint_and_linux_blockdevice_identifier_keys(self) -> None:
        snapshot = build_snapshot()
        slot = snapshot.slots[0]
        slot.serial = "MAINSERIAL0001"
        slot.sas_address = "0x5000c500a1b2c3d4"
        slot.raw_status = {
            # BMC / Quantastor correlation hints carry their own identifiers.
            "serial_hint": "BMCSERIAL9999",
            "sas_address_hint": "5000c500a1b2c3e0",
            # Linux SCALE/generic slots embed the lsblk summary and sysfs transport address.
            "transport_address": "0x5000c500a1b2c3f1",
            "linux_blockdevice": {
                "wwn": "0x5000c500a1b2c3d5",
                "partuuid": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "serial": "MAINSERIAL0001",
            },
        }

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)
        raw_status = redacted.slots[0].raw_status

        self.assertEqual(raw_status["serial_hint"], "...9999")
        self.assertEqual(raw_status["sas_address_hint"], "5000...c3e0")
        self.assertEqual(raw_status["transport_address"], "0x50...c3f1")
        self.assertEqual(raw_status["linux_blockdevice"]["wwn"], "0x50...c3d5")
        self.assertEqual(raw_status["linux_blockdevice"]["partuuid"], "3f25...3301")
        # Mirrored copies of one serial must not widen the disclosed prefix.
        self.assertEqual(raw_status["linux_blockdevice"]["serial"], "...0001")
        self.assertEqual(redacted.slots[0].serial, "...0001")
        for original in (
            "BMCSERIAL9999",
            "5000c500a1b2c3e0",
            "0x5000c500a1b2c3f1",
            "0x5000c500a1b2c3d5",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        ):
            self.assertNotIn(original, redacted.model_dump_json())

    def test_partial_redaction_disambiguates_distinct_serials_with_the_same_suffix(self) -> None:
        snapshot = build_snapshot()
        snapshot.slots[0].serial = "FIRSTSERIAL0001"
        snapshot.slots[0].raw_status = {"serial_hint": "SECONDSERIAL0001"}

        redacted = SnapshotRedactor(snapshot, {}, {}).redact_snapshot(snapshot)

        self.assertEqual(redacted.slots[0].serial, "FI...0001")
        self.assertEqual(redacted.slots[0].raw_status["serial_hint"], "SE...0001")

    def test_short_unknown_system_and_enclosure_ids_are_removed_from_free_text(self) -> None:
        history_cache = {
            "0": {
                "system_id": "unvr",
                "enclosure_id": "252",
                "detail": "moved from unvr enclosure 252",
            }
        }
        redactor = SnapshotRedactor(build_snapshot(), history_cache, {})

        redacted = redactor.redact_history_cache(history_cache)
        system_alias = redacted["0"]["system_id"]
        enclosure_alias = redacted["0"]["enclosure_id"]

        self.assertRegex(system_alias, r"^host-\d{2}$")
        self.assertRegex(enclosure_alias, r"^enc-\d{2}$")
        self.assertEqual(
            redacted["0"]["detail"],
            f"moved from {system_alias} enclosure {enclosure_alias}",
        )
        self.assertNotIn("unvr", json.dumps(redacted))
        self.assertNotIn('"252"', json.dumps(redacted))


class SnapshotRedactorHostnameFormTests(unittest.TestCase):
    @staticmethod
    def _redactor_for(config_host: str) -> SnapshotRedactor:
        return SnapshotRedactor(
            build_snapshot(),
            {},
            {},
            configured_hostnames=collect_configured_hostnames(
                SystemConfig(
                    id="archive-core",
                    label="Archive CORE",
                    truenas=TrueNASConfig(host=config_host),
                ).model_dump(mode="json")
            ),
        )

    def test_short_configured_hostname_also_redacts_its_fqdn(self) -> None:
        redactor = self._redactor_for("https://nas206:8443")

        self.assertEqual(
            redactor.redact_object("SSH timed out for nas206.lab.invalid"),
            "SSH timed out for host-01",
        )
        self.assertEqual(
            redactor.redact_object("nas206-mgmt and xnas206 stay literal"),
            "nas206-mgmt and xnas206 stay literal",
        )

    def test_fqdn_configured_hostname_also_redacts_its_short_name(self) -> None:
        redactor = self._redactor_for("https://nas207.lab.invalid:8443/api/v2")

        self.assertEqual(
            redactor.redact_object("collector on nas207 failed"),
            "collector on host-01 failed",
        )
        self.assertEqual(
            redactor.redact_object("collector on nas207.lab.invalid failed"),
            "collector on host-01 failed",
        )

    def test_configured_hostname_redaction_keeps_the_url_port_and_path(self) -> None:
        redactor = self._redactor_for("https://nas208.lab.invalid")

        self.assertEqual(
            redactor.redact_object("see https://nas208.lab.invalid:8443/ui and nas208:22"),
            "see https://host-01:8443/ui and host-01:22",
        )

    def test_unknown_system_id_in_history_rows_gets_a_freshly_minted_alias(self) -> None:
        history_cache = {
            "0": {"system_id": "other-box", "detail": "slot moved from other-box shelf"}
        }
        redactor = SnapshotRedactor(build_snapshot(), history_cache, {})

        redacted = redactor.redact_history_cache(history_cache)
        again = redactor.redact_history_cache(history_cache)
        minted = redacted["0"]["system_id"]

        self.assertNotIn("other-box", json.dumps(redacted))
        self.assertRegex(minted, r"^host-\d{2}$")
        self.assertNotEqual(minted, "host-01")
        self.assertEqual(redacted["0"]["detail"], f"slot moved from {minted} shelf")
        self.assertEqual(again["0"]["system_id"], minted)

    def test_dotted_system_label_is_redacted_with_its_domain_suffix(self) -> None:
        snapshot = build_snapshot()
        snapshot.systems[0].label = "nas209.lab"
        snapshot.selected_system_label = "nas209.lab"
        snapshot.warnings = ["collector on nas209.lab.invalid failed"]
        redactor = SnapshotRedactor(snapshot, {}, {})

        redacted = redactor.redact_snapshot(snapshot)

        self.assertEqual(redacted.warnings, ["collector on host-01 failed"])


if __name__ == "__main__":
    unittest.main()
