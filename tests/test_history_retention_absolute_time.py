"""History retention compares absolute instants, not timestamp text (#1204).

Migration, imports and history reads accept ISO-8601 timestamps with any UTC
offset. Retention must therefore expire a row only when its absolute time is
older than the cutoff, whatever offset it was stored with. All rows here are
synthetic and live in a temporary directory.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from history_service.domain import MetricSample
from history_service.segment_migration import migrate_segmented_history
from history_service.store import HistoryStore

SYSTEM_ID = "synthetic-system"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
# One-day retention from NOW: the cutoff is 2026-11-14T12:00:00+00:00.


def _retain(
    store: HistoryStore,
    *,
    raw_metric_retention_days: int = 0,
    event_retention_days: int = 0,
    hourly_rollup_retention_days: int = 0,
    daily_rollup_retention_days: int = 0,
    batch_size: int = 100,
    max_batches: int = 10,
) -> dict:
    return store.maintain_retention(
        now=NOW,
        raw_metric_retention_days=raw_metric_retention_days,
        event_retention_days=event_retention_days,
        hourly_rollup_retention_days=hourly_rollup_retention_days,
        daily_rollup_retention_days=daily_rollup_retention_days,
        batch_size=batch_size,
        max_batches=max_batches,
    )


def _insert_events(path: Path, rows: list[tuple[str, str]]) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executemany(
            """
            INSERT INTO slot_events (
                observed_at, system_id, enclosure_key, slot, slot_label,
                event_type, details_json, current_value
            ) VALUES (?, ?, '', 0, '0', 'slot_state_changed', '{}', ?)
            """,
            [(observed_at, SYSTEM_ID, label) for observed_at, label in rows],
        )
        connection.commit()


def _event_labels(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return sorted(
            row[0] for row in connection.execute("SELECT current_value FROM slot_events")
        )


def _sample(observed_at: str, value: int, *, metric_name: str = "temperature_c") -> MetricSample:
    return MetricSample(
        observed_at=observed_at,
        system_id=SYSTEM_ID,
        system_label="Synthetic",
        enclosure_key="enc-a",
        enclosure_id="enc-a",
        enclosure_label="Front Shelf",
        slot=5,
        slot_label="05",
        metric_name=metric_name,
        value_integer=value,
        value_real=None,
        device_name="da5",
        serial="SANITIZED-5",
        model="Drive",
        state="healthy",
    )


# (stored text, label). The label says what retention must do with the row.
# Cutoff is 12:00 UTC on 2026-11-14.
MIXED_OFFSET_EVENTS = [
    ("2026-11-01T00:00:00+00:00", "expired-utc"),
    # 13:00 UTC: newer than the cutoff, but its text sorts before it.
    ("2026-11-14T08:00:00-05:00", "kept-negative-offset"),
    # 11:00 UTC: older than the cutoff, but its text sorts after it.
    ("2026-11-14T20:00:00+09:00", "expired-positive-offset"),
    # 11:00 UTC, stored with a negative offset: a control that must go.
    ("2026-11-14T06:00:00-05:00", "expired-negative-offset"),
    # The widest offsets SQLite reads, one second either side of the cutoff.
    ("2026-11-15T02:58:59+14:59", "expired-widest-positive-offset"),
    ("2026-11-13T21:01:01-14:59", "kept-widest-negative-offset"),
    ("2026-11-14T12:00:00+00:00", "kept-at-cutoff"),
    ("2026-11-14 13:00:00Z", "kept-space-separator"),
    ("2026-11-15T00:00:00+00:00", "kept-utc"),
]
KEPT_EVENTS = sorted(label for _, label in MIXED_OFFSET_EVENTS if label.startswith("kept-"))


class SegmentedRetentionAbsoluteTimeTests(unittest.TestCase):
    def test_segmented_retention_deletes_only_migrated_events_older_than_the_cutoff(self) -> None:
        with tempfile.TemporaryDirectory(prefix="retention-offset-") as temporary:
            root = Path(temporary)
            hot = root / "hot.db"
            segments = root / "segments"
            HistoryStore(str(hot))
            _insert_events(hot, MIXED_OFFSET_EVENTS)
            migrate_segmented_history(
                source=hot,
                segments_directory=segments,
                cutoff="2026-11-10T00:00:00+00:00",
                key_id="synthetic-key-id",
                apply=True,
            )
            store = HistoryStore(str(hot), segment_catalog_path=segments / "catalog.json")
            # The oldest UTC row was sealed into a segment by the migration.
            self.assertNotIn("expired-utc", _event_labels(hot))

            result = store.run_segmented_retention(
                NOW,
                lambda: store.maintain_retention(
                    now=NOW,
                    raw_metric_retention_days=0,
                    event_retention_days=1,
                    hourly_rollup_retention_days=0,
                    daily_rollup_retention_days=0,
                    batch_size=10,
                    max_batches=1,
                    migration_lock_held=True,
                ),
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(_event_labels(hot), KEPT_EVENTS)
            self.assertEqual(result["events_removed"], 3)
            self.assertFalse(result["has_more"])


class HotRetentionAbsoluteTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-offset-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        self.store = HistoryStore(str(self.path))

    def test_event_retention_compares_absolute_time_in_every_batch(self) -> None:
        _insert_events(self.path, MIXED_OFFSET_EVENTS)

        first = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=1)
        rest = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=10)

        self.assertEqual(first["events_removed"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(rest["events_removed"], 3)
        self.assertFalse(rest["has_more"])
        self.assertEqual(_event_labels(self.path), KEPT_EVENTS)

    def test_remaining_row_check_sees_an_expired_row_whose_text_sorts_after_the_cutoff(self) -> None:
        _insert_events(
            self.path,
            [
                ("2026-11-14T10:00:00+00:00", "expired-utc"),
                ("2026-11-14T20:00:00+09:00", "expired-positive-offset"),
            ],
        )

        result = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=1)

        self.assertEqual(result["events_removed"], 1)
        self.assertTrue(result["has_more"])

    def test_metric_retention_keeps_offset_samples_newer_than_the_cutoff(self) -> None:
        self.store.insert_metric_samples(
            [
                _sample("2026-11-14T08:00:00-05:00", 40),  # 13:00 UTC, kept
                _sample("2026-11-14T20:00:00+09:00", 30),  # 11:00 UTC, expired
                _sample("2026-11-14T11:30:00+00:00", 20),  # 11:30 UTC, expired
            ]
        )

        result = _retain(
            self.store,
            raw_metric_retention_days=1,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=365,
        )

        with closing(sqlite3.connect(self.path)) as connection:
            remaining = connection.execute(
                "SELECT observed_at, value_integer FROM metric_samples"
            ).fetchall()
            hourly = connection.execute(
                "SELECT bucket_start, sample_count FROM metric_rollups "
                "WHERE bucket_seconds = 3600 ORDER BY bucket_start"
            ).fetchall()
        self.assertEqual(remaining, [("2026-11-14T08:00:00-05:00", 40)])
        self.assertEqual(result["metric_samples_removed"], 2)
        self.assertFalse(result["has_more"])
        self.assertEqual(
            hourly,
            [("2026-11-14T11:00:00+00:00", 2)],
        )

    def test_rollup_expiry_keeps_offset_buckets_newer_than_the_cutoff(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                """
                INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, enclosure_key, slot,
                    slot_label, metric_name, sample_count, value_sum, value_min,
                    value_max, last_value, last_observed_at, disk_identity_key
                ) VALUES (?, 3600, ?, 'enc-a', 5, '05', ?, 1, 1, 1, 1, 1, ?, '')
                """,
                [
                    # 13:00 UTC, newer than the cutoff.
                    ("2026-11-14T08:00:00-05:00", SYSTEM_ID, "kept", "2026-11-14T08:00:00-05:00"),
                    # 11:00 UTC, older than the cutoff.
                    ("2026-11-14T20:00:00+09:00", SYSTEM_ID, "expired", "2026-11-14T20:00:00+09:00"),
                ],
            )
            connection.commit()

        result = _retain(self.store, hourly_rollup_retention_days=1, batch_size=1, max_batches=1)

        with closing(sqlite3.connect(self.path)) as connection:
            remaining = [row[0] for row in connection.execute("SELECT metric_name FROM metric_rollups")]
        self.assertEqual(remaining, ["kept"])
        self.assertEqual(result["hourly_rollups_removed"], 1)
        self.assertFalse(result["has_more"])

    def test_counter_rollup_keeps_the_absolutely_latest_value_across_offsets(self) -> None:
        # Both samples fall in the 10:00 UTC hour. The later one (10:55 UTC)
        # is stored with a -05:00 offset, so its text sorts first.
        self.store.insert_metric_samples(
            [
                _sample("2026-01-01T10:50:00+00:00", 100, metric_name="bytes_read"),
                _sample("2026-01-01T05:55:00-05:00", 250, metric_name="bytes_read"),
            ]
        )

        for batch_size in (100, 1):
            with self.subTest(batch_size=batch_size):
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("DELETE FROM metric_rollups")
                    connection.commit()
                if batch_size == 1:
                    # Re-seed the raw rows the first pass consumed.
                    self.store.insert_metric_samples(
                        [
                            _sample("2026-01-01T10:50:00+00:00", 100, metric_name="bytes_read"),
                            _sample("2026-01-01T05:55:00-05:00", 250, metric_name="bytes_read"),
                        ]
                    )
                _retain(
                    self.store,
                    raw_metric_retention_days=30,
                    hourly_rollup_retention_days=365,
                    daily_rollup_retention_days=365,
                    batch_size=batch_size,
                    max_batches=10,
                )
                with closing(sqlite3.connect(self.path)) as connection:
                    rollups = connection.execute(
                        "SELECT bucket_seconds, sample_count, last_value, last_observed_at "
                        "FROM metric_rollups ORDER BY bucket_seconds"
                    ).fetchall()
                self.assertEqual(
                    rollups,
                    [
                        (3600, 2, 250.0, "2026-01-01T05:55:00-05:00"),
                        (86400, 2, 250.0, "2026-01-01T05:55:00-05:00"),
                    ],
                )

    def test_a_row_whose_time_sqlite_cannot_read_is_never_expired(self) -> None:
        # +25:00 is outside the offsets SQLite reads, so the row's absolute
        # time is unknown. Retention keeps it instead of guessing from text.
        _insert_events(
            self.path,
            [
                ("2020-01-01T00:00:00+25:00", "unreadable"),
                ("2020-01-01T00:00:00+00:00", "expired-control"),
            ],
        )

        result = _retain(self.store, event_retention_days=1)

        self.assertEqual(_event_labels(self.path), ["unreadable"])
        self.assertEqual(result["events_removed"], 1)
        self.assertFalse(result["has_more"])

    def test_retention_batches_still_read_the_timestamp_index(self) -> None:
        # A guard, not a reproduction: the absolute-time predicate must keep
        # the batch on the timestamp index instead of sorting the table.
        cutoff = "2026-11-14T12:00:00+00:00"
        cases = (
            (HistoryStore._expired_rows_subquery("slot_events", "observed_at"), "idx_slot_events_observed_at"),
            (HistoryStore._expired_rows_subquery("metric_samples", "observed_at"), "idx_metric_samples_observed_at"),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            for query, index_name in cases:
                with self.subTest(index=index_name):
                    parameters = [cutoff] * query.count("?")
                    plan = " ".join(
                        str(row[3])
                        for row in connection.execute(f"EXPLAIN QUERY PLAN {query}", parameters)
                    )
                    self.assertIn(index_name, plan)
                    self.assertNotIn("USE TEMP B-TREE", plan)


if __name__ == "__main__":
    unittest.main()
