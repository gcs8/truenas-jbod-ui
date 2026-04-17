from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.domain import MetricSample, SlotStateRecord, build_slot_events
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


class HistoryStoreTests(unittest.TestCase):
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
            gptid="gptid/5",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
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
                )
            ]
        )

        events = store.list_slot_events("archive-core", "enc-a", 5)
        samples = store.list_metric_samples("archive-core", "enc-a", 5, metric_name="temperature_c")
        scopes = store.list_scopes()
        counts = store.counts()

        self.assertEqual(len(events), 1)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["value"], 31)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(counts["tracked_slots"], 1)
        self.assertEqual(counts["event_count"], 1)
        self.assertEqual(counts["metric_sample_count"], 1)

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

    def test_store_migrates_existing_state_table_before_upserts(self) -> None:
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

        migrated = sqlite3.connect(db_path)
        try:
            columns = {
                str(column_name)
                for _, column_name, *_ in migrated.execute("PRAGMA table_info(slot_state_current)").fetchall()
            }
        finally:
            migrated.close()

        loaded = store.get_slot_state("archive-core", "enc-a", 5)

        self.assertIn("multipath_state", columns)
        self.assertIn("multipath_active_paths", columns)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.topology_label, "tank > raidz2-0 > data")
        self.assertEqual(loaded.multipath_passive_paths, "da44")


class HistoryCollectorTests(unittest.TestCase):
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

        async def enumerate_scopes() -> list[ScopeSnapshot]:
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

        async def enumerate_scopes() -> list[ScopeSnapshot]:
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
