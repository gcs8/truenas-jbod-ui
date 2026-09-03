from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from starlette.requests import Request

from app.request_context import request_context
from history_service import main as history_main
from history_service import migration_lock
from history_service import store as history_store
from history_service.collector import HistoryCollectionStopping, HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings, get_history_settings
from history_service.domain import MetricSample, SlotStateRecord, build_slot_events, isoformat_utc
from history_service.migration_lock import history_lock_path, history_write_lock
from history_service.segment_catalog import MIGRATION_PENDING_MARKER, activation_pending_path
from history_service.store import DISK_IDENTITY_BACKFILL_USER_VERSION, HistoryStore, SlotStateUpdate


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
    def tearDown(self) -> None:
        get_history_settings.cache_clear()

    def test_permission_repair_is_disabled_with_group_scoped_modes_by_default(self) -> None:
        settings = HistorySettings()

        self.assertIs(getattr(settings, "permission_repair_enabled", None), False)
        self.assertEqual(getattr(settings, "shared_dir_mode", None), 0o770)
        self.assertEqual(getattr(settings, "shared_file_mode", None), 0o660)

    def test_blank_segment_catalog_environment_keeps_hot_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "HISTORY_SQLITE_PATH": str(Path(temp_dir) / "history.db"),
                    "HISTORY_SEGMENT_CATALOG_PATH": "",
                },
            ):
                get_history_settings.cache_clear()
                settings = get_history_settings()

        self.assertIsNone(settings.segment_catalog_path)

    def test_permission_repair_settings_reject_world_writable_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "world-writable"):
            HistorySettings(shared_dir_mode=0o777)
        with self.assertRaisesRegex(ValueError, "world-writable"):
            HistorySettings(shared_file_mode=0o666)

    def test_permission_repair_environment_modes_parse_as_octal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "HISTORY_SQLITE_PATH": str(Path(temp_dir) / "history.db"),
                    "HISTORY_PERMISSION_REPAIR_ENABLED": "true",
                    "HISTORY_SHARED_DIR_MODE": "0750",
                    "HISTORY_SHARED_FILE_MODE": "0640",
                },
            ):
                get_history_settings.cache_clear()
                settings = get_history_settings()

        self.assertTrue(settings.permission_repair_enabled)
        self.assertEqual(settings.shared_dir_mode, 0o750)
        self.assertEqual(settings.shared_file_mode, 0o640)

    def test_history_store_factory_forwards_permission_settings(self) -> None:
        settings = HistorySettings(
            sqlite_path="/tmp/history-policy-test.db",
            segment_catalog_path="/tmp/history-segments/catalog.json",
            permission_repair_enabled=True,
            shared_dir_mode=0o750,
            shared_file_mode=0o640,
        )
        factory = getattr(history_main, "build_history_store", None)
        if not callable(factory):
            self.fail("history_service.main does not expose build_history_store")

        sentinel = object()
        with patch.object(history_main, "HistoryStore", return_value=sentinel) as constructor:
            result = factory(settings)

        self.assertIs(result, sentinel)
        constructor.assert_called_once_with(
            settings.sqlite_path,
            segment_catalog_path="/tmp/history-segments/catalog.json",
            permission_repair_enabled=True,
            shared_dir_mode=0o750,
            shared_file_mode=0o640,
        )

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

    def test_history_settings_use_conservative_retention_defaults(self) -> None:
        settings = HistorySettings()

        self.assertEqual(settings.raw_metric_retention_days, 30)
        self.assertEqual(settings.event_retention_days, 365)
        self.assertEqual(settings.hourly_rollup_retention_days, 365)
        self.assertEqual(settings.daily_rollup_retention_days, 1825)
        self.assertEqual(settings.retention_interval_seconds, 3600)
        self.assertEqual(settings.retention_batch_size, 5000)
        self.assertEqual(settings.retention_max_batches_per_run, 20)

    def test_history_settings_allow_keep_forever_retention_values(self) -> None:
        settings = HistorySettings(
            raw_metric_retention_days=0,
            event_retention_days=0,
            hourly_rollup_retention_days=0,
            daily_rollup_retention_days=0,
        )

        self.assertEqual(settings.raw_metric_retention_days, 0)
        self.assertEqual(settings.event_retention_days, 0)
        self.assertEqual(settings.hourly_rollup_retention_days, 0)
        self.assertEqual(settings.daily_rollup_retention_days, 0)

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
    @staticmethod
    def _request(*, root_path: str = "") -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "root_path": root_path,
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": history_main.app,
            }
        )

    @staticmethod
    def _render_dashboard(
        status: dict[str, object],
        counts: dict[str, object],
        scopes: list[dict[str, object]],
        *,
        database_size_bytes: int = 0,
        release_status: dict[str, object] | None = None,
        root_path: str = "",
    ) -> str:
        route = next(route for route in history_main.app.routes if route.path == "/")
        with (
            patch.object(history_main.collector, "status", return_value=status),
            patch.object(history_main.store, "estimated_counts", return_value=counts),
            patch.object(history_main.store, "list_scopes", return_value=scopes),
            patch.object(history_main.store, "database_size_bytes", return_value=database_size_bytes),
            patch.object(
                history_main.get_release_status_service(),
                "snapshot",
                return_value=release_status or {},
            ),
        ):
            response = asyncio.run(
                route.endpoint(
                    request=HistoryDashboardRouteTests._request(root_path=root_path),
                    exact_counts=False,
                )
            )
        return response.body.decode("utf-8")

    def test_dashboard_uses_template_and_gated_static_assets(self) -> None:
        service_dir = Path(history_main.__file__).resolve().parent
        template_path = service_dir / "templates" / "dashboard.html"
        stylesheet_path = service_dir / "static" / "dashboard.css"
        script_path = service_dir / "static" / "dashboard.js"

        self.assertTrue(template_path.is_file(), "history dashboard template must be extracted from main.py")
        self.assertTrue(stylesheet_path.is_file(), "history dashboard styles must be a static asset")
        self.assertTrue(script_path.is_file(), "history dashboard JavaScript must be a static asset")
        self.assertNotIn("<!doctype html>", Path(history_main.__file__).read_text(encoding="utf-8").lower())
        self.assertIn("script_json_text", history_main.templates.env.filters)
        self.assertTrue(any(route.path == "/static" for route in history_main.app.routes))

        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("dashboard.css", template_source)
        self.assertIn("dashboard.js", template_source)
        self.assertNotIn("<style", template_source.lower())
        self.assertNotRegex(template_source, r"_json\s*\|\s*safe")
        script_tags = re.findall(r"<script\b([^>]*)>", template_source, flags=re.IGNORECASE)
        self.assertTrue(script_tags)
        self.assertTrue(
            all("src=" in attributes or 'type="application/json"' in attributes for attributes in script_tags),
            "the history template must not contain executable inline JavaScript",
        )

        workflow = (service_dir.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("node --check history_service/static/dashboard.js", workflow)

    def test_dashboard_renders_fast_and_full_refresh_controls(self) -> None:
        markup = self._render_dashboard(
            {"collector_running": True},
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            release_status={"summary": "dev build"},
        )
        script_source = (Path(history_main.__file__).resolve().parent / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="history-refresh-fast"', markup)
        self.assertIn('id="history-refresh-full"', markup)
        self.assertIn('src="http://testserver/static/dashboard.js"', markup)
        self.assertIn('href="http://testserver/static/dashboard.css"', markup)
        self.assertIn("/api/history/refresh?mode=", script_source)
        self.assertIn("const body = await response.text();", script_source)
        self.assertIn("JSON.parse(body)", script_source)
        self.assertIn("Next background pass", markup)
        self.assertIn("Background backoff", markup)
        self.assertIn("Last collection duration", markup)
        self.assertIn("Last schedule overrun", markup)
        self.assertIn('id="status-last-background-overrun"', markup)
        self.assertIn("Last retention pass", markup)
        self.assertIn('id="status-last-retention-at"', markup)
        self.assertIn("Last retention rows removed", markup)
        self.assertIn('id="status-last-retention-rows-removed"', markup)
        self.assertIn("Last retention failure", markup)
        self.assertIn('id="status-last-retention-error"', markup)
        self.assertIn("Last collection inventory", markup)
        self.assertIn("DB Size", markup)
        self.assertIn("collector-activity-banner", markup)
        self.assertIn("pollCollectorStatus", script_source)
        self.assertIn("pollOverviewStatus", script_source)
        self.assertIn("__HISTORY_DASHBOARD_POLL", script_source)
        self.assertIn('id="status-current-collection"', markup)
        self.assertIn('id="collector-state-value"', markup)
        self.assertIn('id="tracked-scopes-body"', markup)

    def test_dashboard_omits_release_link_for_non_http_urls(self) -> None:
        markup = self._render_dashboard(
            {"collector_running": True},
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            release_status={"summary": "latest release", "latest_url": "javascript:alert(1)"},
        )

        self.assertIn("latest release", markup)
        self.assertNotIn("javascript:alert", markup)
        self.assertNotIn('class="note-link"', markup)

    def test_dashboard_renders_collection_activity_banner_state(self) -> None:
        markup = self._render_dashboard(
            {
                "collector_running": True,
                "collection_running": True,
                "collection_kind": "background",
                "collection_activity": "collecting SMART metrics for Archive CORE / Front Shelf (1/2)",
                "collection_elapsed_seconds": 42,
            },
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            database_size_bytes=1536,
        )
        script_source = (Path(history_main.__file__).resolve().parent / "static" / "dashboard.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("collecting SMART metrics", markup)
        self.assertIn("1.5 KiB", markup)
        self.assertRegex(
            markup,
            r'id="status-current-collection">\s*background for 42s: collecting SMART metrics for Archive CORE / Front Shelf \(1/2\)',
        )
        self.assertRegex(
            markup,
            r'id="collector-activity-banner"[^>]*>\s*History background collection running for 42s: '
            r'collecting SMART metrics for Archive CORE / Front Shelf \(1/2\)\.',
        )
        self.assertIn("History ${kind} collection running", script_source)
        self.assertIn("renderCollectorStatus(payload)", script_source)
        self.assertNotIn("renderOverview(initialOverviewPayload)", script_source)

    def test_dashboard_server_renders_backoff_banner_without_javascript(self) -> None:
        markup = self._render_dashboard(
            {
                "collector_running": True,
                "collection_running": False,
                "background_backoff_seconds_remaining": 125,
            },
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
        )

        self.assertRegex(
            markup,
            r'id="collector-activity-banner"[^>]*>\s*History background collection is backed off for 2m 5s '
            r'after repeated failures\.',
        )
        self.assertRegex(markup, r'id="status-current-collection">\s*not running')

    def test_dashboard_bootstrap_is_script_safe_and_round_trips(self) -> None:
        hostile_text = "</script><script>alert('&')</script>" + chr(0x2028) + chr(0x2029)
        status = {
            "collector_running": True,
            "source_base_url": hostile_text,
            "collection_activity": hostile_text,
        }
        counts = {
            "tracked_slots": 1,
            "event_count": 2,
            "metric_sample_count": 3,
            "estimated": True,
        }
        scopes = [
            {
                "system_label": hostile_text,
                "enclosure_label": "Synthetic Shelf",
                "tracked_slots": 1,
                "event_count": 2,
                "metric_sample_count": 3,
                "last_seen_at": "2026-09-02T12:00:00+00:00",
            }
        ]

        markup = self._render_dashboard(status, counts, scopes, database_size_bytes=1024)

        match = re.search(
            r'<script id="history-dashboard-bootstrap" type="application/json">\s*(.*?)\s*</script>',
            markup,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        bootstrap_text = match.group(1)
        self.assertNotIn("<", bootstrap_text)
        self.assertNotIn(chr(0x2028), bootstrap_text)
        self.assertNotIn(chr(0x2029), bootstrap_text)
        self.assertEqual(
            json.loads(bootstrap_text),
            status,
        )
        self.assertNotIn(hostile_text, markup)
        self.assertIn("&lt;/script&gt;&lt;script&gt;alert", markup)

    def test_dashboard_static_urls_preserve_root_path(self) -> None:
        markup = self._render_dashboard(
            {"collector_running": True},
            {"tracked_slots": 0, "event_count": 0, "metric_sample_count": 0},
            [],
            root_path="/history",
        )

        self.assertIn('href="http://testserver/history/static/dashboard.css"', markup)
        self.assertIn('src="http://testserver/history/static/dashboard.js"', markup)

    def test_overview_marks_trigger_tracked_counts_as_exact(self) -> None:
        with (
            patch.object(history_main.collector, "status", return_value={}),
            patch.object(
                history_main.store,
                "estimated_counts",
                return_value={"tracked_slots": 1, "estimated": False, "count_mode": "tracked"},
            ),
            patch.object(history_main.store, "list_scopes", return_value=[]),
            patch.object(history_main.store, "database_size_bytes", return_value=0),
        ):
            payload = asyncio.run(history_main.overview(exact_counts=False))

        self.assertTrue(payload["counts_exact"])

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
        self.assertEqual(payload["detail"], "History full refresh failed; see service logs.")
        self.assertNotIn("timed out after 45s", json.dumps(payload))
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

    def test_history_read_routes_run_store_calls_off_the_event_loop(self) -> None:
        route_cases = (
            ("/", {"exact_counts": False}),
            ("/", {"exact_counts": True}),
            ("/healthz", {}),
            ("/api/history/overview", {"exact_counts": False}),
            ("/api/history/overview", {"exact_counts": True}),
            (
                "/api/history/slots/{slot}/events",
                {"slot": 5, "system_id": "archive-core", "enclosure_id": "front", "limit": 100},
            ),
            (
                "/api/history/slots/{slot}/metrics",
                {
                    "slot": 5,
                    "system_id": "archive-core",
                    "enclosure_id": "front",
                    "metric_name": "temperature_c",
                    "since": None,
                    "limit": 500,
                },
            ),
            (
                "/api/history/slots/{slot}/bundle",
                {
                    "slot": 5,
                    "system_id": "archive-core",
                    "enclosure_id": "front",
                    "since": None,
                    "event_limit": 12,
                },
            ),
            (
                "/api/history/scopes/slots",
                {
                    "system_id": "archive-core",
                    "enclosure_id": "front",
                    "slots": [5],
                    "metrics": ["temperature_c"],
                    "since": None,
                    "event_limit": 12,
                },
            ),
        )
        store_results = {
            "counts": {"tracked_slots": 0},
            "estimated_counts": {"tracked_slots": 0},
            "list_scopes": [],
            "database_size_bytes": 0,
            "list_slot_events": [],
            "list_metric_samples": [],
            "get_slot_history_bundle": {"events": [], "metrics": {}},
            "list_scope_history": {},
        }

        for route_path, route_kwargs in route_cases:
            with self.subTest(route=route_path, exact_counts=route_kwargs.get("exact_counts")):
                event_loop_thread_id = threading.get_ident()
                store_thread_ids: list[int] = []

                def tracked_result(result):
                    def call(*_args, **_kwargs):
                        store_thread_ids.append(threading.get_ident())
                        return result

                    return call

                route = next(
                    route
                    for route in history_main.app.routes
                    if getattr(route, "path", None) == route_path
                )
                with ExitStack() as stack:
                    for method_name, result in store_results.items():
                        stack.enter_context(
                            patch.object(history_main.store, method_name, side_effect=tracked_result(result))
                        )
                    if route_path == "/":
                        route_kwargs = {"request": self._request(), **route_kwargs}
                    asyncio.run(getattr(route, "endpoint")(**route_kwargs))

                self.assertTrue(store_thread_ids)
                self.assertTrue(
                    all(thread_id != event_loop_thread_id for thread_id in store_thread_ids),
                    f"{route_path} executed a HistoryStore read on the event-loop thread",
                )

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

    def test_history_collector_propagates_current_server_request_id(self) -> None:
        collector = HistoryCollector(
            HistorySettings(source_base_url="http://enclosure-ui:8000", request_timeout_seconds=7),
            MagicMock(),
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'

        with (
            request_context("d" * 32),
            patch("history_service.collector.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            payload = collector._fetch_json_sync("/healthz", {}, "GET", None, {})

        request = urlopen.call_args.args[0]
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(request.get_header("X-request-id"), "d" * 32)


class HistoryStoreTests(unittest.TestCase):
    def test_slot_state_column_contract_matches_existing_schema_and_projections(self) -> None:
        expected_columns = (
            ("system_id", "TEXT NOT NULL"),
            ("system_label", "TEXT"),
            ("enclosure_key", "TEXT NOT NULL"),
            ("enclosure_id", "TEXT"),
            ("enclosure_label", "TEXT"),
            ("slot", "INTEGER NOT NULL"),
            ("slot_label", "TEXT NOT NULL"),
            ("present", "INTEGER NOT NULL"),
            ("state", "TEXT"),
            ("identify_active", "INTEGER NOT NULL"),
            ("device_name", "TEXT"),
            ("serial", "TEXT"),
            ("model", "TEXT"),
            ("gptid", "TEXT"),
            ("persistent_id_label", "TEXT"),
            ("disk_identity_key", "TEXT"),
            ("logical_unit_id", "TEXT"),
            ("sas_address", "TEXT"),
            ("pool_name", "TEXT"),
            ("vdev_name", "TEXT"),
            ("health", "TEXT"),
            ("topology_label", "TEXT"),
            ("multipath_device", "TEXT"),
            ("multipath_mode", "TEXT"),
            ("multipath_state", "TEXT"),
            ("multipath_lunid", "TEXT"),
            ("multipath_primary_path", "TEXT"),
            ("multipath_alternate_path", "TEXT"),
            ("multipath_active_paths", "TEXT"),
            ("multipath_passive_paths", "TEXT"),
            ("multipath_failed_paths", "TEXT"),
            ("multipath_other_paths", "TEXT"),
            ("multipath_active_controllers", "TEXT"),
            ("multipath_passive_controllers", "TEXT"),
            ("multipath_failed_controllers", "TEXT"),
            ("last_seen_at", "TEXT NOT NULL"),
        )
        expected_names = tuple(name for name, _definition in expected_columns)

        expected_update_names = tuple(
            name
            for name in expected_names
            if name not in {"system_id", "enclosure_key", "slot"}
        )
        expected_schema_columns = ",\n".join(
            f"    {name} {definition}" for name, definition in expected_columns
        )
        expected_upsert_columns = ",\n".join(
            f"                {name}" for name in expected_names
        )
        expected_upsert_updates = ",\n".join(
            f"                {name} = excluded.{name}"
            for name in expected_update_names
        )
        expected_adoption_projection = ("?", "?", *expected_names[2:])
        expected_adoption_columns = ",\n".join(
            f"                        {name}" for name in expected_names
        )
        expected_adoption_select = ",\n".join(
            f"                        {projection}"
            for projection in expected_adoption_projection
        )

        self.assertEqual(history_store.SLOT_STATE_COLUMNS, expected_columns)
        self.assertEqual(history_store.SLOT_STATE_COLUMN_NAMES, expected_names)
        self.assertEqual(history_store.SLOT_STATE_UPSERT_COLUMNS, expected_names)
        self.assertEqual(history_store.SLOT_STATE_UPSERT_UPDATE_COLUMNS, expected_update_names)
        self.assertEqual(
            history_store.SLOT_STATE_ADOPTION_PROJECTION,
            expected_adoption_projection,
        )
        self.assertIn(
            f"CREATE TABLE IF NOT EXISTS slot_state_current (\n{expected_schema_columns},\n"
            "    PRIMARY KEY (system_id, enclosure_key, slot)\n);",
            history_store.SCHEMA,
        )
        self.assertEqual(
            history_store.SLOT_STATE_UPSERT_SQL,
            "\n            INSERT INTO slot_state_current (\n"
            f"{expected_upsert_columns}\n"
            f"            ) VALUES ({', '.join('?' for _name in expected_names)})\n"
            "            ON CONFLICT(system_id, enclosure_key, slot) DO UPDATE SET\n"
            f"{expected_upsert_updates}\n"
            "            ",
        )
        self.assertEqual(
            history_store.SLOT_STATE_ADOPTION_SQL,
            "\n                    INSERT OR IGNORE INTO slot_state_current (\n"
            f"{expected_adoption_columns}\n"
            "                    )\n"
            "                    SELECT\n"
            f"{expected_adoption_select}\n"
            "                    FROM slot_state_current\n"
            "                    WHERE system_id = ?\n"
            "                    ",
        )

        record_values: dict[str, Any] = {
            name: f"value-{name}"
            for name in expected_names
            if name != "last_seen_at"
        }
        record_values.update(slot=17, present=True, identify_active=False)
        record = SlotStateRecord(**record_values)
        observed_at = "2030-01-02T03:04:05+00:00"
        expected_parameters = tuple(
            observed_at
            if name == "last_seen_at"
            else int(getattr(record, name))
            if name in {"present", "identify_active"}
            else getattr(record, name)
            for name in expected_names
        )
        self.assertEqual(
            history_store._slot_state_record_values(record, observed_at),
            expected_parameters,
        )
        stored_row = dict(zip(expected_names, expected_parameters, strict=True))
        self.assertEqual(HistoryStore._row_to_slot_state(stored_row), record)  # type: ignore[arg-type]

    def test_rollup_projection_contract_matches_existing_queries(self) -> None:
        expected_projection = """                    NULL AS id,
                    CASE
                        WHEN metric_name IN ('bytes_read', 'bytes_written', 'power_on_hours')
                        THEN last_observed_at
                        ELSE bucket_start
                    END AS observed_at,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    NULL AS value_integer,
                    CASE
                        WHEN metric_name IN ('bytes_read', 'bytes_written', 'power_on_hours')
                        THEN last_value
                        ELSE value_sum / sample_count
                    END AS value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    NULLIF(disk_identity_key, '') AS disk_identity_key,
                    logical_unit_id,
                    sas_address,
                    bucket_seconds AS rollup_seconds,
                    sample_count,
                    value_min,
                    value_max"""

        self.assertEqual(
            history_store.ROLLUP_COUNTER_METRICS,
            ("bytes_read", "bytes_written", "power_on_hours"),
        )
        self.assertEqual(history_store.ROLLUP_TO_SAMPLE_PROJECTION, expected_projection)

    def test_empty_slot_history_payload_is_exact_and_fresh(self) -> None:
        first = HistoryStore._empty_slot_history_payload(("temperature_c", "power_on_hours"))
        second = HistoryStore._empty_slot_history_payload(("temperature_c", "power_on_hours"))

        self.assertEqual(
            first,
            {
                "events": [],
                "metrics": {"temperature_c": [], "power_on_hours": []},
                "sample_counts": {},
                "latest_values": {},
            },
        )
        first["events"].append({"id": 1})
        first["metrics"]["temperature_c"].append({"value": 30})
        self.assertEqual(second["events"], [])
        self.assertEqual(second["metrics"]["temperature_c"], [])

    def test_noninitializing_store_skips_schema_writes_for_maintenance(self) -> None:
        import inspect

        self.assertIn("initialize", inspect.signature(HistoryStore).parameters)
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "history.db"
            with patch.object(HistoryStore, "_initialize") as initialize:
                store = HistoryStore(database, initialize=False)
            initialize.assert_not_called()
            self.assertEqual(store.file_path, database)

    def test_restore_changes_replacement_owner_after_sqlite_closes(self) -> None:
        events: list[str] = []

        class FakeConnection:
            def __init__(self, name: str) -> None:
                self.name = name

            def execute(self, _statement: str) -> None:
                events.append(f"{self.name}:execute")

            def backup(self, _target: object) -> None:
                events.append(f"{self.name}:backup")

            def commit(self) -> None:
                events.append(f"{self.name}:commit")

            def close(self) -> None:
                events.append(f"{self.name}:close")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.db"
            source.write_bytes(b"synthetic sqlite source")
            store = HistoryStore(str(root / "target.db"), initialize=False)

            def publish(
                temp_path: Path,
                _target_path: Path,
                *,
                temp_descriptor: int,
            ) -> None:
                events.append("publish")
                os.close(temp_descriptor)
                temp_path.unlink()

            with (
                patch(
                    "history_service.store.sqlite3.connect",
                    side_effect=(FakeConnection("source"), FakeConnection("replacement")),
                ),
                patch(
                    "history_service.store.os.fchown",
                    side_effect=lambda *_args: events.append("fchown"),
                ),
                patch.object(store, "_publish_replacement", side_effect=publish),
                patch.object(store, "_normalize_database_permissions"),
                patch.object(store, "_initialize_schema") as initialize_schema,
            ):
                store._restore_backup_locked(source)

        initialize_schema.assert_not_called()
        self.assertLess(events.index("source:close"), events.index("fchown"))
        self.assertLess(events.index("replacement:close"), events.index("fchown"))
        self.assertLess(events.index("fchown"), events.index("publish"))

    def test_segmented_retention_claim_is_fail_closed_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "history.db"
            first = HistoryStore(str(db_path))
            backup_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

            self.assertTrue(first.claim_segmented_retention_backup(backup_at))

            restarted = HistoryStore(str(db_path))
            self.assertFalse(restarted.claim_segmented_retention_backup(backup_at))

            newer_backup_at = backup_at + timedelta(days=1)
            self.assertTrue(restarted.claim_segmented_retention_backup(newer_backup_at))
            restarted.release_segmented_retention_backup(newer_backup_at)
            self.assertTrue(restarted.claim_segmented_retention_backup(newer_backup_at))

    def test_segmented_retention_claim_respects_the_shared_history_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(str(Path(temp_dir) / "history.db"))
            backup_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

            with history_write_lock(store.file_path, blocking=False):
                with self.assertRaises(sqlite3.OperationalError):
                    store.claim_segmented_retention_backup(backup_at)

            self.assertTrue(store.claim_segmented_retention_backup(backup_at))

    def test_segmented_retention_finish_respects_the_shared_history_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(str(Path(temp_dir) / "history.db"))
            backup_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            self.assertTrue(store.claim_segmented_retention_backup(backup_at))

            with history_write_lock(store.file_path, blocking=False):
                with self.assertRaises(sqlite3.OperationalError):
                    store.finish_segmented_retention_backup(backup_at, has_more=False)

            store.finish_segmented_retention_backup(backup_at, has_more=False)

    def test_segmented_retention_release_respects_the_shared_history_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(str(Path(temp_dir) / "history.db"))
            backup_at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            self.assertTrue(store.claim_segmented_retention_backup(backup_at))

            with history_write_lock(store.file_path, blocking=False):
                with self.assertRaises(sqlite3.OperationalError):
                    store.release_segmented_retention_backup(backup_at)

            store.release_segmented_retention_backup(backup_at)

    def test_shared_lock_rejects_database_file_mount_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mounted_directory = root / "mounted"
            mounted_directory.mkdir()
            canonical_database_path = mounted_directory / "history.db"
            canonical_database_path.write_bytes(b"")
            alias_directory = root / "alias"
            alias_directory.symlink_to(mounted_directory, target_is_directory=True)
            database_path = alias_directory / "history.db"

            def identify_canonical_mount(path: Path) -> bool:
                self.assertEqual(path, canonical_database_path)
                return True

            with patch.object(
                migration_lock,
                "_database_path_is_mount_point",
                side_effect=identify_canonical_mount,
            ):
                with self.assertRaisesRegex(ValueError, "mount"):
                    with history_write_lock(database_path, blocking=False):
                        self.fail("File-mounted database entered the shared lock")

    def test_shared_lock_converges_when_socket_keys_differ_for_the_same_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "history.db"
            addresses = iter((b"\0history-lock-alias-a", b"\0history-lock-alias-b"))

            with patch.object(migration_lock, "_history_lock_address", side_effect=lambda _path: next(addresses)):
                with history_write_lock(database_path, blocking=False):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "migration"):
                        with history_write_lock(database_path, blocking=False):
                            self.fail("Directory-alias lock owner entered")

    def test_shared_lock_cannot_split_brain_after_legacy_lock_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "history.db"

            with history_write_lock(database_path, blocking=False):
                legacy_lock_path = history_lock_path(database_path)
                legacy_lock_path.unlink(missing_ok=True)
                with self.assertRaisesRegex(sqlite3.OperationalError, "migration"):
                    with history_write_lock(database_path, blocking=False):
                        self.fail("Second migration lock owner entered")

    def test_store_refuses_to_initialize_or_write_while_a_lifecycle_marker_is_pending(self) -> None:
        # Rotation, migration and segmented restore leave a marker while their
        # journal is pending. Only the reader used to honour it; a service
        # restart ran _initialize and the collector kept writing, so recovery
        # could no longer match the hot digest (issue #174).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "history.db"
            segments_directory = root / "segments"
            segments_directory.mkdir()
            catalog_path = segments_directory / "catalog.json"
            store = HistoryStore(
                str(database_path),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            )
            store._execute_write(lambda connection: connection.execute("SELECT 1").fetchone())
            pristine = database_path.read_bytes()

            markers = (
                (activation_pending_path(database_path), "activation is pending"),
                (segments_directory / MIGRATION_PENDING_MARKER, "migration recovery is pending"),
            )
            for marker_path, message in markers:
                marker_path.write_text("{}", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, message):
                        store._execute_write(
                            lambda connection: connection.execute(
                                "INSERT INTO history_table_counts (table_name, row_count) VALUES ('x', 1)"
                            )
                        )
                    with self.assertRaisesRegex(sqlite3.OperationalError, message):
                        HistoryStore(
                            str(database_path),
                            recover_unreadable_database=False,
                            segment_catalog_path=catalog_path,
                        )
                    self.assertEqual(database_path.read_bytes(), pristine, marker_path.name)
                    self.assertFalse(Path(f"{database_path}-wal").exists(), marker_path.name)
                finally:
                    marker_path.unlink()

            recovered = HistoryStore(
                str(database_path),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            )
            self.assertEqual(
                recovered._execute_write(lambda connection: connection.execute("SELECT 1").fetchone())[0],
                1,
            )

    def test_store_rechecks_lifecycle_markers_after_acquiring_the_history_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "history.db"
            marker_path = activation_pending_path(database_path)

            @contextmanager
            def lifecycle_lock_that_creates_a_pending_marker(*args: Any, **kwargs: Any):
                with history_write_lock(*args, **kwargs):
                    marker_path.write_text("{}", encoding="utf-8")
                    yield

            try:
                with patch(
                    "history_service.store.history_write_lock",
                    lifecycle_lock_that_creates_a_pending_marker,
                ):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "activation is pending"):
                        HistoryStore(str(database_path), recover_unreadable_database=False)
            finally:
                marker_path.unlink(missing_ok=True)

    def test_store_initialization_rejects_while_migration_owns_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "history.db"

            with history_write_lock(database_path, blocking=False):
                with self.assertRaisesRegex(sqlite3.OperationalError, "migration"):
                    HistoryStore(str(database_path), recover_unreadable_database=False)

            self.assertFalse(database_path.exists())

    def test_restore_backup_rejects_while_migration_owns_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"), recover_unreadable_database=False)
            backup_path = store.create_backup(root / "backups")
            if backup_path is None:
                self.fail("Expected a backup path")
            original_bytes = store.file_path.read_bytes()

            with history_write_lock(store.file_path, blocking=False):
                with self.assertRaisesRegex(sqlite3.OperationalError, "migration"):
                    store.restore_backup(backup_path)

            self.assertEqual(store.file_path.read_bytes(), original_bytes)

    def test_journal_mode_setup_rejects_while_migration_owns_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(
                str(Path(temp_dir) / "history.db"),
                recover_unreadable_database=False,
            )
            store._journal_mode_identity = None

            with history_write_lock(store.file_path, blocking=False):
                with self.assertRaisesRegex(sqlite3.OperationalError, "migration"):
                    connection = store._connect()
                    connection.close()

    def test_default_store_does_not_widen_existing_database_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "history"
            root.mkdir(mode=0o750)
            db_path = root / "history.db"
            db_path.write_bytes(b"")
            db_path.chmod(0o640)

            HistoryStore(str(db_path))

            self.assertEqual(root.stat().st_mode & 0o777, 0o750)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o640)

    def test_default_store_does_not_rewrite_promoted_backup_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            source_path = root / "source.sqlite3"
            source_path.write_bytes(b"synthetic-backup")
            source_path.chmod(0o600)
            target_path = root / "long-term" / "history.sqlite3"

            store._refresh_backup_copy(source_path, target_path)

            self.assertEqual(target_path.stat().st_mode & 0o777, 0o600)

    def test_default_backup_replacement_preserves_existing_target_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_root = root / "backups"
            backup_root.mkdir()
            snapshot_label = "2030-01-02T03:04:05+00:00"
            target_path = backup_root / "history-20300102T030405Z.sqlite3"
            target_path.write_bytes(b"previous-backup")
            target_path.chmod(0o600)

            backup_path = store.create_backup(backup_root, snapshot_label=snapshot_label)

            self.assertEqual(backup_path, target_path)
            self.assertEqual(target_path.stat().st_mode & 0o777, 0o600)

    def test_default_restore_replacement_preserves_existing_database_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_path = store.create_backup(root / "backups", snapshot_label="2030-01-02T03:04:05+00:00")
            if backup_path is None:
                self.fail("Expected restore source backup to be created")
            store.file_path.chmod(0o600)

            store.restore_backup(backup_path)

            self.assertEqual(store.file_path.stat().st_mode & 0o777, 0o600)

    def test_default_restore_replacement_reapplies_existing_database_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_path = store.create_backup(
                root / "backups",
                snapshot_label="2030-01-02T03:04:05+00:00",
            )
            if backup_path is None:
                self.fail("Expected restore source backup to be created")
            expected_owner = (store.file_path.stat().st_uid, store.file_path.stat().st_gid)

            with patch("history_service.store.os.fchown") as fchown:
                store.restore_backup(backup_path)

            fchown.assert_called_once()
            self.assertEqual(fchown.call_args.args[1:], expected_owner)

    def test_restore_owner_failure_removes_private_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_path = store.create_backup(
                root / "backups",
                snapshot_label="2030-01-02T03:04:05+00:00",
            )
            if backup_path is None:
                self.fail("Expected restore source backup to be created")
            live_before = store.file_path.read_bytes()

            with (
                patch(
                    "history_service.store.os.fchown",
                    side_effect=PermissionError("injected ownership failure"),
                ),
                self.assertRaisesRegex(PermissionError, "injected ownership failure"),
            ):
                store.restore_backup(backup_path)

            replacement_root = root / f".history-replacement-{os.geteuid()}"
            self.assertEqual(store.file_path.read_bytes(), live_before)
            self.assertEqual(list(replacement_root.iterdir()), [])

    def test_failed_restore_publication_preserves_live_sqlite_family(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_path = store.create_backup(root / "backups", snapshot_label="2030-01-02T03:04:05+00:00")
            if backup_path is None:
                self.fail("Expected a backup path")
            main_before = store.file_path.read_bytes()
            wal_path = Path(f"{store.file_path}-wal")
            shm_path = Path(f"{store.file_path}-shm")
            wal_path.write_bytes(b"synthetic-wal")
            shm_path.write_bytes(b"synthetic-shm")

            with (
                patch.object(store, "_rename_at2", side_effect=OSError(errno.ENOSYS, "unsupported")),
                self.assertRaises(OSError),
            ):
                store.restore_backup(backup_path)

            self.assertEqual(store.file_path.read_bytes(), main_before)
            self.assertEqual(wal_path.read_bytes(), b"synthetic-wal")
            self.assertEqual(shm_path.read_bytes(), b"synthetic-shm")

    def test_backup_replacement_uses_private_empty_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            backup_root = root / "backups"

            backup_path = store.create_backup(backup_root, snapshot_label="2030-01-02T03:04:05+00:00")

            self.assertIsNotNone(backup_path)
            replacement_root = backup_root / f".history-replacement-{os.geteuid()}"
            self.assertEqual(replacement_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(replacement_root.iterdir()), [])

    def test_default_promotion_replacement_preserves_existing_target_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            source_path = root / "source.sqlite3"
            source_path.write_bytes(b"replacement-backup")
            source_path.chmod(0o644)
            target_path = root / "long-term" / "history.sqlite3"
            target_path.parent.mkdir()
            target_path.write_bytes(b"previous-backup")
            target_path.chmod(0o600)

            store._refresh_backup_copy(source_path, target_path)

            self.assertEqual(target_path.stat().st_mode & 0o777, 0o600)

    def test_opt_in_backup_replacement_applies_configured_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(
                str(root / "history.db"),
                permission_repair_enabled=True,
                shared_dir_mode=0o750,
                shared_file_mode=0o640,
            )
            backup_root = root / "backups"
            backup_root.mkdir()
            target_path = backup_root / "history-20300102T030405Z.sqlite3"
            target_path.write_bytes(b"previous-backup")
            target_path.chmod(0o600)

            backup_path = store.create_backup(backup_root, snapshot_label="2030-01-02T03:04:05+00:00")

            self.assertEqual(backup_path, target_path)
            self.assertEqual(target_path.stat().st_mode & 0o777, 0o640)

    def test_default_replacement_mode_preservation_refuses_target_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"replacement")
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"target")
            victim_path = root / "victim.sqlite3"
            victim_path.write_bytes(b"victim")
            victim_path.chmod(0o600)
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == target_path and not swapped:
                    swapped = True
                    target_path.unlink()
                    target_path.symlink_to(victim_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaises((OSError, ValueError)),
            ):
                store._preserve_existing_target_mode(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(victim_path.stat().st_mode & 0o777, 0o600)

    def test_default_replacement_mode_preservation_refuses_temp_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"target")
            target_path.chmod(0o600)
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"replacement")
            victim_path = root / "victim.sqlite3"
            victim_path.write_bytes(b"victim")
            victim_path.chmod(0o644)
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == temp_path and not swapped:
                    swapped = True
                    temp_path.unlink()
                    temp_path.symlink_to(victim_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaises((OSError, ValueError)),
            ):
                store._preserve_existing_target_mode(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(victim_path.stat().st_mode & 0o777, 0o644)

    def test_default_replacement_mode_preservation_refuses_temp_inode_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"target")
            target_path.chmod(0o600)
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"replacement")
            replacement_path = root / "replacement-raced.tmp"
            replacement_path.write_bytes(b"raced")
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == temp_path and not swapped:
                    swapped = True
                    temp_path.unlink()
                    replacement_path.replace(temp_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaisesRegex(ValueError, "changed temporary path"),
            ):
                store._preserve_existing_target_mode(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(temp_path.read_bytes(), b"raced")

    def test_default_replacement_publisher_leaves_one_sided_temp_swap_generations_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"original-target")
            target_path.chmod(0o600)
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            injected_path = root / "injected.tmp"
            injected_path.write_bytes(b"injected-replacement")
            injected_path.chmod(0o644)
            real_rename_at2 = store._rename_at2
            swapped = False
            publisher = getattr(store, "_publish_replacement", None)

            def swap_after_validation(source: Path, destination: Path, *, flags: int) -> None:
                nonlocal swapped
                if source == temp_path and not swapped:
                    swapped = True
                    temp_path.unlink()
                    os.replace(injected_path, temp_path)
                real_rename_at2(source, destination, flags=flags)

            self.assertTrue(callable(publisher))
            if not callable(publisher):
                return
            with (
                patch.object(store, "_rename_at2", side_effect=swap_after_validation),
                self.assertRaisesRegex(ValueError, "changed temporary path"),
            ):
                publisher(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(target_path.read_bytes(), b"injected-replacement")
            self.assertEqual(temp_path.read_bytes(), b"original-target")
            self.assertEqual(temp_path.stat().st_mode & 0o777, 0o600)

    def test_default_replacement_publisher_does_not_publish_when_target_vanishes_during_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"original-target")
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            real_open_stable = store._open_stable_regular_file
            removed = False

            def remove_target_before_open(path: Path, *, role: str) -> tuple[int, os.stat_result]:
                nonlocal removed
                if path == target_path and not removed:
                    target_path.unlink()
                    removed = True
                return real_open_stable(path, role=role)

            with (
                patch.object(store, "_open_stable_regular_file", side_effect=remove_target_before_open),
                self.assertRaisesRegex(ValueError, "changed target path"),
            ):
                store._publish_replacement(temp_path, target_path)

            self.assertTrue(removed)
            self.assertFalse(target_path.exists())
            self.assertEqual(temp_path.read_bytes(), b"expected-replacement")

    def test_renameat2_exchange_swaps_two_regular_paths_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_path = root / "first.sqlite3"
            second_path = root / "second.sqlite3"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")

            HistoryStore._rename_at2(first_path, second_path, flags=2)

            self.assertEqual(first_path.read_bytes(), b"second")
            self.assertEqual(second_path.read_bytes(), b"first")

    def test_replacement_exchange_leaves_one_sided_target_swap_generations_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"original-target")
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            displaced_original = root / "displaced-original.sqlite3"
            injected_path = root / "injected.sqlite3"
            injected_path.write_bytes(b"concurrent-target")
            real_rename_at2 = store._rename_at2
            swapped = False

            def replace_target_then_rename(source: Path, target: Path, *, flags: int) -> None:
                nonlocal swapped
                if flags == 2 and not swapped:
                    target_path.replace(displaced_original)
                    injected_path.replace(target_path)
                    swapped = True
                real_rename_at2(source, target, flags=flags)

            with (
                patch.object(store, "_rename_at2", side_effect=replace_target_then_rename),
                self.assertRaisesRegex(ValueError, "changed target path"),
            ):
                store._publish_replacement(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(target_path.read_bytes(), b"expected-replacement")
            self.assertEqual(temp_path.read_bytes(), b"concurrent-target")
            self.assertEqual(displaced_original.read_bytes(), b"original-target")

    def test_replacement_noreplace_preserves_target_that_appears_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            real_rename_at2 = store._rename_at2
            appeared = False

            def create_target_then_rename(source: Path, target: Path, *, flags: int) -> None:
                nonlocal appeared
                if flags == 1 and not appeared:
                    target_path.write_bytes(b"concurrent-target")
                    appeared = True
                real_rename_at2(source, target, flags=flags)

            with (
                patch.object(store, "_rename_at2", side_effect=create_target_then_rename),
                self.assertRaisesRegex(ValueError, "changed target path"),
            ):
                store._publish_replacement(temp_path, target_path)

            self.assertTrue(appeared)
            self.assertEqual(target_path.read_bytes(), b"concurrent-target")

    def test_replacement_noreplace_does_not_unlink_target_swapped_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            displaced_published = root / "displaced-published.sqlite3"
            injected_path = root / "injected.sqlite3"
            injected_path.write_bytes(b"concurrent-target")
            real_rename_at2 = store._rename_at2
            swapped = False

            def swap_after_rename(source: Path, target: Path, *, flags: int) -> None:
                nonlocal swapped
                real_rename_at2(source, target, flags=flags)
                if flags == 1 and not swapped:
                    target_path.replace(displaced_published)
                    injected_path.replace(target_path)
                    swapped = True

            with (
                patch.object(store, "_rename_at2", side_effect=swap_after_rename),
                self.assertRaisesRegex(ValueError, "changed published path"),
            ):
                store._publish_replacement(temp_path, target_path)

            self.assertTrue(swapped)
            self.assertEqual(target_path.read_bytes(), b"concurrent-target")
            self.assertEqual(displaced_published.read_bytes(), b"expected-replacement")

    def test_replacement_uses_and_closes_original_mkstemp_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            descriptor, temp_name = tempfile.mkstemp(dir=root)
            temp_path = Path(temp_name)
            os.write(descriptor, b"expected-replacement")
            temp_path.unlink()
            temp_path.write_bytes(b"substituted-temp")
            target_path = root / "target.sqlite3"

            with self.assertRaisesRegex(ValueError, "changed temporary path"):
                store._publish_replacement(
                    temp_path,
                    target_path,
                    temp_descriptor=descriptor,
                )

            with self.assertRaises(OSError):
                os.fstat(descriptor)
            self.assertFalse(target_path.exists())
            self.assertEqual(temp_path.read_bytes(), b"substituted-temp")

    def test_replacement_cleanup_failure_still_closes_all_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"original-target")
            descriptor, temp_name = tempfile.mkstemp(dir=root)
            temp_path = Path(temp_name)
            os.write(descriptor, b"expected-replacement")
            real_close = os.close
            closed_descriptors: list[int] = []

            def observe_close(fd: int) -> None:
                closed_descriptors.append(fd)
                real_close(fd)

            with (
                patch.object(store, "_unlink_owned_path", create=True, side_effect=OSError("cleanup failed")),
                patch("history_service.store.os.close", side_effect=observe_close),
                self.assertRaisesRegex(OSError, "cleanup failed"),
            ):
                store._publish_replacement(
                    temp_path,
                    target_path,
                    temp_descriptor=descriptor,
                )

            self.assertIn(descriptor, closed_descriptors)
            self.assertGreaterEqual(len(closed_descriptors), 2)
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "requires procfs descriptor paths")
    def test_default_replacement_mode_preservation_only_changes_unpublished_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            target_path = root / "target.sqlite3"
            target_path.write_bytes(b"original-target")
            target_path.chmod(0o600)
            temp_path = root / "replacement.tmp"
            temp_path.write_bytes(b"expected-replacement")
            temp_path.chmod(0o644)
            real_fchmod = os.fchmod
            changed_paths: list[tuple[Path, int]] = []
            publisher = getattr(store, "_publish_replacement", None)

            def observe_fchmod(fd: int, mode: int) -> None:
                changed_paths.append((Path(os.readlink(f"/proc/self/fd/{fd}")), mode))
                real_fchmod(fd, mode)

            self.assertTrue(callable(publisher))
            if not callable(publisher):
                return
            with patch("history_service.store.os.fchmod", side_effect=observe_fchmod):
                publisher(temp_path, target_path)

            self.assertEqual(changed_paths, [(temp_path, 0o600)])
            self.assertEqual(target_path.read_bytes(), b"expected-replacement")
            self.assertEqual(target_path.stat().st_mode & 0o777, 0o600)

    def test_opt_in_permission_repair_applies_exact_modes_to_new_shared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history_root = root / "history"
            backup_root = root / "backups"
            long_term_root = root / "long-term"
            previous_umask = os.umask(0o022)
            try:
                store = HistoryStore(
                    str(history_root / "history.db"),
                    permission_repair_enabled=True,
                    shared_dir_mode=0o770,
                    shared_file_mode=0o660,
                )
                backup_path = store.create_backup(
                    backup_root,
                    snapshot_label="2030-01-02T03:04:05+00:00",
                    long_term_backup_dir=long_term_root,
                    weekly_retention_count=1,
                )
            finally:
                os.umask(previous_umask)

            if backup_path is None:
                self.fail("Expected a backup path")
            weekly_paths = list((long_term_root / "weekly").glob("*.sqlite3"))
            self.assertEqual(len(weekly_paths), 1)
            for path in (history_root, backup_root, long_term_root, long_term_root / "weekly"):
                with self.subTest(path=path):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o770)
            for path in (store.file_path, backup_path, weekly_paths[0]):
                with self.subTest(path=path):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o660)

    def test_opt_in_permission_repair_applies_exact_configured_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "history"
            root.mkdir(mode=0o777)
            db_path = root / "history.db"
            db_path.write_bytes(b"")
            db_path.chmod(0o666)

            try:
                HistoryStore(
                    str(db_path),
                    permission_repair_enabled=True,
                    shared_dir_mode=0o750,
                    shared_file_mode=0o640,
                )
            except TypeError as exc:
                self.fail(f"HistoryStore does not expose configured repair modes: {exc}")

            self.assertEqual(root.stat().st_mode & 0o777, 0o750)
            self.assertEqual(db_path.stat().st_mode & 0o777, 0o640)

    def test_opt_in_permission_repair_rejects_world_writable_modes(self) -> None:
        cases = (
            (0o777, 0o660),
            (0o770, 0o666),
        )
        for shared_dir_mode, shared_file_mode in cases:
            with (
                self.subTest(dir_mode=shared_dir_mode, file_mode=shared_file_mode),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                with self.assertRaisesRegex(ValueError, "world-writable"):
                    HistoryStore(
                        str(Path(temp_dir) / "history.db"),
                        permission_repair_enabled=True,
                        shared_dir_mode=shared_dir_mode,
                        shared_file_mode=shared_file_mode,
                    )

    def test_opt_in_permission_repair_refuses_symlinked_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            real_path = root / "real-history.db"
            real_path.write_bytes(b"")
            real_path.chmod(0o600)
            symlink_path = root / "history.db"
            symlink_path.symlink_to(real_path)

            with self.assertRaisesRegex(ValueError, "symlink"):
                HistoryStore(str(symlink_path), permission_repair_enabled=True)

            self.assertEqual(real_path.stat().st_mode & 0o777, 0o600)

    def test_opt_in_permission_repair_does_not_follow_late_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            store.permission_repair_enabled = True
            store.shared_file_mode = 0o640
            target_path = root / "repair-target"
            target_path.write_bytes(b"target")
            target_path.chmod(0o600)
            victim_path = root / "victim"
            victim_path.write_bytes(b"victim")
            victim_path.chmod(0o600)
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == target_path and not swapped:
                    swapped = True
                    target_path.unlink()
                    target_path.symlink_to(victim_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaises((OSError, ValueError)),
            ):
                store._normalize_shared_path_permissions(target_path)

            self.assertTrue(swapped)
            self.assertTrue(target_path.is_symlink())
            self.assertEqual(victim_path.stat().st_mode & 0o777, 0o600)

    def test_opt_in_permission_repair_refuses_non_regular_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            store.permission_repair_enabled = True
            fifo_path = root / "repair-fifo"
            os.mkfifo(fifo_path)

            with self.assertRaisesRegex(ValueError, "non-regular"):
                store._normalize_shared_path_permissions(fifo_path)

    def test_opt_in_permission_repair_refuses_inode_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            store.permission_repair_enabled = True
            target_path = root / "repair-target"
            target_path.write_bytes(b"original")
            replacement_path = root / "replacement"
            replacement_path.write_bytes(b"replacement")
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == target_path and not swapped:
                    swapped = True
                    target_path.unlink()
                    replacement_path.replace(target_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaisesRegex(ValueError, "changed path"),
            ):
                store._normalize_shared_path_permissions(target_path)

            self.assertTrue(swapped)
            self.assertEqual(target_path.read_bytes(), b"replacement")

    def test_opt_in_permission_repair_refuses_type_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = HistoryStore(str(root / "history.db"))
            store.permission_repair_enabled = True
            target_path = root / "repair-target"
            target_path.write_bytes(b"original")
            real_open = os.open
            swapped = False

            def swap_before_open(path: str | os.PathLike[str], flags: int) -> int:
                nonlocal swapped
                if Path(path) == target_path and not swapped:
                    swapped = True
                    target_path.unlink()
                    os.mkfifo(target_path)
                return real_open(path, flags)

            with (
                patch("history_service.store.os.open", side_effect=swap_before_open),
                self.assertRaisesRegex(ValueError, "non-regular"),
            ):
                store._normalize_shared_path_permissions(target_path)

            self.assertTrue(swapped)

    @staticmethod
    def _metric_sample(observed_at: str, value: int, *, slot: int = 5) -> MetricSample:
        return MetricSample(
            observed_at=observed_at,
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=slot,
            slot_label=f"{slot:02d}",
            metric_name="temperature_c",
            value_integer=value,
            value_real=None,
            device_name=f"da{slot}",
            serial=f"SERIAL-{slot}",
            model="Drive",
            state="healthy",
            gptid=f"gptid/{slot}",
            persistent_id_label="GPTID",
            disk_identity_key=f"serial:SERIAL-{slot}",
            logical_unit_id=None,
            sas_address=None,
        )

    @staticmethod
    def _insert_event_rows(store: HistoryStore, observed_times: list[str]) -> None:
        connection = sqlite3.connect(store.file_path)
        try:
            connection.executemany(
                """
                INSERT INTO slot_events (
                    observed_at, system_id, system_label, enclosure_key,
                    enclosure_id, enclosure_label, slot, slot_label,
                    event_type, previous_value, current_value, device_name,
                    serial, details_json
                ) VALUES (?, 'archive-core', 'Archive CORE', 'enc-a',
                          'enc-a', 'Front Shelf', 5, '05',
                          'slot_state_changed', 'healthy', 'degraded', 'da5',
                          'SERIAL-5', '{}')
                """,
                [(observed_at,) for observed_at in observed_times],
            )
            connection.commit()
        finally:
            connection.close()

    def test_retention_rolls_up_old_metrics_and_prunes_in_bounded_batches(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2026-01-01T10:05:00+00:00", 20),
                self._metric_sample("2026-01-01T10:35:00+00:00", 30),
                self._metric_sample("2026-01-01T10:55:00+00:00", 40),
                self._metric_sample("2026-06-01T00:00:00+00:00", 50),
            ]
        )
        self._insert_event_rows(
            store,
            [
                "2024-01-01T00:00:00+00:00",
                "2024-01-02T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
            ],
        )

        first = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=365,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=2,
            max_batches=1,
        )
        second = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=365,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=2,
            max_batches=1,
        )

        self.assertEqual(first["metric_samples_removed"], 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["metric_samples_removed"], 1)
        self.assertFalse(second["has_more"])
        self.assertEqual(store.counts()["metric_sample_count"], 1)
        self.assertEqual(store.counts()["event_count"], 1)
        samples = store.list_metric_samples(
            "archive-core",
            "enc-a",
            5,
            metric_name="temperature_c",
            since="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual([sample["value"] for sample in samples], [50, 30.0])
        self.assertEqual(samples[1]["rollup_seconds"], 3600)
        self.assertEqual(samples[1]["sample_count"], 3)

    def test_rollups_average_gauges_but_keep_the_latest_counter_value(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2026-01-01T10:05:00+00:00", 20),
                self._metric_sample("2026-01-01T10:55:00+00:00", 30),
                replace(
                    self._metric_sample("2026-01-01T10:05:00+00:00", 100),
                    metric_name="bytes_read",
                ),
                replace(
                    self._metric_sample("2026-01-01T10:55:00+00:00", 250),
                    metric_name="bytes_read",
                ),
            ]
        )

        store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        temperatures = store.list_metric_samples(
            "archive-core", "enc-a", 5,
            metric_name="temperature_c", limit=10,
            since="2026-01-01T00:00:00+00:00",
        )
        counters = store.list_metric_samples(
            "archive-core", "enc-a", 5,
            metric_name="bytes_read", limit=10,
            since="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(temperatures[0]["value"], 25.0)
        self.assertEqual(counters[0]["value"], 250.0)
        self.assertEqual(counters[0]["sample_count"], 2)

    def test_partial_batch_keeps_raw_and_rolled_up_values_from_the_same_hour_visible(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2026-01-01T10:05:00+00:00", 20),
                self._metric_sample("2026-01-01T10:55:00+00:00", 30),
            ]
        )

        result = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=1,
            max_batches=1,
        )
        samples = store.list_metric_samples(
            "archive-core", "enc-a", 5,
            metric_name="temperature_c", limit=10,
            since="2026-01-01T00:00:00+00:00",
        )
        scope = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            event_limit=0,
            metric_limits={"temperature_c": 10},
            since="2026-01-01T00:00:00+00:00",
        )
        homes = store.list_disk_metric_homes(
            "serial:SERIAL-5",
            since="2026-01-01T00:00:00+00:00",
        )

        self.assertTrue(result["has_more"])
        self.assertEqual([sample["value"] for sample in samples], [30, 20.0])
        self.assertEqual(samples[1]["rollup_seconds"], 3600)
        self.assertEqual(
            [sample["value"] for sample in scope[5]["metrics"]["temperature_c"]],
            [30, 20.0],
        )
        self.assertEqual(homes[0]["sample_count"], 2)

    def test_daily_rollup_remains_visible_beside_newer_raw_value_from_same_day(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2026-06-01T10:00:00+00:00", 20),
                self._metric_sample("2026-06-01T14:00:00+00:00", 30),
            ]
        )

        store.maintain_retention(
            now=datetime(2026, 7, 1, 12, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=30,
            daily_rollup_retention_days=365,
            batch_size=100,
            max_batches=2,
        )
        samples = store.list_metric_samples(
            "archive-core", "enc-a", 5,
            metric_name="temperature_c", limit=10,
            since="2026-06-01T00:00:00+00:00",
        )
        scope = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            event_limit=0,
            metric_limits={"temperature_c": 10},
            since="2026-06-01T00:00:00+00:00",
        )

        self.assertEqual([sample["value"] for sample in samples], [30, 20.0])
        self.assertEqual(samples[1]["rollup_seconds"], 86400)
        self.assertEqual(
            [sample["value"] for sample in scope[5]["metrics"]["temperature_c"]],
            [30, 20.0],
        )

    def test_all_history_queries_include_retained_rollups_without_since(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [self._metric_sample("2026-01-01T10:05:00+00:00", 30)]
        )
        store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        samples = store.list_metric_samples(
            "archive-core", "enc-a", 5,
            metric_name="temperature_c", limit=10,
        )
        scope = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            event_limit=0,
            metric_limits={"temperature_c": 10},
        )

        self.assertEqual([sample["value"] for sample in samples], [30.0])
        self.assertEqual(
            [sample["value"] for sample in scope[5]["metrics"]["temperature_c"]],
            [30.0],
        )

    def test_retention_keeps_rows_at_the_exact_cutoff(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [self._metric_sample("2026-06-01T00:00:00+00:00", 31)]
        )
        self._insert_event_rows(store, ["2025-07-01T00:00:00+00:00"])

        result = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=365,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        self.assertEqual(result["total_rows_removed"], 0)
        self.assertEqual(store.counts()["metric_sample_count"], 1)
        self.assertEqual(store.counts()["event_count"], 1)

    def test_retention_zero_values_keep_raw_history_forever(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [self._metric_sample("2020-01-01T00:00:00+00:00", 31)]
        )
        self._insert_event_rows(store, ["2020-01-01T00:00:00+00:00"])

        result = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=0,
            event_retention_days=0,
            hourly_rollup_retention_days=0,
            daily_rollup_retention_days=0,
            batch_size=100,
            max_batches=2,
        )

        self.assertEqual(result["total_rows_removed"], 0)
        self.assertEqual(store.counts()["metric_sample_count"], 1)
        self.assertEqual(store.counts()["event_count"], 1)

    def test_retention_can_stop_between_batches(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2020-01-01T00:00:00+00:00", 20),
                self._metric_sample("2020-01-01T01:00:00+00:00", 21),
                self._metric_sample("2020-01-01T02:00:00+00:00", 22),
            ]
        )
        continuation_checks = 0

        def should_continue() -> bool:
            nonlocal continuation_checks
            continuation_checks += 1
            return continuation_checks == 1

        result = store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=1,
            max_batches=10,
            should_continue=should_continue,
        )

        self.assertEqual(result["metric_samples_removed"], 1)
        self.assertTrue(result["interrupted"])
        self.assertEqual(store.counts()["metric_sample_count"], 2)

    def test_retention_failure_exposes_rows_committed_by_prior_batches(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2020-01-01T00:00:00+00:00", 20),
                self._metric_sample("2020-01-01T01:00:00+00:00", 21),
                self._metric_sample("2020-01-01T02:00:00+00:00", 22),
            ]
        )
        original_execute_write = store._execute_write
        calls = 0

        def fail_second_batch(operation, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("private failure detail")
            return original_execute_write(operation, **kwargs)

        with patch.object(store, "_execute_write", side_effect=fail_second_batch):
            with self.assertRaises(RuntimeError) as raised:
                store.maintain_retention(
                    now=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    raw_metric_retention_days=30,
                    event_retention_days=0,
                    hourly_rollup_retention_days=365,
                    daily_rollup_retention_days=1825,
                    batch_size=1,
                    max_batches=3,
                )

        summary = getattr(raised.exception, "retention_summary", None)
        self.assertIsInstance(summary, dict)
        assert isinstance(summary, dict)
        self.assertEqual(summary["metric_samples_removed"], 1)
        self.assertEqual(summary["hourly_rollups_removed"], 1)
        self.assertEqual(summary["daily_rollups_removed"], 1)
        self.assertEqual(summary["total_rows_removed"], 3)
        self.assertTrue(summary["has_more"])
        self.assertEqual(store.counts()["metric_sample_count"], 2)

    def test_retention_batch_is_bounded_for_60_and_347_slot_fixtures(self) -> None:
        for slot_count in (60, 347):
            with self.subTest(slot_count=slot_count):
                temp_dir = Path(tempfile.mkdtemp())
                store = HistoryStore(str(temp_dir / "history.db"))
                store.insert_metric_samples(
                    [
                        self._metric_sample("2020-01-01T00:00:00+00:00", slot, slot=slot)
                        for slot in range(slot_count)
                    ]
                )

                result = store.maintain_retention(
                    now=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    raw_metric_retention_days=30,
                    event_retention_days=0,
                    hourly_rollup_retention_days=365,
                    daily_rollup_retention_days=1825,
                    batch_size=60,
                    max_batches=1,
                )

                self.assertEqual(result["metric_samples_removed"], 60)
                self.assertEqual(result["has_more"], slot_count > 60)
                self.assertEqual(store.counts()["metric_sample_count"], max(0, slot_count - 60))

    def test_retention_preserves_batched_query_behavior_for_60_and_347_slots(self) -> None:
        for slot_count in (60, 347):
            with self.subTest(slot_count=slot_count):
                temp_dir = Path(tempfile.mkdtemp())
                store = HistoryStore(str(temp_dir / "history.db"))
                samples: list[MetricSample] = []
                for slot in range(slot_count):
                    samples.extend(
                        [
                            self._metric_sample(
                                "2022-01-01T00:05:00+00:00",
                                slot,
                                slot=slot,
                            ),
                            self._metric_sample(
                                "2026-06-30T00:05:00+00:00",
                                slot + 1000,
                                slot=slot,
                            ),
                        ]
                    )
                store.insert_metric_samples(samples)
                connect_calls = 0
                original_connect = store._connect

                def counting_connect() -> sqlite3.Connection:
                    nonlocal connect_calls
                    connect_calls += 1
                    return original_connect()

                started = time.perf_counter()
                with patch.object(store, "_connect", counting_connect):
                    before = store.list_scope_history(
                        "archive-core",
                        "enc-a",
                        slots=list(range(slot_count)),
                        event_limit=0,
                        metric_limits={"temperature_c": 4},
                        since="2022-01-01T00:00:00+00:00",
                    )
                pre_query_seconds = time.perf_counter() - started
                self.assertEqual(connect_calls, 1)

                maintenance_started = time.perf_counter()
                summary = store.maintain_retention(
                    now=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    raw_metric_retention_days=30,
                    event_retention_days=365,
                    hourly_rollup_retention_days=365,
                    daily_rollup_retention_days=1825,
                    batch_size=1000,
                    max_batches=2,
                )
                maintenance_seconds = time.perf_counter() - maintenance_started
                connect_calls = 0
                started = time.perf_counter()
                with patch.object(store, "_connect", counting_connect):
                    after = store.list_scope_history(
                        "archive-core",
                        "enc-a",
                        slots=list(range(slot_count)),
                        event_limit=0,
                        metric_limits={"temperature_c": 4},
                        since="2022-01-01T00:00:00+00:00",
                    )
                post_query_seconds = time.perf_counter() - started

                self.assertEqual(summary["metric_samples_removed"], slot_count)
                self.assertEqual(connect_calls, 1)
                self.assertLess(pre_query_seconds, 5.0)
                self.assertLess(maintenance_seconds, 5.0)
                self.assertLess(post_query_seconds, 5.0)
                for slot in range(slot_count):
                    self.assertEqual(before[slot]["latest_values"]["temperature_c"], slot + 1000)
                    self.assertEqual(after[slot]["latest_values"]["temperature_c"], slot + 1000)
                    self.assertEqual(len(after[slot]["metrics"]["temperature_c"]), 2)

    def test_rollups_participate_in_counts_cleanup_and_adoption(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [self._metric_sample("2026-01-01T10:05:00+00:00", 30)]
        )
        store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        self.assertEqual(store.counts()["metric_rollup_count"], 2)
        summaries = store.list_history_system_summaries()
        self.assertEqual(summaries[0]["metric_rollup_count"], 2)

        adopted = store.adopt_system_history(
            "archive-core",
            "archive-renamed",
            target_system_label="Archive Renamed",
        )

        self.assertEqual(adopted["metric_rollup_count"], 2)
        self.assertEqual(
            [
                item["value"]
                for item in store.list_metric_samples(
                    "archive-renamed",
                    "enc-a",
                    5,
                    metric_name="temperature_c",
                    since="2026-01-01T00:00:00+00:00",
                )
            ],
            [30.0],
        )
        deleted = store.delete_system_history("archive-renamed")
        self.assertEqual(deleted["metric_rollup_count"], 2)
        self.assertEqual(store.counts()["metric_rollup_count"], 0)

    def test_fast_counts_remain_truthful_after_retention_deletes_rows(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [
                self._metric_sample("2020-01-01T00:00:00+00:00", 20),
                self._metric_sample("2020-01-01T01:00:00+00:00", 21),
                self._metric_sample("2026-06-30T00:00:00+00:00", 22),
            ]
        )

        store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        fast_counts = store.estimated_counts()
        self.assertEqual(fast_counts["metric_sample_count"], 1)
        self.assertEqual(fast_counts["metric_rollup_count"], 0)
        self.assertFalse(fast_counts["estimated"])
        self.assertEqual(fast_counts["count_mode"], "tracked")

    def test_retention_rollups_remain_visible_in_batched_scope_and_disk_history(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.insert_metric_samples(
            [self._metric_sample("2026-01-01T10:05:00+00:00", 30)]
        )
        store.maintain_retention(
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=30,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            batch_size=100,
            max_batches=2,
        )

        scope = store.list_scope_history(
            "archive-core",
            "enc-a",
            slots=[5],
            event_limit=0,
            metric_limits={"temperature_c": 10},
            since="2026-01-01T00:00:00+00:00",
        )
        homes = store.list_disk_metric_homes(
            "serial:SERIAL-5",
            since="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(scope[5]["metrics"]["temperature_c"][0]["value"], 30.0)
        self.assertEqual(scope[5]["metrics"]["temperature_c"][0]["rollup_seconds"], 3600)
        self.assertEqual(homes[0]["sample_count"], 1)
        self.assertEqual(homes[0]["slot"], 5)
    def test_store_configures_wal_once_per_database_identity(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        executed_statements: list[str] = []
        original_connect = sqlite3.connect

        class TrackingConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            @property
            def row_factory(self) -> object:
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value: object) -> None:
                self.connection.row_factory = value  # type: ignore[assignment]

            def execute(self, statement: str, parameters: object = ()) -> sqlite3.Cursor:
                executed_statements.append(statement)
                return self.connection.execute(statement, parameters)  # type: ignore[arg-type]

            def close(self) -> None:
                self.connection.close()

            def __getattr__(self, name: str) -> object:
                return getattr(self.connection, name)

        def tracking_connect(*args: object, **kwargs: object) -> TrackingConnection:
            return TrackingConnection(original_connect(*args, **kwargs))  # type: ignore[arg-type]

        with patch("history_service.store.sqlite3.connect", side_effect=tracking_connect):
            store = HistoryStore(str(temp_dir / "history.db"))
            store.estimated_counts()
            store.list_scopes()

        wal_statements = [
            statement
            for statement in executed_statements
            if statement.strip().upper() == "PRAGMA JOURNAL_MODE=WAL"
        ]
        self.assertEqual(wal_statements, ["PRAGMA journal_mode=WAL"])

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

    def test_store_fast_overview_uses_tracked_activity_counts(self) -> None:
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
        self.assertEqual(estimated_counts["metric_sample_count"], 1)
        self.assertFalse(estimated_counts["estimated"])
        self.assertEqual(estimated_counts["count_mode"], "tracked")
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

    def test_store_can_fail_closed_without_quarantining_unreadable_database(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        original = b"not a sqlite database"
        db_path.write_bytes(original)

        with self.assertRaises(sqlite3.DatabaseError):
            HistoryStore(str(db_path), recover_unreadable_database=False)

        self.assertEqual(db_path.read_bytes(), original)
        self.assertEqual(list(temp_dir.glob("history.db.broken-*")), [])

    def test_store_does_not_quarantine_transient_open_failures(self) -> None:
        self.assertFalse(
            HistoryStore._should_recover_database(
                sqlite3.OperationalError("unable to open database file")
            )
        )

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

    def _legacy_metric_row_without_identity_key(self, connection: sqlite3.Connection, slot: int) -> None:
        connection.execute(
            """
            INSERT INTO metric_samples (
                observed_at, system_id, system_label, enclosure_key, enclosure_id, enclosure_label,
                slot, slot_label, metric_name, value_integer, value_real, device_name, serial, model,
                state, gptid, persistent_id_label, disk_identity_key, logical_unit_id, sas_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-16T22:06:00+00:00",
                "archive-core",
                "Archive CORE",
                "enc-a",
                "enc-a",
                "Front Shelf",
                slot,
                f"{slot:02d}",
                "bytes_written",
                123,
                None,
                f"da{slot}",
                f"SERIAL-{slot}",
                "Drive",
                "healthy",
                f"gptid/{slot}",
                "gptid",
                None,
                None,
                None,
            ),
        )

    def test_disk_identity_backfill_runs_once_and_records_user_version(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        HistoryStore(str(db_path))

        with sqlite3.connect(db_path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                DISK_IDENTITY_BACKFILL_USER_VERSION,
            )
            # Simulate a database written before the marker existed: legacy rows, version 0.
            self._legacy_metric_row_without_identity_key(connection, 5)
            connection.execute("PRAGMA user_version = 0")

        with patch.object(
            HistoryStore,
            "_backfill_disk_identity_keys",
            wraps=HistoryStore._backfill_disk_identity_keys,
        ) as backfill:
            HistoryStore(str(db_path))
            HistoryStore(str(db_path))  # second container / restart sharing the same file
            HistoryStore(str(db_path))

        self.assertEqual(backfill.call_count, 1, "only the first initialization after a legacy DB should scan")
        with sqlite3.connect(db_path) as connection:
            row = connection.execute("SELECT disk_identity_key FROM metric_samples WHERE slot = 5").fetchone()
            self.assertEqual(row[0], "serial-5|gptid|gptid/5")
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                DISK_IDENTITY_BACKFILL_USER_VERSION,
            )

    def test_get_slot_states_returns_only_the_requested_scope(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        base = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da0",
            serial="SERIAL-0",
            model="Drive",
            gptid="gptid/0",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )
        store.upsert_slot_state(base, "2026-04-16T22:05:00+00:00")
        store.upsert_slot_state(replace(base, slot=7, slot_label="07", serial="SERIAL-7"), "2026-04-16T22:05:00+00:00")
        store.upsert_slot_state(
            replace(base, enclosure_key="enc-b", enclosure_id="enc-b", slot=0, serial="SERIAL-B0"),
            "2026-04-16T22:05:00+00:00",
        )
        store.upsert_slot_state(
            replace(base, system_id="other", slot=0, serial="SERIAL-O0"),
            "2026-04-16T22:05:00+00:00",
        )

        states = store.get_slot_states("archive-core", "enc-a")

        self.assertEqual(sorted(states), [0, 7])
        self.assertEqual(states[7].serial, "SERIAL-7")
        self.assertEqual(store.get_slot_states("archive-core", "enc-zzz"), {})

    def test_record_slot_updates_writes_events_and_states_in_one_transaction(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        base = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da0",
            serial="SERIAL-0",
            model="Drive",
            gptid="gptid/0",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )
        previous = replace(base, state="healthy")
        current = replace(base, state="warning")
        events = build_slot_events(previous, current, "2026-04-16T22:10:00+00:00")
        self.assertTrue(events, "fixture must produce at least one state event")
        updates = [
            SlotStateUpdate(record=current, observed_at="2026-04-16T22:10:00+00:00", events=events),
            SlotStateUpdate(record=replace(base, slot=1, slot_label="01", serial="SERIAL-1"), observed_at="2026-04-16T22:10:00+00:00"),
        ]

        connect_calls = 0
        original_connect = store._connect

        def counting_connect(**kwargs: Any) -> sqlite3.Connection:
            nonlocal connect_calls
            connect_calls += 1
            return original_connect(**kwargs)

        with patch.object(store, "_connect", counting_connect):
            store.record_slot_updates(updates)

        self.assertEqual(connect_calls, 1, "a batch of updates must use exactly one connection")
        self.assertEqual(store.get_slot_state("archive-core", "enc-a", 0).state, "warning")
        self.assertEqual(store.get_slot_state("archive-core", "enc-a", 1).serial, "SERIAL-1")
        self.assertEqual(
            [event["event_type"] for event in store.list_slot_events("archive-core", "enc-a", 0)],
            [event.event_type for event in events],
        )
        self.assertEqual(store.list_slot_events("archive-core", "enc-a", 1), [])
        store.record_slot_updates([])  # empty batch is a no-op

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

    def test_readonly_database_repair_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(str(Path(temp_dir) / "history.db"))

            with patch.object(store, "_normalize_database_permissions") as normalize:
                repaired = store._attempt_readonly_database_repair(
                    sqlite3.OperationalError("attempt to write a readonly database")
                )

            self.assertFalse(repaired)
            normalize.assert_not_called()

    def test_connect_uses_an_explicit_bounded_timeout(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        connection = MagicMock()

        with patch("history_service.store.sqlite3.connect", return_value=connection) as connect:
            returned_connection = store._connect()

        self.assertIs(returned_connection, connection)
        self.assertIn("timeout", connect.call_args.kwargs)
        self.assertGreater(connect.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(connect.call_args.kwargs["timeout"], 10)

    def test_store_retries_transient_database_locked_write(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        operation = MagicMock(
            side_effect=[sqlite3.OperationalError("database is locked"), "written"]
        )

        with patch("time.sleep") as sleep:
            result = store._execute_write(operation)

        self.assertEqual(result, "written")
        self.assertEqual(operation.call_count, 2)
        sleep.assert_called_once()

    def test_store_database_locked_write_retries_are_bounded(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        operation = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))

        with patch("time.sleep") as sleep:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                store._execute_write(operation)

        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

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
        store._journal_mode_identity = None
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
            patch(
                "history_service.store.sqlite3.connect",
                side_effect=lambda path, **_kwargs: FailingWalConnection(path),
            ),
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
        original_journal_identity = store._journal_mode_identity

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
        with store._connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal_mode, "wal")
        self.assertNotEqual(store._journal_mode_identity, original_journal_identity)

    def test_restore_backup_adopts_pre_rollup_schema(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        db_path = temp_dir / "history.db"
        store = HistoryStore(str(db_path))
        legacy_path = temp_dir / "legacy.sqlite3"
        backup_path = store.create_backup(temp_dir / "backups", retention_count=1)
        self.assertIsNotNone(backup_path)
        assert backup_path is not None
        legacy_path.write_bytes(backup_path.read_bytes())
        with sqlite3.connect(legacy_path) as connection:
            connection.execute("DROP TABLE metric_rollups")
            connection.execute("DROP TABLE history_table_counts")
            connection.commit()

        store.restore_backup(legacy_path)

        self.assertEqual(store.estimated_counts()["metric_rollup_count"], 0)
        with store._connect() as connection:
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertIn("metric_rollups", table_names)
        self.assertIn("history_table_counts", table_names)


class HistoryCollectorTests(unittest.TestCase):
    @staticmethod
    def _scheduled_backup_status(
        *,
        success_at: datetime,
        included_groups: list[str] | None = None,
        absent_groups: list[str] | None = None,
        error_code: str | None = None,
    ) -> dict[str, object]:
        groups = included_groups or ["config_file", "history_db"]
        return {
            "schema_version": 1,
            "enabled": True,
            "included_groups": groups,
            "success_count": 1,
            "failure_count": 1 if error_code else 0,
            "last_attempt_at": success_at.isoformat(),
            "last_success_at": success_at.isoformat(),
            "last_failure_at": success_at.isoformat() if error_code else None,
            "last_size_bytes": 123,
            "last_sha256": "a" * 64,
            "last_artifact_name": "jbod-scheduled-backup-20300102T030405Z-00000001.7z",
            "last_absent_groups": list(absent_groups or []),
            "last_retention_removed": 0,
            "last_error_code": error_code,
        }

    def test_segmented_retention_requires_recent_successful_full_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "scheduled-backup.json"
            now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
            settings = HistorySettings(
                segment_catalog_path=str(Path(temp_dir) / "segments" / "catalog.json"),
                scheduled_backup_status_file=str(status_path),
                segmented_backup_max_age_seconds=36 * 3600,
            )
            collector = HistoryCollector(settings, MagicMock())

            self.assertIsNone(collector._segmented_backup_at_for_retention(now))

            status_path.write_text(
                json.dumps(
                    self._scheduled_backup_status(
                        success_at=now - timedelta(hours=1),
                        included_groups=["config_file"],
                    )
                ),
                encoding="utf-8",
            )
            status_path.chmod(0o600)
            self.assertIsNone(collector._segmented_backup_at_for_retention(now))

            status_path.write_text(
                json.dumps(
                    self._scheduled_backup_status(
                        success_at=now - timedelta(hours=1),
                        absent_groups=["history_db"],
                    )
                ),
                encoding="utf-8",
            )
            self.assertIsNone(collector._segmented_backup_at_for_retention(now))

            status_path.write_text(
                json.dumps(self._scheduled_backup_status(success_at=now - timedelta(hours=37))),
                encoding="utf-8",
            )
            self.assertIsNone(collector._segmented_backup_at_for_retention(now))

            status_path.write_text(
                json.dumps(
                    self._scheduled_backup_status(
                        success_at=now - timedelta(hours=1),
                        error_code="RuntimeError",
                    )
                ),
                encoding="utf-8",
            )
            self.assertIsNone(collector._segmented_backup_at_for_retention(now))

            success_at = now - timedelta(hours=1)
            status_path.write_text(
                json.dumps(self._scheduled_backup_status(success_at=success_at)),
                encoding="utf-8",
            )
            self.assertEqual(collector._segmented_backup_at_for_retention(now), success_at)

    @staticmethod
    def _retention_result(*, has_more: bool) -> dict[str, object]:
        return {
            "metric_samples_removed": 0,
            "events_removed": 0,
            "hourly_rollups_removed": 0,
            "daily_rollups_removed": 0,
            "total_rows_removed": 0,
            "batches_completed": 1,
            "has_more": has_more,
            "interrupted": False,
        }

    def test_segmented_retention_holds_shared_lock_from_claim_through_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
            success_at = now - timedelta(hours=1)
            db_path = Path(temp_dir) / "history.db"
            settings = HistorySettings(
                sqlite_path=str(db_path),
                segment_catalog_path=str(Path(temp_dir) / "segments" / "catalog.json"),
            )
            store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            competing_mutation_entered = False

            def retention_pass(**_kwargs: object) -> dict[str, object]:
                nonlocal competing_mutation_entered
                try:
                    with history_write_lock(db_path, blocking=False):
                        competing_mutation_entered = True
                except sqlite3.OperationalError:
                    pass
                return self._retention_result(has_more=False)

            store.maintain_retention = retention_pass  # type: ignore[method-assign]
            HistoryCollector(settings, store)._run_retention_if_due(
                now,
                backup_succeeded=True,
                backup_at=success_at,
            )

            self.assertFalse(competing_mutation_entered)

    def test_segmented_retention_consumption_survives_collector_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
            success_at = now - timedelta(hours=1)
            db_path = Path(temp_dir) / "history.db"
            settings = HistorySettings(
                sqlite_path=str(db_path),
                segment_catalog_path=str(Path(temp_dir) / "segments" / "catalog.json"),
            )
            first_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            first_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                return_value=self._retention_result(has_more=False)
            )
            HistoryCollector(settings, first_store)._run_retention_if_due(
                now,
                backup_succeeded=True,
                backup_at=success_at,
            )

            second_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            second_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                return_value=self._retention_result(has_more=False)
            )
            HistoryCollector(settings, second_store)._run_retention_if_due(
                now + timedelta(minutes=5),
                backup_succeeded=True,
                backup_at=success_at,
            )

            first_store.maintain_retention.assert_called_once()  # type: ignore[attr-defined]
            second_store.maintain_retention.assert_not_called()  # type: ignore[attr-defined]

    def test_segmented_retention_catchup_survives_collector_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
            success_at = now - timedelta(hours=1)
            db_path = Path(temp_dir) / "history.db"
            settings = HistorySettings(
                sqlite_path=str(db_path),
                segment_catalog_path=str(Path(temp_dir) / "segments" / "catalog.json"),
            )
            first_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            first_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                return_value=self._retention_result(has_more=True)
            )
            HistoryCollector(settings, first_store)._run_retention_if_due(
                now,
                backup_succeeded=True,
                backup_at=success_at,
            )

            second_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            second_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                return_value=self._retention_result(has_more=False)
            )
            HistoryCollector(settings, second_store)._run_retention_if_due(
                now + timedelta(minutes=5),
                backup_succeeded=True,
                backup_at=success_at,
            )

            second_store.maintain_retention.assert_called_once()  # type: ignore[attr-defined]

    def test_segmented_retention_failure_remains_retryable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
            success_at = now - timedelta(hours=1)
            db_path = Path(temp_dir) / "history.db"
            settings = HistorySettings(
                sqlite_path=str(db_path),
                segment_catalog_path=str(Path(temp_dir) / "segments" / "catalog.json"),
            )
            first_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            first_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("private database path")
            )
            HistoryCollector(settings, first_store)._run_retention_if_due(
                now,
                backup_succeeded=True,
                backup_at=success_at,
            )

            second_store = HistoryStore(str(db_path), segment_catalog_path=settings.segment_catalog_path)
            second_store.maintain_retention = MagicMock(  # type: ignore[method-assign]
                return_value=self._retention_result(has_more=False)
            )
            HistoryCollector(settings, second_store)._run_retention_if_due(
                now + timedelta(minutes=5),
                backup_succeeded=True,
                backup_at=success_at,
            )

            second_store.maintain_retention.assert_called_once()  # type: ignore[attr-defined]

    def test_segmented_slow_collection_never_calls_legacy_sqlite_backup(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                segment_catalog_path=str(temp_dir / "segments" / "catalog.json"),
                scheduled_backup_status_file=str(temp_dir / "scheduled-backup.json"),
                startup_grace_seconds=0,
            ),
            store,
        )
        store.create_backup = MagicMock()  # type: ignore[method-assign]
        store.maintain_retention = MagicMock()  # type: ignore[method-assign]
        collector.last_fast_metrics_at = collector.started_at
        collector.last_slow_metrics_at = collector.started_at
        collector._enumerate_scopes = AsyncMock(return_value=[])  # type: ignore[method-assign]

        asyncio.run(collector.run_once(force_slow=True))

        store.create_backup.assert_not_called()  # type: ignore[attr-defined]
        store.maintain_retention.assert_not_called()  # type: ignore[attr-defined]
        self.assertIn(
            "db.backup.skipped",
            [entry["stage"] for entry in collector.status()["collection_stage_timings"]],
        )

    def test_retention_runs_when_due_and_reports_status(self) -> None:
        store = MagicMock()
        store.maintain_retention.return_value = {
            "metric_samples_removed": 12,
            "events_removed": 3,
            "hourly_rollups_removed": 2,
            "daily_rollups_removed": 1,
            "total_rows_removed": 18,
            "batches_completed": 2,
            "has_more": False,
            "interrupted": False,
        }
        settings = HistorySettings(
            raw_metric_retention_days=30,
            event_retention_days=365,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=1825,
            retention_interval_seconds=3600,
            retention_batch_size=60,
            retention_max_batches_per_run=4,
        )
        collector = HistoryCollector(settings, store)
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)

        collector._run_retention_if_due(now, backup_succeeded=True)

        store.maintain_retention.assert_called_once()
        call = store.maintain_retention.call_args.kwargs
        self.assertEqual(call["now"], now)
        self.assertEqual(call["batch_size"], 60)
        self.assertEqual(call["max_batches"], 4)
        self.assertTrue(call["should_continue"]())
        status = collector.status()
        self.assertEqual(status["last_retention_at"], "2026-07-01T00:00:00+00:00")
        self.assertEqual(status["last_retention_rows_removed"], 18)
        self.assertEqual(status["last_retention_metric_samples_removed"], 12)
        self.assertEqual(status["last_retention_events_removed"], 3)
        self.assertIsNone(status["last_retention_error"])

    def test_retention_catchup_runs_again_before_interval_when_more_rows_remain(self) -> None:
        store = MagicMock()
        store.maintain_retention.side_effect = [
            {
                "metric_samples_removed": 10,
                "events_removed": 0,
                "hourly_rollups_removed": 0,
                "daily_rollups_removed": 0,
                "total_rows_removed": 10,
                "batches_completed": 1,
                "has_more": True,
                "interrupted": False,
            },
            {
                "metric_samples_removed": 2,
                "events_removed": 0,
                "hourly_rollups_removed": 0,
                "daily_rollups_removed": 0,
                "total_rows_removed": 2,
                "batches_completed": 1,
                "has_more": False,
                "interrupted": False,
            },
        ]
        collector = HistoryCollector(HistorySettings(retention_interval_seconds=3600), store)
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)

        collector._run_retention_if_due(now, backup_succeeded=True)
        collector._run_retention_if_due(
            now + timedelta(minutes=5),
            backup_succeeded=True,
        )

        self.assertEqual(store.maintain_retention.call_count, 2)
        self.assertFalse(collector.status()["last_retention_has_more"])

    def test_retention_failure_reports_only_exception_class_and_does_not_raise(self) -> None:
        store = MagicMock()
        store.maintain_retention.side_effect = RuntimeError("private database path")
        collector = HistoryCollector(HistorySettings(), store)

        collector._run_retention_if_due(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            backup_succeeded=True,
        )

        status = collector.status()
        self.assertEqual(status["last_retention_error"], "RuntimeError")
        self.assertNotIn("private database path", str(status))

    def test_retention_failure_reports_prior_commits_and_keeps_catchup_pending(self) -> None:
        store = MagicMock()
        failure = RuntimeError("private database path")
        setattr(failure, "retention_summary", {
            "metric_samples_removed": 1,
            "events_removed": 2,
            "hourly_rollups_removed": 0,
            "daily_rollups_removed": 0,
            "total_rows_removed": 3,
            "batches_completed": 1,
            "has_more": True,
            "interrupted": False,
        })
        store.maintain_retention.side_effect = failure
        collector = HistoryCollector(HistorySettings(), store)

        with patch("history_service.collector.observe_history_retention_run") as observe:
            collector._run_retention_if_due(
                datetime(2026, 7, 1, tzinfo=timezone.utc),
                backup_succeeded=True,
            )

        status = collector.status()
        self.assertEqual(status["last_retention_rows_removed"], 3)
        self.assertEqual(status["last_retention_metric_samples_removed"], 1)
        self.assertEqual(status["last_retention_events_removed"], 2)
        self.assertTrue(status["last_retention_has_more"])
        observe.assert_called_once()
        self.assertEqual(
            observe.call_args.kwargs["removed_rows"],
            {
                "metric_samples": 1,
                "events": 2,
                "hourly_rollups": 0,
                "daily_rollups": 0,
            },
        )

    def test_retention_requires_a_successful_same_pass_backup(self) -> None:
        store = MagicMock()
        collector = HistoryCollector(HistorySettings(), store)

        collector._run_retention_if_due(
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            backup_succeeded=False,
        )

        store.maintain_retention.assert_not_called()
        self.assertIsNone(collector.status()["last_retention_attempt_at"])

    def test_stop_waits_for_inflight_worker_before_returning(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            MagicMock(),
        )
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        async def enumerate_scopes(**_: object) -> list[ScopeSnapshot]:
            worker_started.set()
            while not release_worker.is_set():
                await asyncio.sleep(0.01)
            worker_finished.set()
            return []

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]

        async def exercise_stop() -> bool:
            await collector.start()
            started = await asyncio.to_thread(worker_started.wait, 1.0)
            self.assertTrue(started)
            stop_task = asyncio.create_task(collector.stop())
            await asyncio.sleep(0.05)
            returned_before_worker = stop_task.done()
            release_worker.set()
            await asyncio.wait_for(stop_task, timeout=1.0)
            return returned_before_worker

        returned_before_worker = asyncio.run(exercise_stop())

        self.assertFalse(returned_before_worker)
        self.assertTrue(worker_finished.is_set())
        self.assertFalse(collector.collection_running)
        self.assertIsNone(collector._task)

    def test_stop_request_prevents_later_scope_writes(self) -> None:
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
        scopes = [
            ScopeSnapshot(
                system_id=f"system-{index}",
                system_label=f"System {index}",
                enclosure_id=None,
                enclosure_label=None,
                snapshot={"slots": []},
            )
            for index in range(2)
        ]
        collector._enumerate_scopes = AsyncMock(return_value=scopes)  # type: ignore[method-assign]
        first_write_started = threading.Event()
        release_first_write = threading.Event()
        written_scopes: list[str] = []

        def record_slot_changes(records: list[SlotStateRecord], _: str) -> None:
            written_scopes.append(records[0].system_id if records else scopes[len(written_scopes)].system_id)
            if len(written_scopes) == 1:
                first_write_started.set()
                release_first_write.wait(timeout=1.0)

        collector._record_slot_changes = record_slot_changes  # type: ignore[method-assign]

        async def exercise_stop() -> None:
            await collector.start()
            started = await asyncio.to_thread(first_write_started.wait, 1.0)
            self.assertTrue(started)
            stop_task = asyncio.create_task(collector.stop())
            await asyncio.sleep(0.02)
            release_first_write.set()
            await asyncio.wait_for(stop_task, timeout=1.0)

        with patch("history_service.collector.observe_history_collection_run"):
            asyncio.run(exercise_stop())

        self.assertEqual(written_scopes, ["system-0"])

    def test_stop_waits_for_manual_collection_worker(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            MagicMock(),
        )
        worker_started = threading.Event()
        release_worker = threading.Event()

        async def enumerate_scopes(**_: object) -> list[ScopeSnapshot]:
            worker_started.set()
            while not release_worker.is_set():
                await asyncio.sleep(0.01)
            return []

        collector._enumerate_scopes = enumerate_scopes  # type: ignore[method-assign]

        async def exercise_stop() -> bool:
            manual_task = asyncio.create_task(
                collector.run_once(collection_kind="manual")
            )
            started = await asyncio.to_thread(worker_started.wait, 1.0)
            self.assertTrue(started)
            await collector.start()
            stop_task = asyncio.create_task(collector.stop())
            await asyncio.sleep(0.05)
            returned_before_worker = stop_task.done()
            release_worker.set()
            with self.assertRaises(HistoryCollectionStopping):
                await asyncio.wait_for(manual_task, timeout=1.0)
            await asyncio.wait_for(stop_task, timeout=1.0)
            return returned_before_worker

        returned_before_worker = asyncio.run(exercise_stop())

        self.assertFalse(returned_before_worker)
        self.assertFalse(collector.collection_running)

    def test_background_schedule_uses_pass_start_and_reports_overrun(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            MagicMock(),
        )
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

        with (
            patch("history_service.collector.time.perf_counter", return_value=112.5),
            patch("history_service.collector.utcnow", return_value=now),
            self.assertLogs("history_service.collector", level="WARNING") as captured,
        ):
            sleep_for = collector._schedule_next_background_collection(
                started_monotonic=100.0,
                target_interval=10.0,
            )

        self.assertEqual(sleep_for, 7.5)
        self.assertEqual(collector.last_background_overrun_seconds, 2.5)
        self.assertEqual(collector.next_collection_at, now + timedelta(seconds=7.5))
        self.assertIn("overran its 10.0-second interval by 2.5 seconds", captured.output[0])

    def test_stop_interrupts_startup_grace_without_running_collection(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=60,
            ),
            MagicMock(),
        )
        collector.run_once = AsyncMock()  # type: ignore[method-assign]

        async def exercise_stop() -> None:
            await collector.start()
            await asyncio.wait_for(collector.stop(), timeout=0.5)

        asyncio.run(exercise_stop())

        collector.run_once.assert_not_awaited()  # type: ignore[attr-defined]

    def test_background_schedule_skips_exact_boundary_catch_up(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(temp_dir / "history.db"),
                backup_dir=str(temp_dir / "backups"),
                startup_grace_seconds=0,
            ),
            MagicMock(),
        )
        now = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)

        with (
            patch("history_service.collector.time.perf_counter", return_value=120.0),
            patch("history_service.collector.utcnow", return_value=now),
            self.assertLogs("history_service.collector", level="WARNING"),
        ):
            sleep_for = collector._schedule_next_background_collection(
                started_monotonic=100.0,
                target_interval=10.0,
            )

        self.assertEqual(sleep_for, 10.0)
        self.assertEqual(collector.last_background_overrun_seconds, 10.0)
        self.assertEqual(collector.next_collection_at, now + timedelta(seconds=10.0))

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
        self.assertEqual(collector.status()["background_backoff_delay_seconds"], 5)
        self.assertEqual(collector.background_backoff_until, now + timedelta(seconds=5))

        collector._record_background_failure(now + timedelta(seconds=5))
        self.assertEqual(collector.background_consecutive_failures, 2)
        self.assertEqual(collector.background_backoff_until, now + timedelta(seconds=15))

        collector._record_background_failure(now + timedelta(seconds=15))
        self.assertEqual(collector.background_consecutive_failures, 3)
        self.assertEqual(collector.status()["background_backoff_delay_seconds"], 12)
        self.assertEqual(collector.status()["failure_backoff_max_seconds"], 12)
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
        self.assertEqual(collector.status()["background_backoff_delay_seconds"], 0)
        self.assertIsNone(collector.background_backoff_until)
        self.assertEqual(collector.background_backoff_seconds_remaining, 0)

    def test_record_slot_changes_uses_one_read_and_one_write_per_pass(self) -> None:
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
        base = SlotStateRecord(
            system_id="archive-core",
            system_label="Archive CORE",
            enclosure_key="enc-a",
            enclosure_id="enc-a",
            enclosure_label="Front Shelf",
            slot=0,
            slot_label="00",
            present=True,
            state="healthy",
            identify_active=False,
            device_name="da0",
            serial="SERIAL-0",
            model="Drive",
            gptid="gptid/0",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
        )
        first_pass = [replace(base, slot=slot, slot_label=f"{slot:02d}", serial=f"SERIAL-{slot}") for slot in range(12)]
        collector._record_slot_changes(first_pass, "2026-04-16T22:05:00+00:00")
        second_pass = [
            replace(record, state="warning" if record.slot % 3 == 0 else record.state) for record in first_pass
        ]

        connect_calls = 0
        original_connect = store._connect

        def counting_connect(**kwargs: Any) -> sqlite3.Connection:
            nonlocal connect_calls
            connect_calls += 1
            return original_connect(**kwargs)

        with patch.object(store, "_connect", counting_connect):
            collector._record_slot_changes(second_pass, "2026-04-16T22:10:00+00:00")

        self.assertEqual(connect_calls, 2, "one scope read plus one batched write, regardless of slot count")
        for slot in range(12):
            loaded = store.get_slot_state("archive-core", "enc-a", slot)
            self.assertIsNotNone(loaded)
            expected_state = "warning" if slot % 3 == 0 else "healthy"
            self.assertEqual(loaded.state, expected_state)
            events = store.list_slot_events("archive-core", "enc-a", slot)
            if slot % 3 == 0:
                self.assertTrue(events, f"slot {slot} changed state and must have an event")
            else:
                self.assertEqual(events, [])

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

    def test_enumerate_scopes_appends_storage_views_after_enclosure_walk(self) -> None:
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
            "systems": [{"id": "archive-core", "label": "Archive CORE"}],
        }
        system_snapshot = {
            "selected_system_id": "archive-core",
            "selected_system_label": "Archive CORE",
            "selected_system_platform": "core",
            "selected_enclosure_id": "enc-a",
            "selected_enclosure_label": "Front Shelf",
            "enclosures": [{"id": "enc-a", "label": "Front Shelf"}],
            "slots": [],
        }
        storage_views_payload = {
            "system_label": "Archive CORE",
            "views": [
                {
                    "id": "boot-doms",
                    "label": "Boot SATADOMs",
                    "source": "inventory_binding",
                    "slots": [
                        {
                            "slot_index": 0,
                            "slot_label": "DOM-A",
                            "occupied": True,
                            "state": "matched",
                        }
                    ],
                }
            ],
        }
        collector._fetch_inventory = AsyncMock(side_effect=[root_snapshot, system_snapshot])  # type: ignore[method-assign]
        collector._fetch_storage_views = AsyncMock(return_value=storage_views_payload)  # type: ignore[method-assign]

        scopes = asyncio.run(collector._enumerate_scopes())

        self.assertEqual(
            [scope.enclosure_id for scope in scopes],
            ["enc-a", "storage-view:boot-doms"],
        )
        collector._fetch_storage_views.assert_awaited_once_with(  # type: ignore[attr-defined]
            system_id="archive-core",
            force=True,
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

    def test_smart_alert_evidence_requires_an_alert_field(self) -> None:
        self.assertFalse(
            HistoryCollector._smart_summary_has_alert_evidence(
                {"available": True, "power_on_hours": 100}
            )
        )

    def test_smart_alert_evidence_classifies_storcli_fault_as_failure(self) -> None:
        self.assertTrue(
            HistoryCollector._smart_summary_indicates_failure(
                {"available": True, "smart_health_status": "FAULT"}
            )
        )

    def test_run_once_publishes_deduplicated_complete_smart_alert_evidence(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        store = HistoryStore(str(temp_dir / "history.db"))
        store.create_backup = MagicMock(return_value=None)  # type: ignore[method-assign]
        settings = HistorySettings(
            sqlite_path=str(temp_dir / "history.db"),
            backup_dir=str(temp_dir / "backups"),
            poll_interval_seconds=300,
            failure_backoff_max_seconds=900,
            startup_grace_seconds=0,
        )
        collector = HistoryCollector(settings, store)
        live_slots = [
            {
                "slot": 0,
                "present": True,
                "serial": "DISK-A",
                "logical_unit_id": "0x5000cca000000001",
                "device_name": "da0",
                "state": "healthy",
            },
            {
                "slot": 1,
                "present": True,
                "serial": "DISK-B",
                "gptid": "gptid/disk-b",
                "persistent_id_label": "GPTID",
                "device_name": "da1",
                "state": "healthy",
            },
        ]
        duplicate_view_slot = {
            "slot": 7,
            "present": True,
            "serial": "DISK-A",
            "logical_unit_id": "0x5000cca000000001",
            "device_name": "view-da0",
            "state": "matched",
        }
        collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="enc-a",
                    enclosure_label="Front Shelf",
                    snapshot={
                        "selected_system_id": "archive-core",
                        "selected_enclosure_id": "enc-a",
                        "slots": live_slots,
                    },
                ),
                ScopeSnapshot(
                    system_id="archive-core",
                    system_label="Archive CORE",
                    enclosure_id="storage-view:critical",
                    enclosure_label="Critical disks",
                    snapshot={
                        "selected_system_id": "archive-core",
                        "selected_enclosure_id": "storage-view:critical",
                        "slots": [duplicate_view_slot],
                    },
                ),
            ]
        )
        collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    0: {"available": True, "temperature_c": 61, "smart_health_status": "FAILED"},
                    1: {"available": True, "temperature_c": 44, "predictive_errors": 2},
                },
                {7: {"available": True, "temperature_c": 60, "smart_health_status": "FAILED"}},
            ]
        )

        asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False))

        status = collector.status()
        self.assertEqual(status["poll_interval_seconds"], 300)
        self.assertEqual(status["failure_backoff_max_seconds"], 900)
        self.assertEqual(status["last_smart_failure_evidence_disks"], 2)
        self.assertEqual(status["last_max_temperature_celsius"], 61)
        self.assertIsNotNone(status["last_smart_evidence_at"])

        collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    0: {"available": True, "power_on_hours": 100},
                    1: {"available": True, "power_on_hours": 200},
                },
                {7: {"available": True, "power_on_hours": 100}},
            ]
        )
        asyncio.run(collector.run_once(force_fast=True, include_due_intervals=False))

        partial_status = collector.status()
        self.assertEqual(partial_status["last_smart_failure_evidence_disks"], 2)
        self.assertEqual(partial_status["last_max_temperature_celsius"], 61)
        self.assertEqual(partial_status["last_smart_evidence_at"], status["last_smart_evidence_at"])

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
