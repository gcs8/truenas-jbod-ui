"""History retention compares absolute instants, not timestamp text (#1204).

Migration, imports and history reads accept ISO-8601 timestamps with any UTC
offset. Retention must never expire a row whose absolute time is not older
than the cutoff, whatever offset it was stored with, and must keep distinct
microseconds apart. A row stored with a positive offset whose text still sorts
after the cutoff is expired on a later pass, never early, so a batch never
reads rows newer than the cutoff by text. One batch reads at most its size
plus the rows whose text lies in the day before the cutoff. All rows here are
synthetic and live in a temporary directory.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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
    # 11:00 UTC: older than the cutoff, but its text sorts after it, so it
    # waits for a later pass.
    ("2026-11-14T20:00:00+09:00", "deferred-positive-offset"),
    # 11:00 UTC, stored with a negative offset: a control that must go.
    ("2026-11-14T06:00:00-05:00", "expired-negative-offset"),
    # The widest offsets SQLite reads, one second either side of the cutoff.
    ("2026-11-15T02:58:59+14:59", "deferred-widest-positive-offset"),
    ("2026-11-13T21:01:01-14:59", "kept-widest-negative-offset"),
    ("2026-11-14T12:00:00+00:00", "kept-at-cutoff"),
    ("2026-11-14T11:59:59.999999+00:00", "expired-one-microsecond-before"),
    # More than a day before the cutoff by text: expired without a parse.
    ("2026-11-12T23:00:00-05:00", "expired-settled-negative-offset"),
    ("2026-11-14 13:00:00Z", "kept-space-separator"),
    ("2026-11-15T00:00:00+00:00", "kept-utc"),
]
KEPT_EVENTS = sorted(
    label for _, label in MIXED_OFFSET_EVENTS if not label.startswith("expired-")
)


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
        self.assertEqual(first["events_removed"] + rest["events_removed"], 4)
        self.assertFalse(rest["has_more"])
        self.assertEqual(_event_labels(self.path), KEPT_EVENTS)

    def test_remaining_row_check_sees_an_expired_offset_row_after_a_full_batch(self) -> None:
        _insert_events(
            self.path,
            [
                ("2026-11-14T08:00:00-05:00", "kept-negative-offset"),
                ("2026-11-14T06:00:00-05:00", "expired-negative-offset"),
                ("2026-11-14T10:00:00+00:00", "expired-utc"),
            ],
        )

        first = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=1)
        second = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=1)

        self.assertEqual((first["events_removed"], first["has_more"]), (1, True))
        self.assertEqual((second["events_removed"], second["has_more"]), (1, False))
        self.assertEqual(_event_labels(self.path), ["kept-negative-offset"])

    def test_a_positive_offset_row_expires_once_its_text_passes_the_cutoff(self) -> None:
        # 11:00 UTC, stored as 20:00+09:00.
        _insert_events(self.path, [("2026-11-14T20:00:00+09:00", "positive-offset")])

        deferred = _retain(self.store, event_retention_days=1)
        self.assertEqual(_event_labels(self.path), ["positive-offset"])
        self.assertEqual((deferred["events_removed"], deferred["has_more"]), (0, False))

        later = self.store.maintain_retention(
            now=datetime(2026, 11, 15, 20, 0, 1, tzinfo=timezone.utc),
            raw_metric_retention_days=0,
            event_retention_days=1,
            hourly_rollup_retention_days=0,
            daily_rollup_retention_days=0,
            batch_size=100,
            max_batches=10,
        )
        self.assertEqual(_event_labels(self.path), [])
        self.assertEqual(later["events_removed"], 1)

    def test_metric_retention_keeps_offset_samples_newer_than_the_cutoff(self) -> None:
        self.store.insert_metric_samples(
            [
                _sample("2026-11-14T08:00:00-05:00", 40),  # 13:00 UTC, kept
                _sample("2026-11-14T06:15:00-05:00", 30),  # 11:15 UTC, expired
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
                    ("2026-11-14T06:00:00-05:00", SYSTEM_ID, "expired", "2026-11-14T06:00:00-05:00"),
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

    def test_an_unreadable_time_is_kept_near_the_cutoff_and_expired_by_text_once_old(self) -> None:
        # +25:00 is not a valid offset, so the row's exact time is unknown.
        # Within the day before the cutoff it is kept; once its text is more
        # than a day older than the cutoff it is expired, as before #1204, so
        # unreadable rows never pile up in front of every batch.
        _insert_events(
            self.path,
            [
                ("2026-11-14T00:00:00+25:00", "unreadable-recent"),
                ("2020-01-01T00:00:00+25:00", "unreadable-old"),
                ("2020-01-01T00:00:00+00:00", "expired-control"),
            ],
        )

        result = _retain(self.store, event_retention_days=1)

        self.assertEqual(_event_labels(self.path), ["unreadable-recent"])
        self.assertEqual(result["events_removed"], 2)
        self.assertFalse(result["has_more"])

    def test_retention_batches_still_read_the_timestamp_index(self) -> None:
        # A guard, not a reproduction: the absolute-time predicate must keep
        # the batch on the timestamp index instead of sorting the table.
        cutoff = "2026-11-14T12:00:00+00:00"
        # (floor, index range): no floor on a pass's first batch, then one.
        floors = (
            (None, "(observed_at<?)"),
            (("2026-11-14T08:00:00+00:00", 5), "(observed_at>? AND observed_at<?)"),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            HistoryStore._register_retention_functions(connection)
            for table in ("slot_events", "metric_samples"):
                index_name = f"idx_{table}_observed_at"
                for floor, index_range in floors:
                    with self.subTest(index=index_name, floored=floor is not None):
                        query = HistoryStore._expired_rows_subquery(
                            table, "observed_at", floored=floor is not None
                        )
                        parameters = (*HistoryStore._retention_parameters(cutoff, floor), 10)
                        plan = " ".join(
                            str(row[3])
                            for row in connection.execute(f"EXPLAIN QUERY PLAN {query}", parameters)
                        )
                        self.assertIn(f"INDEX {index_name} {index_range}", plan)
                        self.assertNotIn("USE TEMP B-TREE", plan)


NEWER_MICROSECOND = "2026-11-14T10:00:00.000200+00:00"
OLDER_MICROSECOND = "2026-11-14T10:00:00.000100+00:00"
CUTOFF = "2026-11-14T12:00:00+00:00"


def _rollups(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT bucket_seconds, sample_count, last_value, last_observed_at "
            "FROM metric_rollups ORDER BY bucket_seconds"
        ).fetchall()


def _batch_work(path: Path, cutoffs: dict[str, str | None]) -> tuple[int, dict]:
    """Run one real retention batch of size 1 and count SQLite VM steps."""

    with closing(sqlite3.connect(path)) as connection:
        steps = [0]

        def progress() -> int:
            steps[0] += 1
            return 0

        connection.set_progress_handler(progress, 1)
        result = HistoryStore._maintain_retention_batch(
            connection,
            cutoffs={"metric": None, "event": None, "hourly": None, "daily": None, **cutoffs},
            batch_size=1,
        )
        connection.set_progress_handler(None, 0)
        connection.rollback()
    return steps[0], result


class RetentionPrecisionTests(unittest.TestCase):
    """Two samples in the same SQLite millisecond stay in their true order."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-precision-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        self.store = HistoryStore(str(self.path))

    def _retain_counters(self, batch_size: int = 100) -> dict:
        return _retain(
            self.store,
            raw_metric_retention_days=1,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=365,
            batch_size=batch_size,
        )

    def _expected(self) -> list[tuple]:
        return [
            (3600, 2, 200.0, NEWER_MICROSECOND),
            (86400, 2, 200.0, NEWER_MICROSECOND),
        ]

    def test_counter_rollup_keeps_the_later_microsecond_sample_in_one_batch(self) -> None:
        self.store.insert_metric_samples(
            [
                _sample(NEWER_MICROSECOND, 200, metric_name="bytes_read"),
                _sample(OLDER_MICROSECOND, 100, metric_name="bytes_read"),
            ]
        )

        self._retain_counters()

        self.assertEqual(_rollups(self.path), self._expected())

    def test_counter_rollup_keeps_the_later_microsecond_sample_in_one_row_batches(self) -> None:
        self.store.insert_metric_samples(
            [
                _sample(NEWER_MICROSECOND, 200, metric_name="bytes_read"),
                _sample(OLDER_MICROSECOND, 100, metric_name="bytes_read"),
            ]
        )

        self._retain_counters(batch_size=1)

        self.assertEqual(_rollups(self.path), self._expected())

    def test_counter_rollup_merge_keeps_the_later_sample_when_an_older_one_arrives_late(self) -> None:
        self.store.insert_metric_samples([_sample(NEWER_MICROSECOND, 200, metric_name="bytes_read")])
        self._retain_counters()
        self.store.insert_metric_samples([_sample(OLDER_MICROSECOND, 100, metric_name="bytes_read")])

        self._retain_counters()

        self.assertEqual(_rollups(self.path), self._expected())

    def test_an_event_one_hundred_microseconds_before_a_fractional_cutoff_expires(self) -> None:
        _insert_events(
            self.path,
            [
                ("2026-11-14T12:00:00.000000+00:00", "expired"),
                ("2026-11-14T12:00:00.000100+00:00", "kept-at-cutoff"),
            ],
        )

        result = self.store.maintain_retention(
            now=NOW.replace(microsecond=100),
            raw_metric_retention_days=0,
            event_retention_days=1,
            hourly_rollup_retention_days=0,
            daily_rollup_retention_days=0,
            batch_size=100,
            max_batches=10,
        )

        self.assertEqual(_event_labels(self.path), ["kept-at-cutoff"])
        self.assertEqual(result["events_removed"], 1)
        self.assertFalse(result["has_more"])


class RetentionBatchWorkTests(unittest.TestCase):
    """Where one retention batch's work comes from.

    Rows whose text sorts at or after the cutoff are never visited. Rows whose
    text sorts before the settled bound (the day before the cutoff) are taken
    in index order. So one batch visits at most its size plus the rows whose
    text lies in the day before the cutoff; only those window rows grow it.
    """

    SMALL = 200
    LARGE = 4000
    # The unchanged text-order implementation used the same number of steps
    # at both sizes; allow a little slack for B-tree depth.
    SLACK = 100

    def _database(self, name: str) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="retention-work-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / name
        HistoryStore(str(path))
        return path

    def _fill(self, path: Path, observed_at: str, count: int, *, metrics: bool) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.executemany(
                "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, "
                "slot_label, event_type, details_json) "
                "VALUES (?, 'synthetic-system', '', 0, '0', 'slot_state_changed', '{}')",
                ((observed_at,) for _ in range(count)),
            )
            if metrics:
                connection.executemany(
                    "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, "
                    "slot_label, metric_name, value_integer) "
                    "VALUES (?, 'synthetic-system', '', ?, '0', 'bytes_read', 1)",
                    ((observed_at, index) for index in range(count)),
                )
                connection.executemany(
                    "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, "
                    "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                    "value_min, value_max, last_value, last_observed_at) "
                    "VALUES (?, 3600, 'synthetic-system', '', ?, '0', 'bytes_read', 1, 1, 1, 1, 1, ?)",
                    ((observed_at, index, observed_at) for index in range(count)),
                )
            connection.commit()

    def test_a_batch_with_no_expired_rows_does_not_read_every_newer_row(self) -> None:
        cutoffs = {"metric": CUTOFF, "event": CUTOFF, "hourly": CUTOFF, "daily": CUTOFF}
        work = {}
        for count in (self.SMALL, self.LARGE):
            path = self._database(f"newer-{count}.db")
            # One hour newer than the cutoff, canonical UTC.
            self._fill(path, "2026-11-14T13:00:00+00:00", count, metrics=True)
            work[count], result = _batch_work(path, cutoffs)
            self.assertEqual(result["metric_samples_removed"], 0)
            self.assertEqual(result["events_removed"], 0)
            self.assertEqual(result["hourly_rollups_removed"], 0)
            self.assertFalse(result["has_more"])

        self.assertLessEqual(work[self.LARGE] - work[self.SMALL], self.SLACK, work)

    def test_old_unreadable_rows_do_not_grow_the_work_of_a_batch(self) -> None:
        work = {}
        for count in (self.SMALL, self.LARGE):
            path = self._database(f"unreadable-{count}.db")
            self._fill(path, "0000-invalid", count, metrics=False)
            work[count], result = _batch_work(path, {"event": CUTOFF})
            self.assertEqual(result["events_removed"], 1)
            self.assertTrue(result["has_more"])

        self.assertLessEqual(work[self.LARGE] - work[self.SMALL], self.SLACK, work)

    def test_old_rows_beyond_the_batch_size_do_not_grow_the_work_of_a_batch(self) -> None:
        cutoffs = {"metric": CUTOFF, "event": CUTOFF, "hourly": CUTOFF, "daily": CUTOFF}
        work = {}
        for count in (self.SMALL, self.LARGE):
            path = self._database(f"old-{count}.db")
            self._fill(path, "2026-11-01T00:00:00+00:00", count, metrics=True)
            work[count], result = _batch_work(path, cutoffs)
            self.assertEqual(result["metric_samples_removed"], 1)
            self.assertEqual(result["events_removed"], 1)
            self.assertEqual(result["hourly_rollups_removed"], 1)
            self.assertTrue(result["has_more"])

        self.assertLessEqual(work[self.LARGE] - work[self.SMALL], self.SLACK, work)

    def test_only_rows_in_the_day_before_the_cutoff_grow_the_work_of_a_batch(self) -> None:
        # Valid rows newer than the cutoff whose -05:00 text sorts before it,
        # and recent rows whose time cannot be read, both sit in the window.
        # A batch reads each of them once, so its work grows linearly with the
        # window rows. That is the documented bound, not a size-only bound.
        cases = (
            ("negative-offset", "2026-11-14T08:00:00-05:00", True,
             {"metric": CUTOFF, "event": CUTOFF, "hourly": CUTOFF, "daily": CUTOFF}),
            ("recent-unreadable", "2026-11-14T00:00:00+25:00", False, {"event": CUTOFF}),
        )
        for name, observed_at, metrics, cutoffs in cases:
            with self.subTest(name):
                work = {}
                for count in (self.SMALL, self.LARGE):
                    path = self._database(f"{name}-{count}.db")
                    self._fill(path, observed_at, count, metrics=metrics)
                    work[count], result = _batch_work(path, cutoffs)
                    self.assertEqual(result["events_removed"], 0)
                    self.assertFalse(result["has_more"])
                growth = work[self.LARGE] / work[self.SMALL]
                self.assertGreater(growth, 5, work)
                self.assertLessEqual(growth, 1.1 * self.LARGE / self.SMALL, work)

    def test_a_full_pass_reads_each_window_row_a_bounded_number_of_times(self) -> None:
        # Expired UTC rows interleaved with -05:00 rows that are newer than the
        # cutoff, all inside the window, deleted ten rows per batch. Without a
        # lower bound for the pass, every batch re-reads the rejected rows
        # before it and the pass grows with the square of the window.
        work = {}
        for count in (200, 800):
            path = self._database(f"interleaved-{count}.db")
            store = HistoryStore(str(path))
            _insert_events(path, _interleaved_window_events(count))
            steps = _count_store_work(store)
            result = store.maintain_retention(
                now=NOW,
                raw_metric_retention_days=0,
                event_retention_days=1,
                hourly_rollup_retention_days=0,
                daily_rollup_retention_days=0,
                batch_size=10,
                max_batches=1000,
            )
            work[count] = steps[0]
            self.assertEqual(result["events_removed"], count)
            self.assertFalse(result["has_more"])
            self.assertEqual(_event_labels(path), ["kept"] * count)

        self.assertLessEqual(work[800] / work[200], 1.5 * 800 / 200, work)


def _interleaved_window_events(count: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for index in range(count):
        minute, second = divmod(index, 60)
        moment = f"2026-11-14T08:{minute % 60:02d}:{second:02d}"
        rows.append((f"{moment}+00:00", "expired"))  # 08:xx UTC
        rows.append((f"{moment}-05:00", "kept"))  # 13:xx UTC
    return rows


def _count_store_work(store: HistoryStore) -> list[int]:
    """Count SQLite VM steps on every connection the store opens from now on."""

    steps = [0]
    connect = store._connect_locked

    def counting_connect() -> sqlite3.Connection:
        connection = connect()

        def progress() -> int:
            steps[0] += 1
            return 0

        connection.set_progress_handler(progress, 1)
        return connection

    store._connect_locked = counting_connect  # type: ignore[method-assign]
    return steps


class RetentionScanFloorTests(unittest.TestCase):
    """The pass's in-memory lower bound can only delay a deletion."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-floor-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        self.store = HistoryStore(str(self.path))

    def test_a_row_added_behind_the_floor_waits_for_the_next_pass(self) -> None:
        _insert_events(self.path, _interleaved_window_events(30))
        checks = [0]

        def add_old_row_after_first_batch() -> bool:
            checks[0] += 1
            if checks[0] == 2:
                # Expired, and sorts before every row the first batch read.
                _insert_events(self.path, [("2026-11-14T07:00:00+00:00", "late-expired")])
            return True

        first = self.store.maintain_retention(
            now=NOW,
            raw_metric_retention_days=0,
            event_retention_days=1,
            hourly_rollup_retention_days=0,
            daily_rollup_retention_days=0,
            batch_size=10,
            max_batches=100,
            should_continue=add_old_row_after_first_batch,
        )
        self.assertEqual(first["events_removed"], 30)
        self.assertIn("late-expired", _event_labels(self.path))

        second = _retain(self.store, event_retention_days=1)

        self.assertEqual(second["events_removed"], 1)
        self.assertFalse(second["has_more"])
        self.assertEqual(_event_labels(self.path), ["kept"] * 30)

    def test_rollups_cover_exactly_the_samples_deleted_across_floored_batches(self) -> None:
        samples = []
        for index in range(25):
            minute, second = divmod(index, 60)
            moment = f"2026-11-14T08:{minute:02d}:{second:02d}"
            samples.append(_sample(f"{moment}+00:00", 1))  # expired
            samples.append(_sample(f"{moment}-05:00", 2))  # newer than the cutoff
        self.store.insert_metric_samples(samples)

        result = _retain(
            self.store,
            raw_metric_retention_days=1,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=365,
            batch_size=4,
            max_batches=100,
        )

        with closing(sqlite3.connect(self.path)) as connection:
            remaining = [row[0] for row in connection.execute("SELECT value_integer FROM metric_samples")]
            rolled = connection.execute(
                "SELECT bucket_seconds, SUM(sample_count), SUM(value_sum) FROM metric_rollups "
                "GROUP BY bucket_seconds ORDER BY bucket_seconds"
            ).fetchall()
        self.assertEqual(result["metric_samples_removed"], 25)
        self.assertFalse(result["has_more"])
        self.assertEqual(remaining, [2] * 25)
        self.assertEqual(rolled, [(3600, 25, 25.0), (86400, 25, 25.0)])

    def _late_raw_sample_case(self, *, stop_after_insert: bool) -> None:
        # Expired samples between newer -05:00 samples move the floor past the
        # start of the table. After the first batch, an expired sample with
        # its own metric arrives behind the floor. It must stay unrolled until
        # the next pass, and that pass must roll it exactly once per interval.
        samples = []
        for index in range(25):
            moment = f"2026-11-14T08:00:{index:02d}"
            samples.append(_sample(f"{moment}+00:00", 1))  # expired
            samples.append(_sample(f"{moment}-05:00", 2))  # newer than the cutoff
        self.store.insert_metric_samples(samples)
        checks = [0]

        def add_late_sample_after_first_batch() -> bool:
            checks[0] += 1
            if checks[0] == 2:
                self.store.insert_metric_samples(
                    [_sample("2026-11-14T07:00:00+00:00", 7, metric_name="late_metric")]
                )
            # The stopped variant runs one more floored batch, then stops.
            return not (stop_after_insert and checks[0] == 3)

        first = self.store.maintain_retention(
            now=NOW,
            raw_metric_retention_days=1,
            event_retention_days=0,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=365,
            batch_size=4,
            max_batches=100,
            should_continue=add_late_sample_after_first_batch,
        )
        self.assertEqual(first["interrupted"], stop_after_insert)
        if stop_after_insert:
            self.assertEqual(first["batches_completed"], 2)
        self.assertEqual(self._late_rollups(), [])
        self.assertEqual(self._late_samples(), [7])

        for _ in range(2):
            later = _retain(
                self.store,
                raw_metric_retention_days=1,
                hourly_rollup_retention_days=365,
                daily_rollup_retention_days=365,
                batch_size=4,
                max_batches=100,
            )
            self.assertFalse(later["has_more"])
            self.assertEqual(self._late_samples(), [])
            self.assertEqual(self._late_rollups(), [(3600, 1, 7.0), (86400, 1, 7.0)])
        with closing(sqlite3.connect(self.path)) as connection:
            rolled = connection.execute(
                "SELECT bucket_seconds, SUM(sample_count), SUM(value_sum) FROM metric_rollups "
                "WHERE metric_name = 'temperature_c' GROUP BY bucket_seconds ORDER BY bucket_seconds"
            ).fetchall()
            remaining = [row[0] for row in connection.execute("SELECT value_integer FROM metric_samples")]
        self.assertEqual(rolled, [(3600, 25, 25.0), (86400, 25, 25.0)])
        self.assertEqual(remaining, [2] * 25)

    def _late_samples(self) -> list[int]:
        with closing(sqlite3.connect(self.path)) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT value_integer FROM metric_samples WHERE metric_name = 'late_metric'"
                )
            ]

    def _late_rollups(self) -> list[tuple]:
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT bucket_seconds, sample_count, value_sum FROM metric_rollups "
                "WHERE metric_name = 'late_metric' ORDER BY bucket_seconds"
            ).fetchall()

    def test_a_raw_sample_added_behind_the_floor_is_rolled_up_once_on_the_next_pass(self) -> None:
        self._late_raw_sample_case(stop_after_insert=False)

    def test_a_raw_sample_added_behind_the_floor_of_a_stopped_pass_is_rolled_up_once(self) -> None:
        self._late_raw_sample_case(stop_after_insert=True)


# The smallest key SQLite stores, with empty timestamp text: a legal row that
# no artificial "before everything" floor can precede.
MINIMUM_KEY = -(2**63)


class RetentionFirstBatchHasNoFloorTests(unittest.TestCase):
    """Before a pass has a floor, no lower bound at all applies to a table."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-nofloor-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        self.store = HistoryStore(str(self.path))

    def _assert_both_keys_expire(self, table: str) -> None:
        first = _retain(
            self.store,
            event_retention_days=1,
            hourly_rollup_retention_days=1,
            daily_rollup_retention_days=1,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            left = connection.execute(f"SELECT rowid FROM {table} ORDER BY rowid").fetchall()
        self.assertEqual(left, [])
        self.assertEqual(first["total_rows_removed"], 2)
        self.assertFalse(first["has_more"])

    def test_the_minimum_event_key_with_empty_time_text_expires(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO slot_events (id, observed_at, system_id, enclosure_key, slot, "
                "slot_label, event_type, details_json) "
                "VALUES (?, '', 'synthetic-system', '', 0, '0', 'slot_state_changed', '{}')",
                [(MINIMUM_KEY,), (MINIMUM_KEY + 1,)],
            )
            connection.commit()

        self._assert_both_keys_expire("slot_events")

    def test_the_minimum_rollup_key_with_empty_time_text_expires_in_both_intervals(self) -> None:
        for bucket_seconds in (3600, 86400):
            with self.subTest(bucket_seconds=bucket_seconds):
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute("DELETE FROM metric_rollups")
                    connection.executemany(
                        "INSERT INTO metric_rollups (rowid, bucket_start, bucket_seconds, system_id, "
                        "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                        "value_min, value_max, last_value, last_observed_at) "
                        "VALUES (?, '', ?, 'synthetic-system', '', ?, '0', 'bytes_read', 1, 1, 1, 1, 1, '')",
                        [(MINIMUM_KEY, bucket_seconds, 0), (MINIMUM_KEY + 1, bucket_seconds, 1)],
                    )
                    connection.commit()

                self._assert_both_keys_expire("metric_rollups")


REJECTED_WINDOW_TEXT = "2026-11-14T08:00:00-05:00"  # 13:00 UTC, newer than the cutoff
OLD_TEXT = "2026-11-01T00:00:00+00:00"


class RetentionCrossTableWorkTests(unittest.TestCase):
    """A table with nothing left to delete is not read again while others run.

    One table holds only window rows the exact-time test rejects; another
    holds an old backlog that keeps the pass running for many batches. Four
    times the rows may cost about four times the exact-time parses, not
    sixteen: the rejected rows are not re-read in every batch the backlog
    drives.
    """

    def _insert(self, path: Path, population: str, observed_at: str, count: int) -> None:
        with closing(sqlite3.connect(path)) as connection:
            if population == "events":
                connection.executemany(
                    "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, "
                    "slot_label, event_type, details_json) "
                    "VALUES (?, 'synthetic-system', '', 0, '0', 'slot_state_changed', '{}')",
                    ((observed_at,) for _ in range(count)),
                )
            elif population == "metrics":
                connection.executemany(
                    "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, "
                    "slot_label, metric_name, value_integer) "
                    "VALUES (?, 'synthetic-system', '', ?, '0', 'bytes_read', 1)",
                    ((observed_at, index) for index in range(count)),
                )
            else:
                connection.executemany(
                    "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, "
                    "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                    "value_min, value_max, last_value, last_observed_at) "
                    "VALUES (?, ?, 'synthetic-system', '', ?, '0', 'bytes_read', 1, 1, 1, 1, 1, ?)",
                    ((observed_at, int(population), index, observed_at) for index in range(count)),
                )
            connection.commit()

    @staticmethod
    def _count(path: Path, population: str) -> int:
        with closing(sqlite3.connect(path)) as connection:
            if population == "events":
                return connection.execute("SELECT count(*) FROM slot_events").fetchone()[0]
            if population == "metrics":
                return connection.execute("SELECT count(*) FROM metric_samples").fetchone()[0]
            return connection.execute(
                "SELECT count(*) FROM metric_rollups WHERE bucket_seconds = ?", (int(population),)
            ).fetchone()[0]

    def test_rejected_window_rows_are_not_reread_while_another_table_has_a_backlog(self) -> None:
        cases = (
            # (rejected population, backlog population, retention days per kind)
            ("events", "3600", {"event_retention_days": 1, "hourly_rollup_retention_days": 1}),
            ("metrics", "events", {"raw_metric_retention_days": 1, "event_retention_days": 1}),
            ("3600", "events", {"event_retention_days": 1, "hourly_rollup_retention_days": 1}),
            ("86400", "events", {"event_retention_days": 1, "daily_rollup_retention_days": 1}),
        )
        from history_service import store as store_module

        original = store_module._history_utc_instant
        for rejected, backlog, days in cases:
            with self.subTest(rejected=rejected, backlog=backlog):
                calls: dict[int, int] = {}
                for count in (200, 800):
                    with tempfile.TemporaryDirectory(prefix="retention-cross-") as temporary:
                        path = Path(temporary) / "history.db"
                        store = HistoryStore(str(path))
                        self._insert(path, rejected, REJECTED_WINDOW_TEXT, count)
                        self._insert(path, backlog, OLD_TEXT, count)
                        parsed = [0]

                        def counting(value: object) -> str | None:
                            if value == REJECTED_WINDOW_TEXT:
                                parsed[0] += 1
                            return original(value)

                        with mock.patch.object(store_module, "_history_utc_instant", counting):
                            result = _retain(store, batch_size=10, max_batches=1000, **days)
                        calls[count] = parsed[0]
                        # Once per pass; raw samples once for each rollup
                        # interval and once for the delete.
                        self.assertLessEqual(parsed[0], 3 * count, calls)
                        self.assertFalse(result["has_more"])
                        self.assertGreaterEqual(result["batches_completed"], count // 10)
                        self.assertEqual(self._count(path, rejected), count)
                        self.assertEqual(self._count(path, backlog), 0)
                self.assertLessEqual(calls[800] / calls[200], 1.5 * 800 / 200, calls)


class RetentionQueryPlanTests(unittest.TestCase):
    """Every retention statement reads the tables through the timestamp index."""

    TABLES = ("metric_samples", "slot_events", "metric_rollups")

    def test_every_retention_statement_searches_the_timestamp_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="retention-plan-") as temporary:
            path = Path(temporary) / "history.db"
            store = HistoryStore(str(path))
            _insert_events(path, _interleaved_window_events(12))
            store.insert_metric_samples(
                [_sample(observed_at, 1) for observed_at, _ in _interleaved_window_events(12)]
            )
            statements: list[str] = []
            connect = store._connect_locked

            def tracing_connect() -> sqlite3.Connection:
                connection = connect()
                connection.set_trace_callback(statements.append)
                return connection

            store._connect_locked = tracing_connect  # type: ignore[method-assign]
            _retain(
                store,
                raw_metric_retention_days=1,
                event_retention_days=1,
                hourly_rollup_retention_days=1,
                daily_rollup_retention_days=1,
                batch_size=5,
                max_batches=10,
            )
            retention = [
                sql for sql in statements
                if "_history_utc_instant" in sql and not sql.lstrip().upper().startswith("EXPLAIN")
            ]
            kinds = {" ".join(sql.split()[:3]) for sql in retention}
            self.assertGreaterEqual(len(retention), 9 * 2, kinds)
            with closing(sqlite3.connect(path)) as connection:
                HistoryStore._register_retention_functions(connection)
                for sql in retention:
                    plan = [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}")]
                    with self.subTest(statement=" ".join(sql.split())[:60]):
                        for line in plan:
                            for table in self.TABLES:
                                self.assertFalse(line.startswith(f"SCAN {table}"), plan)
                        searches = [line for line in plan if line.startswith("SEARCH")]
                        self.assertTrue(
                            any(
                                "idx_metric_samples_observed_at (observed_at" in line
                                or "idx_slot_events_observed_at (observed_at" in line
                                or "idx_metric_rollups_retention (bucket_seconds=? AND bucket_start" in line
                                for line in searches
                            ),
                            plan,
                        )
                        if not sql.lstrip().upper().startswith("INSERT"):
                            # The rollup insert sorts only the selected batch.
                            self.assertFalse(any("TEMP B-TREE" in line for line in plan), plan)


if __name__ == "__main__":
    unittest.main()
