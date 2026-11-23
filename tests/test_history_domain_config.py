from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from history_service import main as history_main
from history_service.config import HistorySettings, get_history_settings
from history_service.domain import SlotStateRecord, build_slot_events


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
            sas_address="5000c50000008102",
        )
        current = replace(previous, sas_address="5000c50000008100")

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

    def test_history_settings_default_backup_footprint_is_documented(self) -> None:
        settings = HistorySettings()

        self.assertEqual(settings.backup_interval_seconds, 86400)
        self.assertEqual(settings.backup_retention_count, 7)
        self.assertEqual(settings.weekly_backup_retention_count, 4)
        self.assertEqual(settings.monthly_backup_retention_count, 3)

    def test_history_settings_reject_an_unusable_backup_copy_count(self) -> None:
        with self.assertRaises(ValueError):
            HistorySettings(backup_retention_count=0)

    def test_history_settings_bound_the_retention_backup_skip_window(self) -> None:
        self.assertEqual(HistorySettings().retention_backup_skip_max_seconds, 86400)
        with self.assertRaises(ValueError):
            HistorySettings(retention_backup_skip_max_seconds=-1)

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
