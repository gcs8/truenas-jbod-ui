from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from history_service import main as history_main
from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.domain import MetricSample, SlotStateRecord, build_slot_events, isoformat_utc
from history_service.store import HistoryStore


class HistoryDomainTests(unittest.TestCase):
    def test_build_slot_events_groups_state_and_identity_changes(self) -> None:
        previous = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=12,
            slot_label="12",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da12",
            serial="SERIAL-OLD",
            model="Old Model",
            gptid="gptid/old",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="GPTID",
            logical_unit_id="0x5000cca27c7f1111",
            sas_address="0x5000cca27c7f1111",
        )
        current = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=12,
            slot_label="12",
            present=True,
            state="fault",
            identify_active=True,
            device_name="da18",
            serial="SERIAL-NEW",
            model="New Model",
            gptid="gptid/new",
            pool_name="tank",
            vdev_name="spare-0",
            health="DEGRADED",
            persistent_id_label="WWN",
            logical_unit_id="0x5000cca27c7f2229",
            sas_address="0x5000cca27c7f2229",
            topology_label="tank > spare-0",
            multipath_device="multipath/disk12",
            multipath_mode="Active/Passive",
            multipath_state="DEGRADED",
            multipath_lunid="0x5000cca27c7f2229",
            multipath_primary_path="da71",
            multipath_alternate_path="da24",
            multipath_active_paths="da71",
            multipath_failed_paths="da24",
            multipath_active_controllers="mpr1",
            multipath_failed_controllers="mpr0",
        )

        events = build_slot_events(previous, current, "2026-04-16T22:00:00+00:00")

        self.assertEqual(
            {event.event_type for event in events},
            {
                "slot_state_changed",
                "slot_identity_changed",
                "slot_topology_changed",
                "slot_multipath_changed",
            },
        )
        identity_event = next(event for event in events if event.event_type == "slot_identity_changed")
        self.assertEqual(identity_event.gptid, "gptid/new")
        self.assertEqual(identity_event.persistent_id_label, "WWN")
        self.assertEqual(identity_event.logical_unit_id, "0x5000cca27c7f2229")
        self.assertEqual(identity_event.sas_address, "0x5000cca27c7f2229")

    def test_build_slot_events_ignores_empty_sas_address_flaps(self) -> None:
        previous = SlotStateRecord(
            system_id="qsosn-ha",
            system_label="QSOSN HA",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN-Left",
            slot=17,
            slot_label="17",
            present=False,
            state="empty",
            identify_active=False,
            device_name=None,
            serial=None,
            model=None,
            gptid=None,
            pool_name=None,
            vdev_name=None,
            health=None,
            sas_address=None,
        )
        current = replace(previous, sas_address="0")

        events = build_slot_events(previous, current, "2026-06-12T12:00:00+00:00")

        self.assertEqual(events, [])

    def test_build_slot_events_ignores_dual_path_sas_address_flaps(self) -> None:
        previous = SlotStateRecord(
            system_id="qsosn-ha",
            system_label="QSOSN HA",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN-Left",
            slot=3,
            slot_label="03",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="disk/by-id/scsi-SAMSUNG_SERIAL-3",
            serial="SERIAL-3",
            model="SAMSUNG MZILT3T8HALS0D3",
            gptid="scsi-SAMSUNG_SERIAL-3",
            pool_name="HA-Pool-R10",
            vdev_name="mirror-0",
            health="ONLINE",
            sas_address="0x5000c500abcdef02",
        )
        current = replace(previous, sas_address="0x5000c500abcdef03")

        events = build_slot_events(previous, current, "2026-06-12T12:00:00+00:00")

        self.assertEqual(events, [])

    def test_build_slot_events_ignores_quantastor_sas_path_nibble_flaps(self) -> None:
        previous = SlotStateRecord(
            system_id="qsosn-ha",
            system_label="QSOSN HA",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN-Left",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="disk/by-id/scsi-S40BNF0M603885",
            serial="S40BNF0M603885",
            model="SAMSUNG MZILT3T8HALS0D3",
            gptid="scsi-S40BNF0M603885",
            pool_name="HA-Pool-R10",
            vdev_name="mirror-0",
            health="ONLINE",
            sas_address="5002538b496a5512",
        )
        current = replace(previous, sas_address="5002538b496a5510")

        events = build_slot_events(previous, current, "2026-06-12T17:00:25+00:00")

        self.assertEqual(events, [])

    def test_build_slot_events_treats_presence_flaps_as_state_only(self) -> None:
        present = SlotStateRecord(
            system_id="unvr-pro",
            system_label="UniFi UNVR Pro",
            enclosure_key="front-7",
            enclosure_id="front-7",
            enclosure_label="Front 7 Bay",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdb",
            serial="Y5F2A056FJKH",
            model="TOSHIBA_MG09ACA16TE",
            gptid="0x5000039e68d25d38",
            pool_name="/volume/7400794a-85c0-46e6-b7e1-3cb98dee6b2f",
            vdev_name="md3",
            health="good",
            topology_label="/volume/7400794a-85c0-46e6-b7e1-3cb98dee6b2f > md3 > data",
        )
        missing = replace(
            present,
            present=False,
            state="unknown",
            device_name=None,
            serial=None,
            model=None,
            gptid=None,
            pool_name=None,
            vdev_name=None,
            health=None,
            topology_label=None,
            disk_identity_key=None,
        )

        missing_events = build_slot_events(present, missing, "2026-06-12T17:33:12+00:00")
        restored_events = build_slot_events(missing, present, "2026-06-12T17:38:40+00:00")

        self.assertEqual([event.event_type for event in missing_events], ["slot_state_changed"])
        self.assertEqual([event.event_type for event in restored_events], ["slot_state_changed"])

    def test_build_slot_events_keeps_identity_event_when_serial_changes_with_sas(self) -> None:
        previous = SlotStateRecord(
            system_id="qsosn-ha",
            system_label="QSOSN HA",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN-Left",
            slot=3,
            slot_label="03",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="disk/by-id/scsi-SAMSUNG_SERIAL-3",
            serial="SERIAL-3",
            model="SAMSUNG MZILT3T8HALS0D3",
            gptid="scsi-SAMSUNG_SERIAL-3",
            pool_name="HA-Pool-R10",
            vdev_name="mirror-0",
            health="ONLINE",
            sas_address="0x5000c500abcdef02",
        )
        current = replace(
            previous,
            device_name="disk/by-id/scsi-SAMSUNG_SERIAL-4",
            serial="SERIAL-4",
            gptid="scsi-SAMSUNG_SERIAL-4",
            sas_address="0x5000c500abcdef03",
        )

        events = build_slot_events(previous, current, "2026-06-12T12:00:00+00:00")

        self.assertEqual({event.event_type for event in events}, {"slot_identity_changed"})


class HistoryConfigTests(unittest.TestCase):
    def test_history_settings_default_request_timeout_allows_slow_live_inventory(self) -> None:
        self.assertEqual(HistorySettings().request_timeout_seconds, 45)

    def test_history_settings_default_failure_backoff_is_bounded(self) -> None:
        settings = HistorySettings()

        self.assertEqual(settings.failure_backoff_initial_seconds, 30)
        self.assertEqual(settings.failure_backoff_max_seconds, 900)

    def test_history_settings_default_backup_interval_matches_slow_interval(self) -> None:
        settings = HistorySettings()

        self.assertEqual(settings.backup_interval_seconds, settings.slow_interval_seconds)

    def test_history_settings_fast_collection_uses_cached_inventory_by_default(self) -> None:
        self.assertFalse(HistorySettings().force_inventory_on_fast_collection)

    def test_history_settings_uses_sqlite_parent_for_backup_dirs_when_sqlite_path_changes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        settings = HistorySettings(sqlite_path=str(temp_dir / "history.db"))

        self.assertEqual(settings.backup_dir, str(temp_dir / "backups"))
        self.assertEqual(settings.long_term_backup_dir, str(temp_dir / "backups" / "long-term"))

    def test_history_settings_rebases_long_term_backup_dir_when_backup_dir_changes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        settings = HistorySettings(backup_dir=str(temp_dir / "backups"))

        self.assertEqual(settings.long_term_backup_dir, str(temp_dir / "backups" / "long-term"))


class HistoryDashboardRouteTests(unittest.TestCase):
    def test_dashboard_renders_fast_and_full_refresh_controls(self) -> None:
        markup = history_main.render_dashboard(
            {"collector_running": True},
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            app_version="0.test",
            release_status={"summary": "dev build"},
        )

        self.assertIn('id="history-refresh-fast"', markup)
        self.assertIn('id="history-refresh-full"', markup)
        self.assertIn("/api/history/refresh?mode=", markup)
        self.assertIn("const body = await response.text();", markup)
        self.assertIn("JSON.parse(body)", markup)
        self.assertIn("Next background pass", markup)
        self.assertIn("Background backoff", markup)
        self.assertIn("Last collection duration", markup)
        self.assertIn("Last collection inventory", markup)
        self.assertIn("DB Size", markup)
        self.assertIn("collector-activity-banner", markup)
        self.assertIn("pollCollectorStatus", markup)
        self.assertIn("pollOverviewStatus", markup)
        self.assertIn("__HISTORY_DASHBOARD_POLL", markup)
        self.assertIn('id="status-current-collection"', markup)
        self.assertIn('id="collector-state-value"', markup)
        self.assertIn('id="tracked-scopes-body"', markup)

    def test_dashboard_renders_collection_activity_banner_state(self) -> None:
        markup = history_main.render_dashboard(
            {
                "collector_running": True,
                "collection_running": True,
                "collection_kind": "background",
                "collection_activity": "collecting SMART metrics for Archive CORE / Front Shelf (1/2)",
                "collection_elapsed_seconds": 42,
            },
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            app_version="0.test",
            database_size_bytes=1536,
        )

        self.assertIn("collecting SMART metrics", markup)
        self.assertIn("1.5 KiB", markup)
        self.assertIn("History ${kind} collection running", markup)
        self.assertIn("renderCollectorStatus(payload)", markup)
        self.assertIn("renderOverview(initialOverviewPayload)", markup)

    def test_history_refresh_endpoint_forces_fast_collection(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")

        with (
            patch.object(history_main.collector, "run_once", new_callable=AsyncMock) as run_once,
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}),
            patch.object(history_main.store, "list_scopes", return_value=[]),
        ):
            payload = asyncio.run(route.endpoint(mode="fast"))

        run_once.assert_awaited_once_with(
            force_fast=True,
            force_slow=False,
            include_due_intervals=False,
            cached_root_only=True,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "fast")
        self.assertFalse(payload["counts_exact"])

    def test_history_refresh_endpoint_forces_full_collection(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")

        with (
            patch.object(history_main.collector, "run_once", new_callable=AsyncMock) as run_once,
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}),
            patch.object(history_main.store, "list_scopes", return_value=[]),
        ):
            payload = asyncio.run(route.endpoint(mode="full"))

        run_once.assert_awaited_once_with(
            force_fast=True,
            force_slow=True,
            include_due_intervals=False,
            cached_root_only=False,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "full")

    def test_history_refresh_endpoint_returns_json_error_on_collection_failure(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")

        with (
            patch.object(
                history_main.collector,
                "run_once",
                new_callable=AsyncMock,
                side_effect=RuntimeError("POST http://enclosure-ui:8000/api/slots/smart-batch timed out after 45s"),
            ) as run_once,
            patch.object(
                history_main.collector,
                "status",
                return_value={
                    "collector_running": True,
                    "last_error": "POST http://enclosure-ui:8000/api/slots/smart-batch timed out after 45s",
                },
            ),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}),
            patch.object(history_main.store, "list_scopes", return_value=[]),
            patch.object(history_main.logger, "exception"),
        ):
            response = asyncio.run(route.endpoint(mode="full"))

        run_once.assert_awaited_once_with(
            force_fast=True,
            force_slow=True,
            include_due_intervals=False,
            cached_root_only=False,
        )
        self.assertEqual(response.status_code, 500)
        payload = json.loads(response.body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mode"], "full")
        self.assertIn("timed out after 45s", payload["detail"])
        self.assertFalse(payload["counts_exact"])

    def test_history_refresh_endpoint_reports_existing_collection_as_conflict(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")

        with (
            patch.object(type(history_main.collector), "collection_running", new_callable=PropertyMock, return_value=True),
            patch.object(history_main.collector, "run_once", new_callable=AsyncMock) as run_once,
            patch.object(
                history_main.collector,
                "status",
                return_value={"collector_running": True, "collection_running": True},
            ),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}),
            patch.object(history_main.store, "list_scopes", return_value=[]),
        ):
            response = asyncio.run(route.endpoint(mode="full"))

        run_once.assert_not_awaited()
        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mode"], "full")
        self.assertIn("already running", payload["detail"])

    def test_history_overview_includes_database_size(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/overview")

        with (
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}),
            patch.object(history_main.store, "database_size_bytes", return_value=4096),
            patch.object(history_main.store, "list_scopes", return_value=[]),
        ):
            payload = asyncio.run(route.endpoint(exact_counts=False))

        self.assertEqual(payload["database"]["size_bytes"], 4096)

    def test_history_fetch_json_timeout_reports_url_and_timeout(self) -> None:
        collector = HistoryCollector(
            HistorySettings(source_base_url="http://enclosure-ui:8000", request_timeout_seconds=7),
            MagicMock(),
        )

        with patch("history_service.collector.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(RuntimeError) as captured:
                collector._fetch_json_sync(
                    "/api/slots/smart-batch",
                    {"system_id": "scale-a", "fresh": "true"},
                    "POST",
                    b'{"slots":[1]}',
                    {"Content-Type": "application/json"},
                )

        self.assertIn(
            "POST http://enclosure-ui:8000/api/slots/smart-batch?system_id=scale-a&fresh=true timed out after 7s",
            str(captured.exception),
        )


class HistoryStoreTests(unittest.TestCase):
    def test_database_size_bytes_includes_wal_and_shm_files(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        store = HistoryStore(str(db_path))
        base_size = db_path.stat().st_size
        Path(f"{db_path}-wal").write_bytes(b"w" * 7)
        Path(f"{db_path}-shm").write_bytes(b"s" * 11)

        self.assertEqual(store.database_size_bytes(), base_size + 18)

    def test_latest_backup_snapshot_at_uses_newest_rotated_backup(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        backup_dir = temp_dir / "backups"
        backup_dir.mkdir()
        store = HistoryStore(str(db_path))
        older = backup_dir / "history-20260515T010000Z.sqlite3"
        newer = backup_dir / "history-20260515T020000Z.sqlite3"
        older.write_bytes(b"older")
        newer.write_bytes(b"newer")
        older_time = datetime(2026, 5, 15, 1, 0, tzinfo=timezone.utc).timestamp()
        newer_time = datetime(2026, 5, 15, 2, 0, tzinfo=timezone.utc).timestamp()
        os.utime(older, (older_time, older_time))
        os.utime(newer, (newer_time, newer_time))

        self.assertEqual(
            store.latest_backup_snapshot_at(backup_dir),
            datetime(2026, 5, 15, 2, 0, tzinfo=timezone.utc),
        )

    def test_store_persists_scope_events_and_metrics(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="eui.000000000000001000a075012b91c7cf",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )

        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        changed_record = replace(record, health="DEGRADED")
        store.insert_events(
            build_slot_events(record, changed_record, "2026-04-16T22:10:00+00:00")
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="eui.000000000000001000a075012b91c7cf",
                    persistent_id_label="EUI64",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                )
            ]
        )

        loaded = store.get_slot_state("archive-core", "enc-a", 5)
        events = store.list_slot_events("archive-core", "enc-a", 5)
        samples = store.list_metric_samples("archive-core", "enc-a", 5, metric_name="temperature_c")
        scopes = store.list_scopes()
        counts = store.counts()

        self.assertIsNotNone(loaded)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["value"], 31)
        self.assertEqual(samples[0]["gptid"], "eui.000000000000001000a075012b91c7cf")
        self.assertEqual(samples[0]["persistent_id_label"], "EUI64")
        self.assertEqual(samples[0]["logical_unit_id"], "0x5000cca27c7f0005")
        self.assertEqual(samples[0]["sas_address"], "0x5000cca27c7f1005")
        self.assertEqual(events[0]["gptid"], "eui.000000000000001000a075012b91c7cf")
        self.assertEqual(events[0]["persistent_id_label"], "EUI64")
        self.assertEqual(events[0]["logical_unit_id"], "0x5000cca27c7f0005")
        self.assertEqual(events[0]["sas_address"], "0x5000cca27c7f1005")
        self.assertEqual(loaded.gptid, "eui.000000000000001000a075012b91c7cf")
        self.assertEqual(loaded.persistent_id_label, "EUI64")
        self.assertEqual(loaded.logical_unit_id, "0x5000cca27c7f0005")
        self.assertEqual(loaded.sas_address, "0x5000cca27c7f1005")
        self.assertEqual(len(scopes), 1)
        self.assertEqual(counts["tracked_slots"], 1)
        self.assertEqual(counts["event_count"], 1)
        self.assertEqual(counts["metric_sample_count"], 1)

    def test_scope_history_applies_since_window_before_metric_rank(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="eui.000000000000001000a075012b91c7cf",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-15T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=29,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="eui.000000000000001000a075012b91c7cf",
                    persistent_id_label="EUI64",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="eui.000000000000001000a075012b91c7cf",
                    persistent_id_label="EUI64",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
            ]
        )

        payload = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            metric_limits={"temperature_c": 10},
            since="2026-04-16T00:00:00+00:00",
        )

        self.assertEqual([sample["value"] for sample in payload[5]["metrics"]["temperature_c"]], [31])
        self.assertEqual(payload[5]["sample_counts"]["temperature_c"], 1)
        self.assertEqual(payload[5]["latest_values"]["temperature_c"], 31)

    def test_scope_history_can_skip_events_for_metric_only_reads(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="eui.000000000000001000a075012b91c7cf",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        store.upsert_slot_state(record, "2026-04-16T22:00:00+00:00")
        store.insert_events(
            build_slot_events(record, replace(record, health="DEGRADED"), "2026-04-16T22:10:00+00:00")
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="bytes_written",
                    value_integer=100,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="eui.000000000000001000a075012b91c7cf",
                    persistent_id_label="EUI64",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                )
            ]
        )

        payload = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            event_limit=0,
            metric_limits={"bytes_written": 10},
        )

        self.assertEqual(payload[5]["events"], [])
        self.assertEqual(payload[5]["latest_values"]["bytes_written"], 100)

    def test_store_fast_overview_uses_estimated_activity_counts(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/slot-5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )

        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:15:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=32,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                ),
            ]
        )
        with sqlite3.connect(store.file_path) as connection:
            connection.execute("DELETE FROM metric_samples WHERE id = 1")
            connection.commit()

        exact_counts = store.counts()
        estimated_counts = store.estimated_counts()
        fast_scopes = store.list_scopes(include_activity_counts=False)
        exact_scopes = store.list_scopes()

        self.assertEqual(exact_counts["tracked_slots"], 1)
        self.assertEqual(exact_counts["metric_sample_count"], 1)
        self.assertEqual(estimated_counts["tracked_slots"], 1)
        self.assertEqual(estimated_counts["metric_sample_count"], 2)
        self.assertTrue(estimated_counts["estimated"])
        self.assertEqual(estimated_counts["count_mode"], "id_upper_bound")
        self.assertEqual(len(fast_scopes), 1)
        self.assertEqual(fast_scopes[0]["tracked_slots"], 1)
        self.assertIsNone(fast_scopes[0]["event_count"])
        self.assertIsNone(fast_scopes[0]["metric_sample_count"])
        self.assertEqual(fast_scopes[0]["activity_counts_deferred"], 1)
        self.assertEqual(exact_scopes[0]["metric_sample_count"], 1)

    def test_get_slot_history_bundle_auto_follows_matching_disk_metrics_across_homes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        persistent_id = "eui.000000000000001000a075012b91c7cf"
        legacy_record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid=persistent_id,
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        current_record = SlotStateRecord(
            system_id="archive-scale",
            system_label="Archive SCALE",
            enclosure_key="enc-b",
            enclosure_id="enc-b",
            enclosure_label="Rear Shelf",
            slot=11,
            slot_label="11",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdm",
            serial="SERIAL-5",
            model="Drive 5",
            gptid=persistent_id,
            pool_name="tank",
            vdev_name="mirror-1",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )

        store.upsert_slot_state(legacy_record, "2026-04-10T22:00:00+00:00")
        store.upsert_slot_state(current_record, "2026-04-20T22:00:00+00:00")
        store.insert_events(
            build_slot_events(
                current_record,
                replace(current_record, health="DEGRADED"),
                "2026-04-20T23:00:00+00:00",
            )
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-10T23:00:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="bytes_written",
                    value_integer=100,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid=persistent_id,
                    persistent_id_label="EUI64",
                    disk_identity_key=legacy_record.disk_identity_key,
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
                MetricSample(
                    observed_at="2026-04-20T23:00:00+00:00",
                    system_id="archive-scale",
                    system_label="Archive SCALE",
                    enclosure_key="enc-b",
                    enclosure_id="enc-b",
                    enclosure_label="Rear Shelf",
                    slot=11,
                    slot_label="11",
                    metric_name="bytes_written",
                    value_integer=200,
                    value_real=None,
                    device_name="sdm",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid=persistent_id,
                    persistent_id_label="EUI64",
                    disk_identity_key=current_record.disk_identity_key,
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
            ]
        )
        with sqlite3.connect(store.file_path) as connection:
            connection.execute(
                """
                INSERT INTO metric_samples (
                    observed_at,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    value_integer,
                    value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    disk_identity_key,
                    logical_unit_id,
                    sas_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-20T22:30:00+00:00",
                    "archive-scale",
                    "Archive SCALE",
                    "enc-b",
                    "enc-b",
                    "Rear Shelf",
                    11,
                    "11",
                    "bytes_written",
                    150,
                    None,
                    "sdm",
                    "SERIAL-5",
                    "Drive 5",
                    "healthy",
                    persistent_id,
                    "EUI64",
                    None,
                    "0x5000cca27c7f0005",
                    "0x5000cca27c7f1005",
                ),
            )

        payload = store.get_slot_history_bundle(
            "archive-scale",
            "enc-b",
            11,
            metric_limits={"bytes_written": 10},
        )

        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(
            [sample["value"] for sample in payload["metrics"]["bytes_written"]],
            [200, 150, 100],
        )
        self.assertTrue(payload["disk_history"]["identity_available"])
        self.assertTrue(payload["disk_history"]["followed"])
        self.assertEqual(payload["disk_history"]["prior_home_count"], 1)

    def test_get_slot_history_bundle_uses_requested_window_before_following_older_home(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        persistent_id = "wwn-0x5000cca27c7f0005"
        legacy_record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid=persistent_id,
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="WWN",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        current_record = replace(
            legacy_record,
            system_id="archive-scale",
            system_label="Archive SCALE",
            enclosure_key="enc-b",
            enclosure_id="enc-b",
            enclosure_label="Rear Shelf",
            slot=11,
            slot_label="11",
            device_name="sdm",
        )

        store.upsert_slot_state(legacy_record, "2026-04-10T22:00:00+00:00")
        store.upsert_slot_state(current_record, "2026-04-20T22:00:00+00:00")
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-10T23:00:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="bytes_written",
                    value_integer=100,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid=persistent_id,
                    persistent_id_label="WWN",
                    disk_identity_key=legacy_record.disk_identity_key,
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
                MetricSample(
                    observed_at="2026-04-20T23:00:00+00:00",
                    system_id="archive-scale",
                    system_label="Archive SCALE",
                    enclosure_key="enc-b",
                    enclosure_id="enc-b",
                    enclosure_label="Rear Shelf",
                    slot=11,
                    slot_label="11",
                    metric_name="bytes_written",
                    value_integer=200,
                    value_real=None,
                    device_name="sdm",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid=persistent_id,
                    persistent_id_label="WWN",
                    disk_identity_key=current_record.disk_identity_key,
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                ),
            ]
        )

        payload = store.get_slot_history_bundle(
            "archive-scale",
            "enc-b",
            11,
            metric_limits={"bytes_written": 10},
            since="2026-04-19T00:00:00+00:00",
        )

        self.assertEqual(
            [sample["value"] for sample in payload["metrics"]["bytes_written"]],
            [200],
        )
        self.assertFalse(payload["disk_history"]["followed"])
        self.assertEqual(len(payload["disk_history"]["homes"]), 1)

    def test_get_slot_history_bundle_keeps_slot_zero_as_current_home(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        current_record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="storage-view:boot-doms",
            enclosure_id="storage-view:boot-doms",
            enclosure_label="Boot SATADOMs",
            slot=0,
            slot_label="DOM-A",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="ada0",
            serial="SERIAL-0",
            model="SATADOM",
            gptid="eui.000000000000001000a075012b91c700",
            pool_name="boot",
            vdev_name="mirror-0",
            health="ONLINE",
            persistent_id_label="EUI64",
            logical_unit_id="0x5000cca27c7f0000",
            sas_address="0x5000cca27c7f1000",
        )

        store.upsert_slot_state(current_record, "2026-04-20T22:00:00+00:00")
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-20T23:00:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="storage-view:boot-doms",
                    enclosure_id="storage-view:boot-doms",
                    enclosure_label="Boot SATADOMs",
                    slot=0,
                    slot_label="DOM-A",
                    metric_name="bytes_written",
                    value_integer=200,
                    value_real=None,
                    device_name="ada0",
                    serial="SERIAL-0",
                    model="SATADOM",
                    state="healthy",
                    gptid="eui.000000000000001000a075012b91c700",
                    persistent_id_label="EUI64",
                    disk_identity_key=current_record.disk_identity_key,
                    logical_unit_id="0x5000cca27c7f0000",
                    sas_address="0x5000cca27c7f1000",
                ),
            ]
        )

        payload = store.get_slot_history_bundle(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_limits={"bytes_written": 10},
        )

        self.assertFalse(payload["disk_history"]["followed"])
        self.assertEqual(payload["disk_history"]["prior_home_count"], 0)
        self.assertIsNotNone(payload["disk_history"]["current_home"])
        self.assertEqual(payload["disk_history"]["current_home"]["slot"], 0)

    def test_delete_system_history_removes_only_matching_system_rows(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        archive_record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="GPTID",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        quantastor_record = SlotStateRecord(
            system_id="qs-cryostorage",
            system_label="QS CryoStorage",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN Left",
            slot=0,
            slot_label="DOM-A",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdx",
            serial="QS-DOM-0",
            model="SATADOM",
            gptid="gptid/dom-a",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
        )

        store.upsert_slot_state(archive_record, "2026-04-16T22:05:00+00:00")
        store.upsert_slot_state(quantastor_record, "2026-04-16T22:05:00+00:00")
        store.insert_events(
            build_slot_events(archive_record, replace(archive_record, health="DEGRADED"), "2026-04-16T22:10:00+00:00")
        )
        store.insert_events(
            build_slot_events(
                quantastor_record,
                replace(quantastor_record, health="DEGRADED"),
                "2026-04-16T22:10:00+00:00",
            )
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="qs-cryostorage",
                    system_label="QS CryoStorage",
                    enclosure_key="node-a",
                    enclosure_id="node-a",
                    enclosure_label="QSOSN Left",
                    slot=0,
                    slot_label="DOM-A",
                    metric_name="temperature_c",
                    value_integer=29,
                    value_real=None,
                    device_name="sdx",
                    serial="QS-DOM-0",
                    model="SATADOM",
                    state="healthy",
                ),
            ]
        )

        summary = store.delete_system_history("qs-cryostorage")

        self.assertEqual(summary["removed_system_ids"], ["qs-cryostorage"])
        self.assertEqual(summary["tracked_slots"], 1)
        self.assertEqual(summary["event_count"], 1)
        self.assertEqual(summary["metric_sample_count"], 1)
        self.assertEqual(summary["total_rows"], 3)
        self.assertIsNotNone(store.get_slot_state("archive-core", "enc-a", 5))
        self.assertIsNone(store.get_slot_state("qs-cryostorage", "node-a", 0))
        self.assertEqual(len(store.list_slot_events("archive-core", "enc-a", 5)), 1)
        self.assertEqual(len(store.list_slot_events("qs-cryostorage", "node-a", 0)), 0)
        self.assertEqual(len(store.list_metric_samples("archive-core", "enc-a", 5, metric_name="temperature_c")), 1)
        self.assertEqual(
            len(store.list_metric_samples("qs-cryostorage", "node-a", 0, metric_name="temperature_c")),
            0,
        )

    def test_purge_orphaned_history_removes_missing_system_rows_even_without_current_slot_state(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        archive_record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="GPTID",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
        )
        quantastor_record = SlotStateRecord(
            system_id="qs-cryostorage",
            system_label="QS CryoStorage",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN Left",
            slot=0,
            slot_label="DOM-A",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdx",
            serial="QS-DOM-0",
            model="SATADOM",
            gptid="gptid/dom-a",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
        )
        ghost_record = SlotStateRecord(
            system_id="ghost-system",
            system_label="Ghost System",
            enclosure_key="ghost-enc",
            enclosure_id="ghost-enc",
            enclosure_label="Ghost Shelf",
            slot=7,
            slot_label="07",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdghost",
            serial="GHOST-7",
            model="Ghost Disk",
            gptid="gptid/ghost-7",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
        )

        store.upsert_slot_state(archive_record, "2026-04-16T22:05:00+00:00")
        store.upsert_slot_state(quantastor_record, "2026-04-16T22:05:00+00:00")
        store.insert_events(
            build_slot_events(archive_record, replace(archive_record, health="DEGRADED"), "2026-04-16T22:10:00+00:00")
        )
        store.insert_events(
            build_slot_events(
                quantastor_record,
                replace(quantastor_record, health="DEGRADED"),
                "2026-04-16T22:10:00+00:00",
            )
        )
        store.insert_events(
            build_slot_events(
                ghost_record,
                replace(ghost_record, health="DEGRADED"),
                "2026-04-16T22:10:00+00:00",
            )
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="qs-cryostorage",
                    system_label="QS CryoStorage",
                    enclosure_key="node-a",
                    enclosure_id="node-a",
                    enclosure_label="QSOSN Left",
                    slot=0,
                    slot_label="DOM-A",
                    metric_name="temperature_c",
                    value_integer=29,
                    value_real=None,
                    device_name="sdx",
                    serial="QS-DOM-0",
                    model="SATADOM",
                    state="healthy",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="ghost-system",
                    system_label="Ghost System",
                    enclosure_key="ghost-enc",
                    enclosure_id="ghost-enc",
                    enclosure_label="Ghost Shelf",
                    slot=7,
                    slot_label="07",
                    metric_name="temperature_c",
                    value_integer=40,
                    value_real=None,
                    device_name="sdghost",
                    serial="GHOST-7",
                    model="Ghost Disk",
                    state="healthy",
                ),
            ]
        )

        summary = store.purge_orphaned_history(["archive-core"])
        counts = store.counts()

        self.assertEqual(summary["removed_system_ids"], ["ghost-system", "qs-cryostorage"])
        self.assertEqual(summary["tracked_slots"], 1)
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["metric_sample_count"], 2)
        self.assertEqual(summary["total_rows"], 5)
        self.assertEqual(counts["tracked_slots"], 1)
        self.assertEqual(counts["event_count"], 1)
        self.assertEqual(counts["metric_sample_count"], 1)
        self.assertIsNotNone(store.get_slot_state("archive-core", "enc-a", 5))
        self.assertIsNone(store.get_slot_state("qs-cryostorage", "node-a", 0))
        self.assertEqual(len(store.list_slot_events("ghost-system", "ghost-enc", 7)), 0)
        self.assertEqual(len(store.list_metric_samples("ghost-system", "ghost-enc", 7, metric_name="temperature_c")), 0)

    def test_adopt_system_history_rehomes_removed_rows_into_target_system(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        source_record = SlotStateRecord(
            system_id="qs-cryostorage",
            system_label="QS CryoStorage",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN Left",
            slot=0,
            slot_label="DOM-A",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sda",
            serial="QS-DOM-A",
            model="SATADOM",
            gptid="gptid/dom-a",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
            persistent_id_label="GPTID",
        )
        second_source_record = SlotStateRecord(
            system_id="qs-cryostorage",
            system_label="QS CryoStorage",
            enclosure_key="node-b",
            enclosure_id="node-b",
            enclosure_label="QSOSN Right",
            slot=1,
            slot_label="DOM-B",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sdb",
            serial="QS-DOM-B",
            model="SATADOM",
            gptid="gptid/dom-b",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
            persistent_id_label="GPTID",
        )
        target_record = SlotStateRecord(
            system_id="qsosn-ha",
            system_label="QSOSN HA",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN Left",
            slot=0,
            slot_label="DOM-A",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="sda",
            serial="QS-DOM-A-NEW",
            model="SATADOM",
            gptid="gptid/dom-a-new",
            pool_name=None,
            vdev_name=None,
            health="ONLINE",
            persistent_id_label="GPTID",
        )

        store.upsert_slot_state(source_record, "2026-04-16T22:05:00+00:00")
        store.upsert_slot_state(second_source_record, "2026-04-16T22:06:00+00:00")
        store.upsert_slot_state(target_record, "2026-04-20T22:05:00+00:00")
        store.insert_events(
            build_slot_events(
                source_record,
                replace(source_record, health="DEGRADED"),
                "2026-04-16T22:10:00+00:00",
            )
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="qs-cryostorage",
                    system_label="QS CryoStorage",
                    enclosure_key="node-a",
                    enclosure_id="node-a",
                    enclosure_label="QSOSN Left",
                    slot=0,
                    slot_label="DOM-A",
                    metric_name="bytes_written",
                    value_integer=10,
                    value_real=None,
                    device_name="sda",
                    serial="QS-DOM-A",
                    model="SATADOM",
                    state="healthy",
                    gptid="gptid/dom-a",
                    persistent_id_label="GPTID",
                ),
                MetricSample(
                    observed_at="2026-04-16T22:11:00+00:00",
                    system_id="qs-cryostorage",
                    system_label="QS CryoStorage",
                    enclosure_key="node-b",
                    enclosure_id="node-b",
                    enclosure_label="QSOSN Right",
                    slot=1,
                    slot_label="DOM-B",
                    metric_name="bytes_written",
                    value_integer=20,
                    value_real=None,
                    device_name="sdb",
                    serial="QS-DOM-B",
                    model="SATADOM",
                    state="healthy",
                    gptid="gptid/dom-b",
                    persistent_id_label="GPTID",
                ),
            ]
        )

        summary = store.adopt_system_history(
            "qs-cryostorage",
            "qsosn-ha",
            target_system_label="QSOSN HA",
        )

        self.assertEqual(summary["tracked_slots"], 2)
        self.assertEqual(summary["event_count"], 1)
        self.assertEqual(summary["metric_sample_count"], 2)
        self.assertEqual(summary["slot_state_conflicts"], 1)
        self.assertEqual(summary["total_rows"], 5)
        self.assertIsNone(store.get_slot_state("qs-cryostorage", "node-a", 0))
        self.assertIsNone(store.get_slot_state("qs-cryostorage", "node-b", 1))
        adopted_slot = store.get_slot_state("qsosn-ha", "node-b", 1)
        preserved_slot = store.get_slot_state("qsosn-ha", "node-a", 0)
        self.assertIsNotNone(adopted_slot)
        self.assertEqual(adopted_slot.system_label, "QSOSN HA")
        self.assertIsNotNone(preserved_slot)
        self.assertEqual(preserved_slot.serial, "QS-DOM-A-NEW")
        target_events = store.list_slot_events("qsosn-ha", "node-a", 0)
        target_metrics = store.list_metric_samples("qsosn-ha", "node-a", 0, metric_name="bytes_written")
        self.assertEqual(len(target_events), 1)
        self.assertEqual(target_events[0]["system_label"], "QSOSN HA")
        self.assertEqual(len(target_metrics), 1)
        self.assertEqual(target_metrics[0]["system_label"], "QSOSN HA")
        self.assertEqual(
            [summary["system_id"] for summary in store.list_history_system_summaries(["qsosn-ha"])],
            [],
        )

    def test_store_recovers_from_unreadable_database_file(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        db_path.write_text("not a sqlite database", encoding="utf-8")

        store = HistoryStore(str(db_path))
        counts = store.counts()
        broken_files = list(temp_dir.glob("history.db.broken-*"))

        self.assertEqual(counts["tracked_slots"], 0)
        self.assertEqual(counts["event_count"], 0)
        self.assertEqual(counts["metric_sample_count"], 0)
        self.assertTrue(db_path.exists())
        self.assertEqual(len(broken_files), 1)

    def test_store_creates_rotating_backup_snapshots(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )

        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        first_backup = store.create_backup(
            temp_dir / "backups",
            snapshot_label="2026-04-16T22:05:00+00:00",
            retention_count=2,
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="gptid/5",
                    persistent_id_label="GPTID",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                )
            ]
        )
        second_backup = store.create_backup(
            temp_dir / "backups",
            snapshot_label="2026-04-16T22:10:00+00:00",
            retention_count=2,
        )
        third_backup = store.create_backup(
            temp_dir / "backups",
            snapshot_label="2026-04-16T22:15:00+00:00",
            retention_count=2,
        )

        backup_connection = sqlite3.connect(second_backup)
        try:
            metric_count = backup_connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        finally:
            backup_connection.close()

        remaining_backups = sorted((temp_dir / "backups").glob("history-*.sqlite3"))

        self.assertFalse(first_backup.exists())
        self.assertTrue(second_backup.exists())
        self.assertTrue(third_backup.exists())
        self.assertEqual(metric_count, 1)
        self.assertEqual(len(remaining_backups), 2)

    def test_store_promotes_weekly_and_monthly_long_term_backups(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )

        store.upsert_slot_state(record, "2026-01-05T22:05:00+00:00")
        store.create_backup(
            temp_dir / "backups",
            snapshot_label="2026-01-05T22:05:00+00:00",
            retention_count=8,
            long_term_backup_dir=temp_dir / "long-term",
            weekly_retention_count=4,
            monthly_retention_count=3,
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-01-06T22:10:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="da5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="gptid/5",
                    persistent_id_label="GPTID",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                )
            ]
        )
        store.create_backup(
            temp_dir / "backups",
            snapshot_label="2026-01-06T22:10:00+00:00",
            retention_count=8,
            long_term_backup_dir=temp_dir / "long-term",
            weekly_retention_count=4,
            monthly_retention_count=3,
        )

        current_weekly = temp_dir / "long-term" / "weekly" / "history-weekly-2026-W02.sqlite3"
        current_monthly = temp_dir / "long-term" / "monthly" / "history-monthly-2026-01.sqlite3"
        weekly_connection = sqlite3.connect(current_weekly)
        monthly_connection = sqlite3.connect(current_monthly)
        try:
            weekly_metric_count = weekly_connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
            monthly_metric_count = monthly_connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        finally:
            weekly_connection.close()
            monthly_connection.close()

        self.assertEqual(weekly_metric_count, 1)
        self.assertEqual(monthly_metric_count, 1)

        for snapshot_label in (
            "2026-01-12T22:10:00+00:00",
            "2026-01-19T22:10:00+00:00",
            "2026-01-26T22:10:00+00:00",
            "2026-02-02T22:10:00+00:00",
            "2026-03-02T22:10:00+00:00",
            "2026-04-06T22:10:00+00:00",
        ):
            store.create_backup(
                temp_dir / "backups",
                snapshot_label=snapshot_label,
                retention_count=8,
                long_term_backup_dir=temp_dir / "long-term",
                weekly_retention_count=4,
                monthly_retention_count=3,
            )

        weekly_backups = sorted((temp_dir / "long-term" / "weekly").glob("history-weekly-*.sqlite3"))
        monthly_backups = sorted((temp_dir / "long-term" / "monthly").glob("history-monthly-*.sqlite3"))
        self.assertEqual(
            [path.name for path in weekly_backups],
            [
                "history-weekly-2026-W05.sqlite3",
                "history-weekly-2026-W06.sqlite3",
                "history-weekly-2026-W10.sqlite3",
                "history-weekly-2026-W15.sqlite3",
            ],
        )
        self.assertEqual(
            [path.name for path in monthly_backups],
            [
                "history-monthly-2026-02.sqlite3",
                "history-monthly-2026-03.sqlite3",
                "history-monthly-2026-04.sqlite3",
            ],
        )

    def test_store_migrates_existing_history_tables_before_writes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE slot_state_current (
                    system_id TEXT NOT NULL,
                    system_label TEXT,
                    enclosure_key TEXT NOT NULL,
                    enclosure_id TEXT,
                    enclosure_label TEXT,
                    slot INTEGER NOT NULL,
                    slot_label TEXT NOT NULL,
                    present INTEGER NOT NULL,
                    state TEXT,
                    identify_active INTEGER NOT NULL,
                    device_name TEXT,
                    serial TEXT,
                    model TEXT,
                    gptid TEXT,
                    pool_name TEXT,
                    vdev_name TEXT,
                    health TEXT,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (system_id, enclosure_key, slot)
                );
                CREATE TABLE slot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    system_label TEXT,
                    enclosure_key TEXT NOT NULL,
                    enclosure_id TEXT,
                    enclosure_label TEXT,
                    slot INTEGER NOT NULL,
                    slot_label TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_value TEXT,
                    current_value TEXT,
                    device_name TEXT,
                    serial TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE metric_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    system_id TEXT NOT NULL,
                    system_label TEXT,
                    enclosure_key TEXT NOT NULL,
                    enclosure_id TEXT,
                    enclosure_label TEXT,
                    slot INTEGER NOT NULL,
                    slot_label TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    value_integer INTEGER,
                    value_real REAL,
                    device_name TEXT,
                    serial TEXT,
                    model TEXT,
                    state TEXT
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        store = HistoryStore(str(db_path))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="multipath/disk5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            persistent_id_label="GPTID",
            logical_unit_id="0x5000cca27c7f0005",
            sas_address="0x5000cca27c7f1005",
            topology_label="tank > raidz2-0 > data",
            multipath_device="multipath/disk5",
            multipath_mode="Active/Passive",
            multipath_state="OPTIMAL",
            multipath_lunid="0x5000cca27c7f2229",
            multipath_primary_path="da5",
            multipath_alternate_path="da44",
            multipath_active_paths="da5",
            multipath_passive_paths="da44",
            multipath_active_controllers="mpr0",
            multipath_passive_controllers="mpr1",
        )

        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        store.insert_events(
            build_slot_events(record, replace(record, serial="SERIAL-5B"), "2026-04-16T22:06:00+00:00")
        )
        store.insert_metric_samples(
            [
                MetricSample(
                    observed_at="2026-04-16T22:06:00+00:00",
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_key="enc-a",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    slot=5,
                    slot_label="05",
                    metric_name="temperature_c",
                    value_integer=31,
                    value_real=None,
                    device_name="multipath/disk5",
                    serial="SERIAL-5",
                    model="Drive 5",
                    state="healthy",
                    gptid="gptid/5",
                    persistent_id_label="GPTID",
                    logical_unit_id="0x5000cca27c7f0005",
                    sas_address="0x5000cca27c7f1005",
                )
            ]
        )

        migrated = sqlite3.connect(db_path)
        try:
            state_columns = {
                str(column_name)
                for _, column_name, *_ in migrated.execute("PRAGMA table_info(slot_state_current)").fetchall()
            }
            event_columns = {
                str(column_name)
                for _, column_name, *_ in migrated.execute("PRAGMA table_info(slot_events)").fetchall()
            }
            metric_columns = {
                str(column_name)
                for _, column_name, *_ in migrated.execute("PRAGMA table_info(metric_samples)").fetchall()
            }
        finally:
            migrated.close()

        loaded = store.get_slot_state("archive-core", "enc-a", 5)
        events = store.list_slot_events("archive-core", "enc-a", 5)
        samples = store.list_metric_samples("archive-core", "enc-a", 5, metric_name="temperature_c")

        self.assertIn("persistent_id_label", state_columns)
        self.assertIn("logical_unit_id", state_columns)
        self.assertIn("sas_address", state_columns)
        self.assertIn("gptid", event_columns)
        self.assertIn("persistent_id_label", event_columns)
        self.assertIn("logical_unit_id", event_columns)
        self.assertIn("sas_address", event_columns)
        self.assertIn("gptid", metric_columns)
        self.assertIn("persistent_id_label", metric_columns)
        self.assertIn("logical_unit_id", metric_columns)
        self.assertIn("sas_address", metric_columns)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(loaded.topology_label, "tank > raidz2-0 > data")
        self.assertEqual(loaded.multipath_passive_paths, "da44")
        self.assertEqual(loaded.persistent_id_label, "GPTID")
        self.assertEqual(events[0]["persistent_id_label"], "GPTID")
        self.assertEqual(samples[0]["logical_unit_id"], "0x5000cca27c7f0005")

    def test_backfill_disk_identity_keys_repairs_existing_metric_rows(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))

        with sqlite3.connect(store.file_path) as connection:
            connection.execute(
                """
                INSERT INTO metric_samples (
                    observed_at,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    value_integer,
                    value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    disk_identity_key,
                    logical_unit_id,
                    sas_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-16T22:06:00+00:00",
                    "archive-core",
                    "Archive CORE",
                    "enc-a",
                    "enc-a",
                    "Front Shelf",
                    5,
                    "05",
                    "bytes_written",
                    123,
                    None,
                    "da5",
                    "SERIAL-5",
                    "Drive 5",
                    "healthy",
                    "eui.000000000000001000a075012b91c7cf",
                    "EUI64",
                    None,
                    "0x5000cca27c7f0005",
                    "0x5000cca27c7f1005",
                ),
            )
            HistoryStore._backfill_disk_identity_keys(connection)
            row = connection.execute(
                "SELECT disk_identity_key FROM metric_samples WHERE system_id = 'archive-core' AND enclosure_key = 'enc-a' AND slot = 5"
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(
            row[0],
            "serial-5|eui64|eui.000000000000001000a075012b91c7cf",
        )

    def test_store_retries_write_after_readonly_database_error(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )

        failing_connection = MagicMock()
        failing_connection.execute.side_effect = sqlite3.OperationalError(
            "attempt to write a readonly database"
        )
        working_connection = MagicMock()

        with (
            patch.object(store, "_connect", side_effect=[failing_connection, working_connection]),
            patch.object(store, "_attempt_readonly_database_repair", return_value=True) as repair,
        ):
            store.upsert_slot_state(record, "2026-04-19T09:10:28+00:00")

        repair.assert_called_once()
        working_connection.execute.assert_called_once()
        working_connection.commit.assert_called_once()

    def test_connect_applies_temp_store_and_cache_size_pragmas(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))

        with store._connect() as connection:
            temp_store = connection.execute("PRAGMA temp_store").fetchone()[0]
            cache_size = connection.execute("PRAGMA cache_size").fetchone()[0]

        self.assertEqual(temp_store, 2)
        self.assertEqual(cache_size, -16384)

    def test_connect_falls_back_when_wal_enablement_hits_disk_io_error(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        original_connect = sqlite3.connect

        class FailingWalConnection:
            def __init__(self, path: str) -> None:
                self._connection = original_connect(path)

            def __enter__(self) -> sqlite3.Connection:
                return self._connection.__enter__()

            def __exit__(self, exc_type, exc, tb) -> bool | None:
                return self._connection.__exit__(exc_type, exc, tb)

            def __getattr__(self, name: str):
                return getattr(self._connection, name)

            @property
            def row_factory(self):
                return self._connection.row_factory

            @row_factory.setter
            def row_factory(self, value) -> None:
                self._connection.row_factory = value

            def execute(self, sql: str, *args, **kwargs):
                if sql == "PRAGMA journal_mode=WAL":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._connection.execute(sql, *args, **kwargs)

        with (
            patch("history_service.store.sqlite3.connect", side_effect=lambda path: FailingWalConnection(path)),
            self.assertLogs("history_service.store", level="WARNING") as logs,
        ):
            with store._connect() as connection:
                result = connection.execute("SELECT 1").fetchone()[0]

        self.assertEqual(result, 1)
        self.assertTrue(any("could not enable WAL mode" in message for message in logs.output))

    def test_restore_backup_normalizes_database_permissions(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        store = HistoryStore(str(db_path))
        record = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )
        store.upsert_slot_state(record, "2026-04-16T22:05:00+00:00")
        backup_path = store.create_backup(temp_dir / "backups", retention_count=1)
        self.assertIsNotNone(backup_path)

        db_path.write_text("placeholder", encoding="utf-8")
        wal_path = Path(f"{db_path}-wal")
        shm_path = Path(f"{db_path}-shm")
        wal_path.write_text("wal", encoding="utf-8")
        shm_path.write_text("shm", encoding="utf-8")

        with patch.object(store, "_normalize_database_permissions") as normalize_permissions:
            store.restore_backup(backup_path)

        normalize_permissions.assert_called_once()
        self.assertTrue(db_path.exists())
        self.assertFalse(wal_path.exists())
        self.assertFalse(shm_path.exists())


class HistoryCollectorTests(unittest.TestCase):
    def test_background_startup_collection_is_fast_only(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = MagicMock()
        store.estimated_counts.return_value = {}
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )

        async def run_once(**_: object) -> None:
            collector.last_success_at = isoformat_utc()
            collector._stopping.set()

        collector.run_once = AsyncMock(side_effect=run_once)  # type: ignore[method-assign]

        with patch("history_service.collector.observe_history_collection_run"):
            asyncio.run(collector._run_loop())

        collector.run_once.assert_awaited_once_with(  # type: ignore[attr-defined]
            force_fast=True,
            force_slow=False,
            include_due_intervals=False,
            cached_root_only=True,
            collection_kind="background",
        )

    def test_background_failure_backoff_grows_and_caps(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                failure_backoff_initial_seconds=5,
                failure_backoff_max_seconds=12,
                startup_grace_seconds=0,
            ),
            MagicMock(),
        )
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

        collector._record_background_failure(now)
        self.assertEqual(collector.background_consecutive_failures, 1)
        self.assertEqual(collector.background_backoff_until, now + timedelta(seconds=5))

        collector._record_background_failure(now + timedelta(seconds=5))
        self.assertEqual(collector.background_consecutive_failures, 2)
        self.assertEqual(collector.background_backoff_until, now + timedelta(seconds=15))

        collector._record_background_failure(now + timedelta(seconds=15))
        self.assertEqual(collector.background_consecutive_failures, 3)
        self.assertEqual(collector.background_backoff_until, now + timedelta(seconds=27))
        self.assertEqual(collector.next_collection_at, collector.background_backoff_until)
        self.assertGreater(collector.status()["background_backoff_seconds_remaining"], 0)

    def test_run_once_success_clears_background_failure_backoff(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector._record_background_failure(datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc))
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once())

        self.assertEqual(collector.background_consecutive_failures, 0)
        self.assertIsNone(collector.background_backoff_until)
        self.assertEqual(collector.background_backoff_seconds_remaining, 0)

    def test_record_slot_changes_backfills_extended_state_without_event_noise(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        baseline = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=5,
            slot_label="05",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="multipath/disk5",
            serial="SERIAL-5",
            model="Drive 5",
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )
        upgraded = replace(
            baseline,
            topology_label="tank > raidz2-0 > data",
            multipath_device="multipath/disk5",
            multipath_mode="Active/Passive",
            multipath_state="OPTIMAL",
            multipath_lunid="0x5000cca27c7f2229",
            multipath_primary_path="da5",
            multipath_alternate_path="da44",
            multipath_active_paths="da5",
            multipath_passive_paths="da44",
            multipath_active_controllers="mpr0",
            multipath_passive_controllers="mpr1",
        )

        store.upsert_slot_state(baseline, "2026-04-16T22:05:00+00:00")
        collector._record_slot_changes([upgraded], "2026-04-16T22:10:00+00:00")

        events = store.list_slot_events("archive-core", "enc-a", 5)
        loaded = store.get_slot_state("archive-core", "enc-a", 5)

        self.assertEqual(events, [])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.topology_label, "tank > raidz2-0 > data")
        self.assertEqual(loaded.multipath_state, "OPTIMAL")

    def test_record_slot_changes_preserves_topology_detail_during_degradation(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        baseline = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=30,
            slot_label="30",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="multipath/disk36",
            serial="3FJ0NN6T",
            model="WDC WUH721818AL5204",
            gptid="gptid/8fadc7eb-fe53-11ec-b425-0cc47a8ff400",
            pool_name="The-Repository",
            vdev_name="raidz2-2",
            health="ONLINE",
            topology_label="The-Repository > raidz2-2 > data",
        )
        degraded = replace(
            baseline,
            vdev_name=None,
            topology_label="The-Repository > data",
        )

        store.upsert_slot_state(baseline, "2026-06-12T09:50:00+00:00")
        collector._record_slot_changes([degraded], "2026-06-12T09:54:00+00:00")

        events = store.list_slot_events("archive-core", "enc-a", 30)
        loaded = store.get_slot_state("archive-core", "enc-a", 30)

        self.assertEqual(events, [])
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.vdev_name, "raidz2-2")
        self.assertEqual(loaded.topology_label, "The-Repository > raidz2-2 > data")

    def test_record_slot_changes_confirms_real_topology_change_before_event(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        baseline = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=30,
            slot_label="30",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="multipath/disk36",
            serial="3FJ0NN6T",
            model="WDC WUH721818AL5204",
            gptid="gptid/8fadc7eb-fe53-11ec-b425-0cc47a8ff400",
            pool_name="The-Repository",
            vdev_name="raidz2-2",
            health="ONLINE",
            topology_label="The-Repository > raidz2-2 > data",
        )
        moved = replace(
            baseline,
            vdev_name="raidz2-3",
            topology_label="The-Repository > raidz2-3 > data",
        )

        store.upsert_slot_state(baseline, "2026-06-12T09:50:00+00:00")
        collector._record_slot_changes([moved], "2026-06-12T09:54:00+00:00")

        self.assertEqual(store.list_slot_events("archive-core", "enc-a", 30), [])
        loaded = store.get_slot_state("archive-core", "enc-a", 30)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.vdev_name, "raidz2-2")

        collector._record_slot_changes([moved], "2026-06-12T09:59:00+00:00")

        events = store.list_slot_events("archive-core", "enc-a", 30)
        loaded = store.get_slot_state("archive-core", "enc-a", 30)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "slot_topology_changed")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.vdev_name, "raidz2-3")

    def test_enumerate_scopes_includes_inventory_bound_storage_views(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        root_snapshot = {
            "systems": [
                {
                    "id": "archive-core",
                    "label": "Archive CORE",
                }
            ]
        }
        system_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": True,
                    "message": "TrueNAS API reachable.",
                }
            },
            "enclosures": [],
        }
        storage_views_payload = {
            "system_label": "Archive CORE",
            "views": [
                {
                    "id": "boot-doms",
                    "label": "Boot SATADOMs",
                    "source": "inventory_binding",
                    "backing_enclosure_id": "enc-a",
                    "slots": [
                        {
                            "slot_index": 0,
                            "slot_label": "DOM-A",
                            "occupied": True,
                            "state": "matched",
                            "device_name": "ada0",
                            "serial": "SER-DOM-0",
                            "model": "SATADOM 0",
                            "gptid": "gptid/dom-a",
                            "persistent_id_label": "GPTID",
                            "logical_unit_id": "0x5000c500abcd0000",
                            "sas_address": "0x5000c500abcd0001",
                            "pool_name": "freenas-boot",
                            "health": "ONLINE",
                            "placement_key": "device match: ada0",
                        }
                    ],
                },
                {
                    "id": "primary-chassis",
                    "label": "Primary Chassis",
                    "source": "selected_enclosure_snapshot",
                    "slots": [
                        {
                            "slot_index": 0,
                            "slot_label": "00",
                            "occupied": True,
                            "state": "healthy",
                            "device_name": "da0",
                        }
                    ],
                },
            ],
        }

        collector._fetch_inventory = AsyncMock(side_effect=[root_snapshot, system_snapshot])  # type: ignore[method-assign]
        collector._fetch_storage_views = AsyncMock(return_value=storage_views_payload)  # type: ignore[method-assign]

        scopes = asyncio.run(collector._enumerate_scopes())

        storage_scope = next((scope for scope in scopes if scope.enclosure_id == "storage-view:boot-doms"), None)
        self.assertIsNotNone(storage_scope)
        self.assertNotIn("storage-view:primary-chassis", [scope.enclosure_id for scope in scopes])
        self.assertEqual(storage_scope.snapshot["slots"][0]["slot"], 0)
        self.assertEqual(storage_scope.snapshot["slots"][0]["device_name"], "ada0")
        self.assertEqual(storage_scope.snapshot["slots"][0]["persistent_id_label"], "GPTID")
        self.assertEqual(storage_scope.snapshot["slots"][0]["logical_unit_id"], "0x5000c500abcd0000")
        self.assertEqual(storage_scope.snapshot["slots"][0]["sas_address"], "0x5000c500abcd0001")
        self.assertEqual(storage_scope.snapshot["storage_view_backing_enclosure_id"], "enc-a")
        self.assertEqual(
            collector._fetch_inventory.await_args_list[0].kwargs,  # type: ignore[attr-defined]
            {"force": True},
        )
        self.assertEqual(
            collector._fetch_inventory.await_args_list[1].kwargs,  # type: ignore[attr-defined]
            {"system_id": "archive-core", "force": True},
        )
        self.assertEqual(
            collector._fetch_storage_views.await_args.kwargs,  # type: ignore[attr-defined]
            {"system_id": "archive-core", "force": True},
        )

    def test_enumerate_scopes_can_use_cached_inventory_for_lazy_fast_passes(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        root_snapshot = {
            "systems": [
                {
                    "id": "archive-core",
                    "label": "Archive CORE",
                }
            ]
        }
        system_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": True,
                    "message": "TrueNAS API reachable.",
                }
            },
            "enclosures": [],
        }

        collector._fetch_inventory = AsyncMock(side_effect=[root_snapshot, system_snapshot])  # type: ignore[method-assign]
        collector._fetch_storage_views = AsyncMock(return_value={"system_label": "Archive CORE", "views": []})  # type: ignore[method-assign]

        scopes = asyncio.run(collector._enumerate_scopes(force_inventory=False))

        self.assertEqual(len(scopes), 1)
        self.assertEqual(
            collector._fetch_inventory.await_args_list[0].kwargs,  # type: ignore[attr-defined]
            {"force": False},
        )
        self.assertEqual(
            collector._fetch_inventory.await_args_list[1].kwargs,  # type: ignore[attr-defined]
            {"system_id": "archive-core", "force": False},
        )
        self.assertEqual(
            collector._fetch_storage_views.await_args.kwargs,  # type: ignore[attr-defined]
            {"system_id": "archive-core", "force": False},
        )

    def test_enumerate_scopes_cached_root_only_does_not_walk_all_systems(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        root_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_enclosure_id": "front",
            "selected_enclosure_label": "Front 24 Bay",
            "systems": [
                {"id": "archive-core", "label": "Archive CORE"},
                {"id": "unvr-pro", "label": "UniFi UNVR Pro"},
            ],
            "enclosures": [
                {"id": "front", "label": "Front 24 Bay"},
                {"id": "rear", "label": "Rear 12 Bay"},
            ],
            "slots": [],
        }
        collector._fetch_inventory = AsyncMock(return_value=root_snapshot)  # type: ignore[method-assign]
        collector._fetch_storage_views = AsyncMock(return_value={})  # type: ignore[method-assign]

        scopes = asyncio.run(collector._enumerate_scopes(force_inventory=False, cached_root_only=True))

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].system_id, "archive-core")
        self.assertEqual(scopes[0].enclosure_id, "front")
        collector._fetch_inventory.assert_awaited_once_with(force=False)  # type: ignore[attr-defined]
        collector._fetch_storage_views.assert_not_awaited()  # type: ignore[attr-defined]

    def test_run_once_fast_collection_uses_cached_inventory_and_records_timings(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector.last_fast_metrics_at = collector.started_at
        collector.last_slow_metrics_at = collector.started_at
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once(force_fast=True))

        collector._enumerate_scopes.assert_awaited_once_with(force_inventory=False)  # type: ignore[attr-defined]
        status = collector.status()
        self.assertFalse(status["last_collection_inventory_forced"])
        self.assertIsNotNone(status["last_collection_duration_seconds"])
        self.assertIn(
            "enumerate.scopes",
            [entry["stage"] for entry in status["collection_stage_timings"]],
        )

    def test_manual_fast_collection_ignores_due_slow_interval(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False, cached_root_only=True))

        collector._enumerate_scopes.assert_awaited_once_with(  # type: ignore[attr-defined]
            force_inventory=False,
            cached_root_only=True,
        )
        status = collector.status()
        self.assertFalse(status["last_collection_inventory_forced"])
        self.assertIsNotNone(collector.last_fast_metrics_at)
        self.assertIsNone(collector.last_slow_metrics_at)

    def test_run_once_full_collection_forces_inventory(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector.last_fast_metrics_at = collector.started_at
        collector.last_slow_metrics_at = collector.started_at
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once(force_fast=True, force_slow=True))

        collector._enumerate_scopes.assert_awaited_once_with(force_inventory=True)  # type: ignore[attr-defined]
        self.assertTrue(collector.status()["last_collection_inventory_forced"])

    def test_run_once_records_smart_failure_without_failing_collection(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.create_backup = MagicMock(return_value=None)  # type: ignore[method-assign]
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    snapshot={
                        "selected_system_id": "archive-core",
                        "selected_system_label": "Archive CORE",
                        "selected_enclosure_id": "enc-a",
                        "selected_enclosure_label": "Front Shelf",
                        "slots": [
                            {
                                "slot": 0,
                                "present": True,
                                "serial": "S1",
                                "device_name": "da0",
                                "state": "OK",
                            }
                        ],
                    },
                )
            ]
        )
        collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("POST http://enclosure-ui:8000/api/slots/smart-batch timed out after 45s")
        )

        asyncio.run(collector.run_once(force_slow=True, include_due_intervals=False))

        status = collector.status()
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNotNone(status["last_slow_metrics_at"])
        failed_stage = next(entry for entry in status["collection_stage_timings"] if entry["stage"] == "smart.failed")
        self.assertEqual(failed_stage["system_id"], "archive-core")
        self.assertTrue(failed_stage["force_fresh"])
        self.assertIn("timed out", failed_stage["error"])

    def test_run_once_skips_recent_history_backup_during_slow_collection(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                backup_interval_seconds=3600,
                startup_grace_seconds=0,
            ),
            store,
        )
        recent_backup_at = datetime.now(timezone.utc)
        store.latest_backup_snapshot_at = MagicMock(return_value=recent_backup_at)  # type: ignore[method-assign]
        store.create_backup = MagicMock()  # type: ignore[method-assign]
        collector.last_fast_metrics_at = collector.started_at
        collector.last_slow_metrics_at = collector.started_at
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once(force_slow=True))

        store.create_backup.assert_not_called()  # type: ignore[attr-defined]
        status = collector.status()
        self.assertEqual(status["last_backup_at"], isoformat_utc(recent_backup_at))
        self.assertIn(
            "db.backup.skipped",
            [entry["stage"] for entry in status["collection_stage_timings"]],
        )

    def test_fetch_inventory_omits_force_param_when_cached_inventory_requested(self) -> None:
        collector = HistoryCollector(HistorySettings(source_base_url="http://enclosure-ui:8000"), MagicMock())
        collector._fetch_json = AsyncMock(return_value={})  # type: ignore[method-assign]

        asyncio.run(collector._fetch_inventory(system_id="scale-a", enclosure_id="front", force=False))

        collector._fetch_json.assert_awaited_once_with(  # type: ignore[attr-defined]
            "/api/inventory",
            params={
                "system_id": "scale-a",
                "enclosure_id": "front",
            },
        )

    def test_enumerate_scopes_skips_timed_out_saved_system(self) -> None:
        collector = HistoryCollector(HistorySettings(source_base_url="http://enclosure-ui:8000"), MagicMock())
        root_snapshot = {
            "systems": [
                {"id": "slow-system", "label": "Slow System"},
                {"id": "archive-core", "label": "Archive CORE"},
            ],
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
        }
        archive_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_enclosure_id": "enc-a",
            "selected_enclosure_label": "Front Shelf",
            "enclosures": [{"id": "enc-a", "label": "Front Shelf"}],
            "slots": [{"slot": 0, "present": True, "serial": "S1"}],
        }

        async def fetch_inventory(
            system_id: str | None = None,
            enclosure_id: str | None = None,
            *,
            force: bool = True,
        ) -> dict[str, object]:
            if system_id is None:
                return root_snapshot
            if system_id == "slow-system":
                raise RuntimeError("GET http://enclosure-ui:8000/api/inventory?force=true timed out after 45s")
            self.assertEqual(system_id, "archive-core")
            self.assertIsNone(enclosure_id)
            self.assertTrue(force)
            return archive_snapshot

        collector._fetch_inventory = fetch_inventory  # type: ignore[method-assign]
        collector._enumerate_storage_view_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        scopes = asyncio.run(collector._enumerate_scopes(force_inventory=True))

        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0].system_id, "archive-core")
        stages = collector.current_collection_stage_timings
        self.assertIn("inventory.system_failed", [entry["stage"] for entry in stages])
        failed_stage = next(entry for entry in stages if entry["stage"] == "inventory.system_failed")
        self.assertEqual(failed_stage["system_id"], "slow-system")
        self.assertIn("timed out", failed_stage["error"])

    def test_run_once_records_inventory_bound_storage_view_metrics(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        storage_view_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "selected_enclosure_id": "storage-view:boot-doms",
            "selected_enclosure_label": "Boot SATADOMs",
            "storage_view_id": "boot-doms",
            "storage_view_backing_enclosure_id": "enc-a",
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": True,
                    "message": "TrueNAS API reachable.",
                }
            },
            "slots": [
                {
                    "slot": 0,
                    "slot_label": "DOM-A",
                    "enclosure_id": "storage-view:boot-doms",
                    "enclosure_label": "Boot SATADOMs",
                    "present": True,
                    "state": "matched",
                    "identify_active": False,
                    "device_name": "ada0",
                    "serial": "SER-DOM-0",
                    "model": "SATADOM 0",
                    "gptid": "gptid/dom-a",
                    "persistent_id_label": "GPTID",
                    "logical_unit_id": "0x5000c500abcd0000",
                    "sas_address": "0x5000c500abcd0001",
                    "pool_name": "freenas-boot",
                    "health": "ONLINE",
                    "topology_label": "device match: ada0",
                }
            ],
        }

        async def enumerate_scopes(*, force_inventory: bool = True) -> list[ScopeSnapshot]:
            return [
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="storage-view:boot-doms",
                    enclosure_label="Boot SATADOMs",
                    snapshot=storage_view_snapshot,
                )
            ]

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]
        collector._fetch_json = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "available": True,
                "temperature_c": 31,
                "bytes_read": 100,
                "bytes_written": 200,
                "annualized_bytes_read": 25,
                "annualized_bytes_written": None,
                "power_on_hours": 48,
            }
        )

        asyncio.run(collector.run_once())

        temperature_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="temperature_c",
        )
        read_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="bytes_read",
        )
        annualized_read_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="annualized_bytes_read",
        )
        loaded = store.get_slot_state("archive-core", "storage-view:boot-doms", 0)

        self.assertEqual(len(temperature_samples), 1)
        self.assertEqual(len(read_samples), 1)
        self.assertEqual(len(annualized_read_samples), 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(temperature_samples[0]["persistent_id_label"], "GPTID")
        self.assertEqual(temperature_samples[0]["logical_unit_id"], "0x5000c500abcd0000")
        self.assertEqual(temperature_samples[0]["sas_address"], "0x5000c500abcd0001")
        self.assertEqual(read_samples[0]["gptid"], "gptid/dom-a")
        self.assertEqual(annualized_read_samples[0]["value"], 25)
        self.assertEqual(loaded.persistent_id_label, "GPTID")
        self.assertEqual(loaded.logical_unit_id, "0x5000c500abcd0000")
        self.assertEqual(loaded.sas_address, "0x5000c500abcd0001")
        self.assertEqual(
            collector._fetch_json.await_args_list[0].args[0],  # type: ignore[attr-defined]
            "/api/storage-views/boot-doms/slots/0/smart",
        )
        self.assertEqual(
            collector._fetch_json.await_args_list[0].kwargs["params"],  # type: ignore[attr-defined]
            {
                "system_id": "archive-core",
                "enclosure_id": "enc-a",
                "fresh": "true",
            },
        )

    def test_fetch_smart_summaries_uses_fresh_batch_params_for_live_slots(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        scope = ScopeSnapshot(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            snapshot={
                "selected_system_id": "archive-core",
                "selected_system_label": "Archive CORE",
                "selected_enclosure_id": "enc-a",
                "selected_enclosure_label": "Front Shelf",
                "slots": [],
            },
        )
        collector._fetch_json = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "summaries": [
                    {"slot": 5, "summary": {"available": True, "bytes_written": 1234}},
                    {"slot": 6, "summary": {"available": True, "bytes_written": 5678}},
                ]
            }
        )

        summaries = asyncio.run(collector._fetch_smart_summaries(scope, [5, 6], force_fresh=True))

        self.assertEqual(sorted(summaries), [5, 6])
        self.assertEqual(
            collector._fetch_json.await_args.kwargs["params"],  # type: ignore[attr-defined]
            {
                "system_id": "archive-core",
                "enclosure_id": "enc-a",
                "fresh": "true",
            },
        )
        self.assertEqual(
            collector._fetch_json.await_args.kwargs["method"],  # type: ignore[attr-defined]
            "POST",
        )
        self.assertIsNone(collector._fetch_json.await_args.kwargs["timeout_seconds"])  # type: ignore[attr-defined]

    def test_fetch_smart_summaries_uses_short_timeout_for_cached_batch(self) -> None:
        collector = HistoryCollector(
            HistorySettings(source_base_url="http://enclosure-ui:8000", request_timeout_seconds=45),
            MagicMock(),
        )
        scope = ScopeSnapshot(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            snapshot={
                "selected_system_id": "archive-core",
                "selected_system_label": "Archive CORE",
                "selected_enclosure_id": "enc-a",
                "selected_enclosure_label": "Front Shelf",
                "slots": [],
            },
        )
        collector._fetch_json = AsyncMock(return_value={"summaries": []})  # type: ignore[method-assign]

        asyncio.run(collector._fetch_smart_summaries(scope, [5, 6], force_fresh=False))

        self.assertEqual(
            collector._fetch_json.await_args.kwargs["params"],  # type: ignore[attr-defined]
            {
                "system_id": "archive-core",
                "enclosure_id": "enc-a",
            },
        )
        self.assertEqual(collector._fetch_json.await_args.kwargs["timeout_seconds"], 5)  # type: ignore[attr-defined]

    def test_run_once_force_slow_recollects_even_when_slow_interval_not_due(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        collector.last_fast_metrics_at = collector.started_at
        collector.last_slow_metrics_at = collector.started_at

        storage_view_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "selected_enclosure_id": "storage-view:boot-doms",
            "selected_enclosure_label": "Boot SATADOMs",
            "storage_view_id": "boot-doms",
            "storage_view_backing_enclosure_id": "enc-a",
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": True,
                    "message": "TrueNAS API reachable.",
                }
            },
            "slots": [
                {
                    "slot": 0,
                    "slot_label": "DOM-A",
                    "enclosure_id": "storage-view:boot-doms",
                    "enclosure_label": "Boot SATADOMs",
                    "present": True,
                    "state": "matched",
                    "identify_active": False,
                    "device_name": "ada0",
                    "serial": "SER-DOM-0",
                    "model": "SATADOM 0",
                    "gptid": "gptid/dom-a",
                    "persistent_id_label": "GPTID",
                    "logical_unit_id": "0x5000c500abcd0000",
                    "sas_address": "0x5000c500abcd0001",
                    "pool_name": "freenas-boot",
                    "health": "ONLINE",
                    "topology_label": "device match: ada0",
                }
            ],
        }

        async def enumerate_scopes(*, force_inventory: bool = True) -> list[ScopeSnapshot]:
            return [
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="storage-view:boot-doms",
                    enclosure_label="Boot SATADOMs",
                    snapshot=storage_view_snapshot,
                )
            ]

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]
        collector._fetch_json = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "available": True,
                "temperature_c": 31,
                "bytes_read": 100,
                "bytes_written": 200,
                "annualized_bytes_read": 25,
                "annualized_bytes_written": 50,
                "power_on_hours": 48,
            }
        )

        asyncio.run(collector.run_once(force_slow=True))

        temperature_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="temperature_c",
        )
        read_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="bytes_read",
        )
        annualized_read_samples = store.list_metric_samples(
            "archive-core",
            "storage-view:boot-doms",
            0,
            metric_name="annualized_bytes_read",
        )

        self.assertEqual(len(temperature_samples), 0)
        self.assertEqual(len(read_samples), 1)
        self.assertEqual(len(annualized_read_samples), 1)
        self.assertEqual(
            collector._fetch_json.await_args_list[0].kwargs["params"],  # type: ignore[attr-defined]
            {
                "system_id": "archive-core",
                "enclosure_id": "enc-a",
                "fresh": "true",
            },
        )

    def test_run_once_skips_degraded_api_snapshot_without_event_noise(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        baseline = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=30,
            slot_label="30",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="multipath/disk36",
            serial="3FJ0NN6T",
            model="WDC WUH721818AL5204",
            gptid="gptid/8fadc7eb-fe53-11ec-b425-0cc47a8ff400",
            pool_name="The-Repository",
            vdev_name="raidz2-2",
            health="ONLINE",
            topology_label="The-Repository > raidz2-2 > data",
            multipath_device="multipath/disk36",
            multipath_mode="Active/Active",
            multipath_state="DEGRADED",
            multipath_lunid="5000cca2c271f220",
            multipath_primary_path="da71",
            multipath_alternate_path="da24",
            multipath_active_paths="da71",
            multipath_failed_paths="da24",
            multipath_active_controllers="mpr1",
            multipath_failed_controllers="mpr0",
        )
        degraded_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": False,
                    "message": "timed out during opening handshake",
                },
                "ssh": {
                    "enabled": True,
                    "ok": True,
                    "message": "SSH probe completed.",
                },
            },
            "slots": [
                {
                    "slot": 30,
                    "slot_label": "30",
                    "enclosure_id": "enc-a",
                    "enclosure_label": "Front Shelf",
                    "present": True,
                    "state": "unknown",
                    "identify_active": False,
                    "device_name": "da24",
                    "serial": "3FJ0NN6T",
                    "model": "WDC WUH721818AL5204",
                    "gptid": None,
                    "pool_name": None,
                    "vdev_name": None,
                    "health": "OK (0x01 0x00 0x00 0x00)",
                }
            ],
        }

        store.upsert_slot_state(baseline, "2026-04-17T05:52:26+00:00")

        async def enumerate_scopes(*, force_inventory: bool = True) -> list[ScopeSnapshot]:
            return [
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    snapshot=degraded_snapshot,
                )
            ]

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]

        asyncio.run(collector.run_once())

        events = store.list_slot_events("archive-core", "enc-a", 30)
        loaded = store.get_slot_state("archive-core", "enc-a", 30)

        self.assertEqual(events, [])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.device_name, "multipath/disk36")
        self.assertEqual(loaded.gptid, "gptid/8fadc7eb-fe53-11ec-b425-0cc47a8ff400")
        self.assertEqual(loaded.pool_name, "The-Repository")
        self.assertEqual(loaded.multipath_state, "DEGRADED")

    def test_run_once_skips_quantastor_snapshot_with_incomplete_topology(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            store,
        )
        baseline = SlotStateRecord(
            system_id="qs-cryostorage",
            system_label="QS CryoStorage",
            enclosure_key="node-a",
            enclosure_id="node-a",
            enclosure_label="QSOSN-Right",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="disk/by-id/scsi-SAMSUNG_MZILT3T8HALS0D3_S40BNF0M603885",
            serial="S40BNF0M603885",
            model="SAMSUNG MZILT3T8HALS0D3",
            gptid="scsi-SAMSUNG_MZILT3T8HALS0D3_S40BNF0M603885",
            pool_name="HA-Pool-R10",
            vdev_name="mirror-0",
            health="ONLINE",
            topology_label="HA-Pool-R10 > mirror-0 > data (Active on QSOSN-Right)",
        )
        incomplete_snapshot = {
            "selected_system_id": "qs-cryostorage",
            "selected_system_label": "QS CryoStorage",
            "selected_system_platform": "quantastor",
            "platform_context": {
                "topology_complete": False,
            },
            "sources": {
                "api": {
                    "enabled": True,
                    "ok": True,
                    "message": "Quantastor API reachable.",
                },
                "ssh": {
                    "enabled": True,
                    "ok": True,
                    "message": "SSH probe completed.",
                },
            },
            "slots": [
                {
                    "slot": 0,
                    "slot_label": "00",
                    "enclosure_id": "node-a",
                    "enclosure_label": "QSOSN-Right",
                    "present": True,
                    "state": "healthy",
                    "identify_active": False,
                    "device_name": "disk/by-id/scsi-SAMSUNG_MZILT3T8HALS0D3_S40BNF0M603885",
                    "serial": "S40BNF0M603885",
                    "model": "SAMSUNG MZILT3T8HALS0D3",
                    "gptid": "scsi-SAMSUNG_MZILT3T8HALS0D3_S40BNF0M603885",
                    "pool_name": "HA-Pool-R10",
                    "vdev_name": "disk",
                    "health": "ONLINE",
                    "topology_label": "HA-Pool-R10 > disk > data (Active on QSOSN-Right)",
                }
            ],
        }

        store.upsert_slot_state(baseline, "2026-04-17T03:20:00+00:00")

        async def enumerate_scopes(*, force_inventory: bool = True) -> list[ScopeSnapshot]:
            return [
                ScopeSnapshot(
                    system_id="qs-cryostorage",
                    system_label="QS CryoStorage",
                    enclosure_id="node-a",
                    enclosure_label="QSOSN-Right",
                    snapshot=incomplete_snapshot,
                )
            ]

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]

        asyncio.run(collector.run_once())

        events = store.list_slot_events("qs-cryostorage", "node-a", 0)
        loaded = store.get_slot_state("qs-cryostorage", "node-a", 0)

        self.assertEqual(events, [])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.vdev_name, "mirror-0")
        self.assertEqual(loaded.topology_label, "HA-Pool-R10 > mirror-0 > data (Active on QSOSN-Right)")
