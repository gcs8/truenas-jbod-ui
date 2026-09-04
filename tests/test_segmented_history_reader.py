from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from history_service import segment_reader
from history_service.segment_reader import MAX_HISTORY_QUERY_LIMIT, SegmentedHistoryReader
from history_service.store import SCHEMA, HistoryStore


class SegmentedHistoryReaderCliTests(unittest.TestCase):
    def test_reader_orders_and_filters_mixed_offsets_by_absolute_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(
                hot_path,
                [(2, "2025-01-02T10:00:00+00:00")],
                [(2, "2025-01-02T10:00:00+00:00", 20)],
            )
            self._create_database(
                segment_path,
                [(1, "2025-01-01T23:30:00-12:00")],
                [(1, "2025-01-01T23:30:00-12:00", 10)],
            )
            reader = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            )

            events = reader.list_slot_events("system-1", "enclosure-1", 1, limit=10)
            samples = reader.list_metric_samples(
                "system-1",
                "enclosure-1",
                1,
                metric_name="temperature",
                limit=10,
            )
            ranged = reader.list_metric_samples(
                "system-1",
                "enclosure-1",
                1,
                metric_name="temperature",
                limit=10,
                since="2025-01-02T11:00:00+00:00",
            )

            self.assertEqual([event["id"] for event in events], [1, 2])
            self.assertEqual([sample["id"] for sample in samples], [1, 2])
            self.assertEqual([sample["id"] for sample in ranged], [1])

    def test_disk_home_enumeration_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [], [])
            self._create_database(segment_path, [], [])
            with sqlite3.connect(hot_path) as connection:
                connection.execute(
                    """
                    INSERT INTO slot_state_current (
                        system_id, enclosure_key, slot, slot_label,
                        present, identify_active, last_seen_at, disk_identity_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "system-current",
                        "enclosure-current",
                        1,
                        "slot-1",
                        1,
                        0,
                        "2025-02-01T00:00:00+00:00",
                        "disk-many-homes",
                    ),
                )
            with sqlite3.connect(segment_path) as connection:
                connection.executemany(
                    """
                    INSERT INTO metric_samples (
                        id, observed_at, system_id, enclosure_key, slot, slot_label,
                        metric_name, value_integer, disk_identity_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            index,
                            f"2025-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                            f"system-{index}",
                            f"enclosure-{index}",
                            index,
                            f"slot-{index}",
                            "temperature",
                            30,
                            "disk-many-homes",
                        )
                        for index in range(1, MAX_HISTORY_QUERY_LIMIT + 2)
                    ],
                )
            reader = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            )

            homes = reader.list_disk_metric_homes(
                "disk-many-homes",
                limit=MAX_HISTORY_QUERY_LIMIT,
            )
            bundle = reader.get_slot_history_bundle(
                "system-current",
                "enclosure-current",
                1,
                event_limit=1,
                metric_limits={"temperature": 1},
            )

            self.assertEqual(len(homes), MAX_HISTORY_QUERY_LIMIT)
            self.assertEqual(
                len(bundle["disk_history"]["homes"]),
                MAX_HISTORY_QUERY_LIMIT,
            )

    def test_history_store_routes_reads_through_a_configured_segment_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(
                hot_path,
                [(2, "2025-01-02T00:00:00+00:00")],
                [(2, "2025-01-02T00:00:00+00:00", 32)],
            )
            self._create_database(
                segment_path,
                [(1, "2025-01-01T00:00:00+00:00")],
                [(1, "2025-01-01T00:00:00+00:00", 31)],
            )
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-01T00:00:00+00:00",
                                "coverage_end": "2025-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            store = HistoryStore(
                str(hot_path),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            )

            self.assertEqual(
                [event["id"] for event in store.list_slot_events("system-1", "enclosure-1", 1)],
                [2, 1],
            )
            self.assertEqual(
                [
                    sample["id"]
                    for sample in store.list_metric_samples(
                        "system-1",
                        "enclosure-1",
                        1,
                        metric_name="temperature",
                    )
                ],
                [2, 1],
            )
            self.assertEqual(store.counts()["event_count"], 2)
            self.assertEqual(
                [
                    sample["id"]
                    for sample in store.list_scope_history(
                        "system-1",
                        "enclosure-1",
                        slots=[1],
                        event_limit=10,
                        metric_limits={"temperature": 10},
                    )[1]["metrics"]["temperature"]
                ],
                [2, 1],
            )
            store._segment_reader_cache = None
            store._segment_reader_identity = None
            with patch.object(
                SegmentedHistoryReader,
                "from_catalog",
                wraps=SegmentedHistoryReader.from_catalog,
            ) as catalog_loader:
                store.list_slot_events("system-1", "enclosure-1", 1)
                store.counts()
            self.assertEqual(catalog_loader.call_count, 1)
            with self.assertRaisesRegex(ValueError, "segmented"):
                store.create_backup(root / "backups")
            with self.assertRaisesRegex(ValueError, "segmented"):
                store.delete_system_history("system-1")

    def test_reader_aggregates_counts_scopes_and_system_summaries_across_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(
                hot_path,
                [(2, "2025-01-02T00:00:00+00:00")],
                [(2, "2025-01-02T00:00:00+00:00", 32)],
            )
            self._create_database(
                segment_path,
                [(1, "2025-01-01T00:00:00+00:00")],
                [(1, "2025-01-01T00:00:00+00:00", 31)],
            )
            with sqlite3.connect(hot_path) as connection:
                connection.execute("UPDATE slot_events SET system_id = 'system-new'")
                connection.execute("UPDATE metric_samples SET system_id = 'system-new'")
                connection.execute(
                    """
                    INSERT INTO slot_state_current (
                        system_id, system_label, enclosure_key, enclosure_id,
                        enclosure_label, slot, slot_label, present,
                        identify_active, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "system-new",
                        "System New",
                        "enclosure-1",
                        "enclosure-1",
                        "Enclosure 1",
                        1,
                        "slot-1",
                        1,
                        0,
                        "2025-01-02T00:00:00+00:00",
                    ),
                )
            with sqlite3.connect(segment_path) as connection:
                connection.execute("UPDATE slot_events SET system_id = 'system-old', system_label = 'System Old'")
                connection.execute("UPDATE metric_samples SET system_id = 'system-old', system_label = 'System Old'")

            reader = SegmentedHistoryReader(hot_path=hot_path, segment_paths=[segment_path])
            counts = reader.counts()
            scopes = reader.list_scopes()
            summaries = reader.list_history_system_summaries()
            database_size_bytes = reader.database_size_bytes()

            self.assertEqual(
                counts,
                {
                    "tracked_slots": 1,
                    "event_count": 2,
                    "metric_sample_count": 2,
                    "metric_rollup_count": 0,
                },
            )
            self.assertEqual(
                database_size_bytes,
                hot_path.stat().st_size + segment_path.stat().st_size,
            )
            self.assertEqual(len(scopes), 1)
            self.assertEqual(scopes[0]["system_id"], "system-new")
            self.assertEqual(scopes[0]["event_count"], 1)
            self.assertEqual(
                {
                    summary["system_id"]: (
                        summary["tracked_slots"],
                        summary["event_count"],
                        summary["metric_sample_count"],
                    )
                    for summary in summaries
                },
                {
                    "system-new": (1, 1, 1),
                    "system-old": (0, 1, 1),
                },
            )

    def test_reader_batches_scope_history_across_hot_and_sealed_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(
                hot_path,
                [(3, "2025-01-03T00:00:00+00:00"), (4, "2025-01-04T00:00:00+00:00")],
                [(3, "2025-01-03T00:00:00+00:00", 33), (4, "2025-01-04T00:00:00+00:00", 44)],
            )
            self._create_database(
                segment_path,
                [(1, "2025-01-01T00:00:00+00:00"), (2, "2025-01-02T00:00:00+00:00")],
                [(1, "2025-01-01T00:00:00+00:00", 31), (2, "2025-01-02T00:00:00+00:00", 42)],
            )
            for path in (hot_path, segment_path):
                with sqlite3.connect(path) as connection:
                    connection.execute("UPDATE slot_events SET slot = 2, slot_label = 'slot-2' WHERE id IN (2, 4)")
                    connection.execute("UPDATE metric_samples SET slot = 2, slot_label = 'slot-2' WHERE id IN (2, 4)")

            reader = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            )
            with patch.object(reader, "_query_connection", wraps=reader._query_connection) as query_connection:
                histories = reader.list_scope_history(
                    "system-1",
                    "enclosure-1",
                    slots=[1, 2],
                    event_limit=10,
                    metric_limits={"temperature": 10},
                )

            self.assertEqual([event["id"] for event in histories[1]["events"]], [3, 1])
            self.assertEqual([event["id"] for event in histories[2]["events"]], [4, 2])
            self.assertEqual(
                [sample["id"] for sample in histories[1]["metrics"]["temperature"]],
                [3, 1],
            )
            self.assertEqual(
                [sample["id"] for sample in histories[2]["metrics"]["temperature"]],
                [4, 2],
            )
            self.assertEqual(histories[1]["sample_counts"], {"temperature": 2})
            self.assertEqual(histories[2]["latest_values"], {"temperature": 44})
            self.assertEqual(query_connection.call_count, 2)

    def test_reader_follows_one_disk_identity_across_hot_and_sealed_homes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [], [(2, "2025-01-02T00:00:00+00:00", 32)])
            self._create_database(segment_path, [], [(1, "2025-01-01T00:00:00+00:00", 31)])
            with sqlite3.connect(hot_path) as connection:
                connection.execute(
                    """
                    UPDATE metric_samples
                    SET disk_identity_key = ?, system_id = ?, enclosure_key = ?, slot = ?
                    """,
                    ("disk-identity-1", "system-new", "enclosure-new", 2),
                )
                connection.execute(
                    """
                    INSERT INTO metric_samples (
                        id, observed_at, system_id, enclosure_key, slot, slot_label,
                        metric_name, value_integer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        3,
                        "2025-01-03T00:00:00+00:00",
                        "system-new",
                        "enclosure-new",
                        2,
                        "slot-2",
                        "temperature",
                        33,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO slot_state_current (
                        system_id, enclosure_key, slot, slot_label, present,
                        identify_active, last_seen_at, disk_identity_key,
                        serial, gptid, persistent_id_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "system-new",
                        "enclosure-new",
                        2,
                        "slot-2",
                        1,
                        0,
                        "2025-01-03T00:00:00+00:00",
                        "disk-identity-1",
                        "SERIAL-1",
                        "gptid/1",
                        "GPTID",
                    ),
                )
            with sqlite3.connect(segment_path) as connection:
                connection.execute(
                    """
                    UPDATE metric_samples
                    SET disk_identity_key = ?, system_id = ?, enclosure_key = ?, slot = ?
                    """,
                    ("disk-identity-1", "system-old", "enclosure-old", 1),
                )

            reader = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            )
            samples = reader.list_disk_metric_samples(
                "disk-identity-1",
                metric_name="temperature",
                limit=10,
            )
            homes = reader.list_disk_metric_homes("disk-identity-1")
            followed = reader.list_followed_metric_samples(
                "system-new",
                "enclosure-new",
                2,
                "disk-identity-1",
                metric_name="temperature",
                limit=10,
            )
            bundle = reader.get_slot_history_bundle(
                "system-new",
                "enclosure-new",
                2,
                metric_limits={"temperature": 10},
            )

            self.assertEqual([sample["id"] for sample in samples], [2, 1])
            self.assertEqual([sample["id"] for sample in followed], [3, 2, 1])
            self.assertEqual([sample["id"] for sample in bundle["metrics"]["temperature"]], [3, 2, 1])
            self.assertEqual(bundle["sample_counts"], {"temperature": 3})
            self.assertEqual(bundle["latest_values"], {"temperature": 33})
            self.assertTrue(bundle["disk_history"]["followed"])
            self.assertEqual(bundle["disk_history"]["prior_home_count"], 1)
            self.assertEqual(
                [(sample["system_id"], sample["enclosure_key"], sample["slot"]) for sample in samples],
                [
                    ("system-new", "enclosure-new", 2),
                    ("system-old", "enclosure-old", 1),
                ],
            )
            self.assertEqual(
                [
                    (home["system_id"], home["enclosure_key"], home["slot"], home["sample_count"])
                    for home in homes
                ],
                [
                    ("system-old", "enclosure-old", 1, 1),
                    ("system-new", "enclosure-new", 2, 1),
                ],
            )

    def test_catalog_reader_rejects_mixed_offset_coverage_that_hides_an_interior_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(
                segment_path,
                [
                    (1, "2025-01-01T00:00:00+14:00"),
                    (2, "2025-01-01T10:00:00-12:00"),
                    (3, "2025-01-01T12:00:00+14:00"),
                ],
            )
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-01T00:00:00+14:00",
                                "coverage_end": "2025-01-01T12:00:00+14:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "coverage"):
                SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                ).list_slot_events(
                    "system-1",
                    "enclosure-1",
                    1,
                    since="2025-01-01T00:00:00+00:00",
                )

    def test_catalog_query_rejects_segment_swap_restored_before_path_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            parked_path = root / "parked.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._create_database(replacement_path, [(99, "2025-01-02T00:00:00+00:00")])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-01T00:00:00+00:00",
                                "coverage_end": "2025-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            reader = SegmentedHistoryReader.from_catalog(hot_path=hot_path, catalog_path=catalog_path)
            original_connect = sqlite3.connect
            connect_count = 0
            decoy_descriptor = -1

            def bounce_segment_during_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
                nonlocal connect_count, decoy_descriptor
                connect_count += 1
                if connect_count != 2:
                    return original_connect(database, *args, **kwargs)
                decoy_descriptor = os.open(segment_path, os.O_RDONLY)
                segment_path.replace(parked_path)
                replacement_path.replace(segment_path)
                connection = original_connect(database, *args, **kwargs)
                segment_path.replace(replacement_path)
                parked_path.replace(segment_path)
                return connection

            try:
                with patch.object(sqlite3, "connect", side_effect=bounce_segment_during_connect):
                    events = reader.list_slot_events("system-1", "enclosure-1", 1)
            finally:
                if decoy_descriptor >= 0:
                    os.close(decoy_descriptor)

            self.assertEqual([event["id"] for event in events], [1])

    def test_catalog_reader_rejects_coverage_that_does_not_match_segment_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-02T00:00:00+00:00")])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-01T00:00:00+00:00",
                                "coverage_end": "2025-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "coverage"):
                SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                ).list_slot_events(
                    "system-1",
                    "enclosure-1",
                    1,
                    since="2025-01-02T00:00:00+00:00",
                )

    def test_catalog_query_uses_the_same_segment_inode_it_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._create_database(replacement_path, [(99, "2025-01-02T00:00:00+00:00")])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-01T00:00:00+00:00",
                                "coverage_end": "2025-01-01T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            reader = SegmentedHistoryReader.from_catalog(hot_path=hot_path, catalog_path=catalog_path)
            original_connect = sqlite3.connect
            connect_count = 0

            def replace_before_segment_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
                nonlocal connect_count
                connect_count += 1
                if connect_count == 2:
                    replacement_path.replace(segment_path)
                return original_connect(database, *args, **kwargs)

            with patch.object(sqlite3, "connect", side_effect=replace_before_segment_connect):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    reader.list_slot_events("system-1", "enclosure-1", 1)

    def test_catalog_reader_preselects_only_segments_covering_a_narrow_since_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            segments = []
            first_day = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
            for day in range(1, 34):
                segment_id = f"segment-{day:04d}"
                observed_at = (first_day + timedelta(days=day - 1)).isoformat()
                segment_path = root / f"{segment_id}.sqlite3"
                self._create_database(segment_path, [(day, observed_at)])
                segments.append(
                    {
                        "segment_id": segment_id,
                        "file_name": segment_path.name,
                        "size_bytes": segment_path.stat().st_size,
                        "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                        "coverage_start": observed_at,
                        "coverage_end": observed_at,
                    }
                )
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": segments,
                    }
                ),
                encoding="utf-8",
            )

            events = SegmentedHistoryReader.from_catalog(
                hot_path=hot_path,
                catalog_path=catalog_path,
            ).list_slot_events(
                "system-1",
                "enclosure-1",
                1,
                since="2025-02-02T00:00:00+00:00",
            )

            self.assertEqual([event["id"] for event in events], [33])

    def test_catalog_reader_refuses_an_unbounded_query_that_exceeds_the_segment_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            segments = []
            first_day = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
            for day in range(1, 34):
                segment_id = f"segment-{day:04d}"
                observed_at = (first_day + timedelta(days=day - 1)).isoformat()
                segment_path = root / f"{segment_id}.sqlite3"
                self._create_database(segment_path, [(day, observed_at)])
                segments.append(
                    {
                        "segment_id": segment_id,
                        "file_name": segment_path.name,
                        "size_bytes": segment_path.stat().st_size,
                        "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                        "coverage_start": observed_at,
                        "coverage_end": observed_at,
                    }
                )
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": segments,
                    }
                ),
                encoding="utf-8",
            )

            reader = SegmentedHistoryReader.from_catalog(hot_path=hot_path, catalog_path=catalog_path)
            with self.assertRaisesRegex(ValueError, "segment limit"):
                reader.list_slot_events("system-1", "enclosure-1", 1)

    def test_reader_merges_hot_raw_samples_with_sealed_hourly_rollups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [], [(3, "2025-01-03T12:00:00+00:00", 33)])
            self._create_database(segment_path, [])
            with sqlite3.connect(segment_path) as connection:
                connection.execute(
                    """
                    INSERT INTO metric_rollups (
                        bucket_start, bucket_seconds, system_id, enclosure_key, slot, slot_label,
                        metric_name, sample_count, value_sum, value_min, value_max, last_value,
                        last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2025-01-02T00:00:00+00:00",
                        3600,
                        "system-1",
                        "enclosure-1",
                        1,
                        "slot-1",
                        "temperature",
                        2,
                        64.0,
                        31.0,
                        33.0,
                        33.0,
                        "2025-01-02T00:30:00+00:00",
                    ),
                )

            samples = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            ).list_metric_samples(
                "system-1",
                "enclosure-1",
                1,
                metric_name="temperature",
                limit=10,
            )

            self.assertEqual([sample["value"] for sample in samples], [33, 32.0])
            self.assertEqual(samples[1]["rollup_seconds"], 3600)
            self.assertEqual(samples[1]["sample_count"], 2)
            self.assertEqual(samples[1]["value_min"], 31.0)
            self.assertEqual(samples[1]["value_max"], 33.0)

    def test_reader_merges_partial_rollups_with_the_same_bucket_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [])
            insert_sql = """
                INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, enclosure_key, slot, slot_label,
                    metric_name, sample_count, value_sum, value_min, value_max, last_value,
                    last_observed_at
                ) VALUES (?, 3600, 'system-1', 'enclosure-1', 1, 'slot-1',
                          'temperature', ?, ?, ?, ?, ?, ?)
            """
            with sqlite3.connect(segment_path) as connection:
                connection.execute(
                    insert_sql,
                    (
                        "2025-01-01T10:00:00+00:00",
                        2,
                        62.0,
                        30.0,
                        32.0,
                        32.0,
                        "2025-01-01T10:30:00+00:00",
                    ),
                )
            with sqlite3.connect(hot_path) as connection:
                connection.execute(
                    insert_sql,
                    (
                        "2025-01-01T10:00:00+00:00",
                        2,
                        70.0,
                        34.0,
                        36.0,
                        36.0,
                        "2025-01-01T10:50:00+00:00",
                    ),
                )

            reader = SegmentedHistoryReader(
                hot_path=hot_path,
                segment_paths=[segment_path],
            )
            samples = reader.list_metric_samples(
                "system-1",
                "enclosure-1",
                1,
                metric_name="temperature",
                limit=10,
            )
            batched_samples = reader.list_scope_history(
                "system-1",
                "enclosure-1",
                slots=[1],
                event_limit=0,
                metric_limits={"temperature": 10},
            )[1]["metrics"]["temperature"]

            self.assertEqual(len(batched_samples), 1)
            self.assertEqual(batched_samples[0]["value"], 33.0)
            self.assertEqual(batched_samples[0]["sample_count"], 4)
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["value"], 33.0)
            self.assertEqual(samples[0]["sample_count"], 4)
            self.assertEqual(samples[0]["value_min"], 30.0)
            self.assertEqual(samples[0]["value_max"], 36.0)

    def test_catalog_loader_refuses_a_dangling_pending_migration_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / ".migration-pending.json").symlink_to(root / "missing-recovery-receipt")

            with self.assertRaisesRegex(ValueError, "recovery"):
                SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )

    def test_catalog_loader_refuses_a_generation_with_pending_migration_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / ".migration-pending.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "recovery"):
                SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )

    def test_catalog_loader_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            catalog_path.write_text(
                '{"catalog_version":1,"generation_id":"generation-0001",'
                '"complete":false,"complete":true,"segments":[]}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate"):
                SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )

    def test_catalog_verification_does_not_materialize_segment_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(2, "2025-01-02T00:00:00+00:00")])
            segment_sha256 = hashlib.sha256(segment_path.read_bytes()).hexdigest()
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": segment_sha256,
                                "coverage_start": "2025-01-02T00:00:00+00:00",
                                "coverage_end": "2025-01-02T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(Path, "read_bytes", side_effect=AssertionError("segment bytes materialized")):
                reader = SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )

            self.assertEqual(reader.segment_paths, (segment_path.resolve(),))

    def test_catalog_digest_verification_is_reused_across_isolated_query_connections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                reader = SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )
                verified_digest_count = sha256.call_count
                original_connect = sqlite3.connect
                with patch.object(
                    segment_reader.sqlite3,
                    "connect",
                    wraps=original_connect,
                ) as connect:
                    first = reader.list_slot_events("system-1", "enclosure-1", 1)
                    second = reader.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(verified_digest_count, 1)
            self.assertEqual(sha256.call_count, verified_digest_count)
            self.assertEqual(connect.call_count, 4)
            self.assertEqual([event["id"] for event in first], [1])
            self.assertEqual([event["id"] for event in second], [1])

    def test_catalog_digest_verification_is_reused_without_generation_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog.pop("generation_id")
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                reader = SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )
                verified_digest_count = sha256.call_count
                reader.list_slot_events("system-1", "enclosure-1", 1)
                reader.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(verified_digest_count, 1)
            self.assertEqual(sha256.call_count, verified_digest_count)

    def test_catalog_digest_cache_reauthenticates_an_atomically_replaced_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            replacement_path = root / "replacement.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            replacement_path.write_bytes(segment_path.read_bytes())
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=hot_path,
                catalog_path=catalog_path,
            )
            replacement_path.replace(segment_path)
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                events = reader.list_slot_events("system-1", "enclosure-1", 1)
                reader.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(sha256.call_count, 1)
            self.assertEqual([event["id"] for event in events], [1])

    def test_catalog_digest_cache_reauthenticates_segment_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=hot_path,
                catalog_path=catalog_path,
            )
            metadata = segment_path.stat()
            os.utime(
                segment_path,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
            )
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                reader.list_slot_events("system-1", "enclosure-1", 1)
                reader.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(sha256.call_count, 1)

    def test_catalog_generation_change_gets_a_fresh_verified_digest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            successor_catalog_path = root / "successor-catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            self._write_catalog(
                successor_catalog_path,
                segment_path,
                generation_id="generation-0002",
            )
            store = HistoryStore(
                str(hot_path),
                recover_unreadable_database=False,
                segment_catalog_path=catalog_path,
            )
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                store.list_slot_events("system-1", "enclosure-1", 1)
                first_reader = store._segment_reader_cache
                successor_catalog_path.replace(catalog_path)
                store.list_slot_events("system-1", "enclosure-1", 1)
                second_reader = store._segment_reader_cache
                store.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(sha256.call_count, 2)
            self.assertIsNot(first_reader, second_reader)

    def test_catalog_digest_cache_does_not_outlive_its_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [(1, "2025-01-01T00:00:00+00:00")])
            self._write_catalog(catalog_path, segment_path)
            original_sha256 = hashlib.sha256

            with patch.object(segment_reader.hashlib, "sha256", wraps=original_sha256) as sha256:
                first_reader = SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )
                first_reader.list_slot_events("system-1", "enclosure-1", 1)
                second_reader = SegmentedHistoryReader.from_catalog(
                    hot_path=hot_path,
                    catalog_path=catalog_path,
                )
                second_reader.list_slot_events("system-1", "enclosure-1", 1)

            self.assertEqual(sha256.call_count, 2)

    def test_cli_merges_hot_and_sealed_raw_metric_samples_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [], [(3, "2025-01-03T00:00:00+00:00", 33)])
            self._create_database(segment_path, [], [(2, "2025-01-02T00:00:00+00:00", 32)])

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/query_segmented_history.py",
                    "--kind",
                    "raw-metric-samples",
                    "--hot",
                    str(hot_path),
                    "--segment",
                    str(segment_path),
                    "--system-id",
                    "system-1",
                    "--enclosure-id",
                    "enclosure-1",
                    "--slot",
                    "1",
                    "--metric-name",
                    "temperature",
                    "--limit",
                    "2",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([sample["id"] for sample in json.loads(result.stdout)], [3, 2])
            self.assertEqual([sample["value"] for sample in json.loads(result.stdout)], [33, 32])

    def test_cli_loads_complete_catalog_and_verifies_segment_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            catalog_path = root / "catalog.json"
            self._create_database(hot_path, [(3, "2025-01-03T00:00:00+00:00")])
            self._create_database(segment_path, [(2, "2025-01-02T00:00:00+00:00")])
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "generation-0001",
                        "complete": True,
                        "segments": [
                            {
                                "segment_id": "segment-0001",
                                "file_name": segment_path.name,
                                "size_bytes": segment_path.stat().st_size,
                                "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                                "coverage_start": "2025-01-02T00:00:00+00:00",
                                "coverage_end": "2025-01-02T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/query_segmented_history.py",
                    "--hot",
                    str(hot_path),
                    "--catalog",
                    str(catalog_path),
                    "--system-id",
                    "system-1",
                    "--enclosure-id",
                    "enclosure-1",
                    "--slot",
                    "1",
                    "--limit",
                    "2",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([event["id"] for event in json.loads(result.stdout)], [3, 2])

    def test_cli_rejects_query_with_more_than_segment_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [])
            self._create_database(segment_path, [])
            command = [
                sys.executable,
                "scripts/query_segmented_history.py",
                "--hot",
                str(hot_path),
            ]
            for _ in range(33):
                command.extend(("--segment", str(segment_path)))
            command.extend(("--system-id", "system-1", "--slot", "1"))

            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("segment limit", result.stderr)

    def test_cli_merges_hot_and_sealed_slot_events_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hot_path = root / "hot.sqlite3"
            segment_path = root / "segment-0001.sqlite3"
            self._create_database(hot_path, [(3, "2025-01-03T00:00:00+00:00")])
            self._create_database(
                segment_path,
                [(1, "2025-01-01T00:00:00+00:00"), (2, "2025-01-02T00:00:00+00:00")],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/query_segmented_history.py",
                    "--hot",
                    str(hot_path),
                    "--segment",
                    str(segment_path),
                    "--system-id",
                    "system-1",
                    "--enclosure-id",
                    "enclosure-1",
                    "--slot",
                    "1",
                    "--limit",
                    "2",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            events = json.loads(result.stdout)
            self.assertEqual([event["id"] for event in events], [3, 2])
            self.assertEqual(
                [event["observed_at"] for event in events],
                ["2025-01-03T00:00:00+00:00", "2025-01-02T00:00:00+00:00"],
            )

    @staticmethod
    def _write_catalog(
        catalog_path: Path,
        segment_path: Path,
        *,
        generation_id: str = "generation-0001",
    ) -> None:
        catalog_path.write_text(
            json.dumps(
                {
                    "catalog_version": 1,
                    "generation_id": generation_id,
                    "complete": True,
                    "segments": [
                        {
                            "segment_id": "segment-0001",
                            "file_name": segment_path.name,
                            "size_bytes": segment_path.stat().st_size,
                            "sha256": hashlib.sha256(segment_path.read_bytes()).hexdigest(),
                            "coverage_start": "2025-01-01T00:00:00+00:00",
                            "coverage_end": "2025-01-01T00:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_quiesced_hot_reader_creates_no_sqlite_sidecars_on_a_service_wal_hot(self) -> None:
        # The service leaves the hot database with a WAL header. A plain mode=ro
        # open of that quiesced file creates -wal/-shm that survive close, and
        # the next rotation or migration preflight refuses it (issue #278).
        with tempfile.TemporaryDirectory() as temporary_directory:
            hot_path = Path(temporary_directory) / "hot.sqlite3"
            self._create_service_wal_hot(hot_path, [(3, "2025-01-03T00:00:00+00:00")])

            reader = SegmentedHistoryReader(hot_path=hot_path, quiesced_hot=True)
            events = reader.list_slot_events("system-1", "enclosure-1", 1, limit=10)

            self.assertEqual([event["id"] for event in events], [3])
            self._assert_no_sqlite_sidecars(hot_path)

    def test_quiesced_hot_reader_refuses_a_hot_that_has_sidecars(self) -> None:
        # immutable=1 ignores an existing WAL, so a hot database that is still
        # open elsewhere is refused instead of being read without its pending frames.
        with tempfile.TemporaryDirectory() as temporary_directory:
            hot_path = Path(temporary_directory) / "hot.sqlite3"
            self._create_database(hot_path, [(3, "2025-01-03T00:00:00+00:00")])
            Path(f"{hot_path}-wal").write_bytes(b"")
            reader = SegmentedHistoryReader(hot_path=hot_path, quiesced_hot=True)

            with self.assertRaisesRegex(ValueError, "sidecar"):
                reader.list_slot_events("system-1", "enclosure-1", 1, limit=10)

    def test_cli_query_of_a_service_wal_hot_creates_no_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hot_path = Path(temporary_directory) / "hot.sqlite3"
            self._create_service_wal_hot(hot_path, [(3, "2025-01-03T00:00:00+00:00")])

            result = self._run_query_cli(hot_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([event["id"] for event in json.loads(result.stdout)], [3])
            self._assert_no_sqlite_sidecars(hot_path)

    def test_cli_reads_a_hot_beside_a_running_service_only_with_live_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hot_path = Path(temporary_directory) / "hot.sqlite3"
            self._create_service_wal_hot(hot_path, [(3, "2025-01-03T00:00:00+00:00")])
            service_connection = sqlite3.connect(hot_path)
            try:
                service_connection.execute("SELECT count(*) FROM slot_events").fetchone()
                self.assertTrue(Path(f"{hot_path}-wal").exists())

                refused = self._run_query_cli(hot_path)
                live = self._run_query_cli(hot_path, "--live")
            finally:
                service_connection.close()

            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("sidecar", refused.stderr)
            # The packaged CLI is operator-facing: a plain message, not a traceback.
            self.assertNotIn("Traceback", refused.stderr)
            self.assertIn("--live", refused.stderr)
            self.assertEqual(live.returncode, 0, live.stderr)
            self.assertEqual([event["id"] for event in json.loads(live.stdout)], [3])

    @staticmethod
    def _run_query_cli(hot_path: Path, *extra_arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "scripts/query_segmented_history.py",
                "--hot",
                str(hot_path),
                "--system-id",
                "system-1",
                "--enclosure-id",
                "enclosure-1",
                "--slot",
                "1",
                *extra_arguments,
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
            text=True,
        )

    def _create_service_wal_hot(self, path: Path, events: list[tuple[int, str]]) -> None:
        """Build a hot database the way the service leaves it: WAL header, closed cleanly, no sidecars."""
        self._create_database(path, events)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        self.assertEqual(path.read_bytes()[18:20], b"\x02\x02")
        self._assert_no_sqlite_sidecars(path)

    def _assert_no_sqlite_sidecars(self, path: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(f"{path}{suffix}").exists(), f"{path.name}{suffix} exists")

    @staticmethod
    def _create_database(
        path: Path,
        events: list[tuple[int, str]],
        samples: list[tuple[int, str, int]] = [],
    ) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            for event_id, observed_at in events:
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        id, observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, observed_at, "system-1", "enclosure-1", 1, "slot-1", "state_change", "{}"),
                )
            for sample_id, observed_at, value in samples:
                connection.execute(
                    """
                    INSERT INTO metric_samples (
                        id, observed_at, system_id, enclosure_key, slot, slot_label, metric_name, value_integer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sample_id, observed_at, "system-1", "enclosure-1", 1, "slot-1", "temperature", value),
                )
