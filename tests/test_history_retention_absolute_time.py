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
    ENCODING = "UTF-8"

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

    def _insert_native(self, rows: list[object]) -> None:
        # bytes are the database's own text bytes, written as a BLOB literal
        # cast to TEXT, which SQLite reads in the database's encoding.
        with closing(sqlite3.connect(self.path)) as connection:
            for slot, observed_at in enumerate(rows):
                value = f"CAST(x'{observed_at.hex()}' AS TEXT)" if isinstance(observed_at, bytes) else "?"
                connection.execute(
                    "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, slot_label, "
                    f"event_type, details_json) VALUES ({value}, ?, '', ?, '0', 'slot_state_changed', '{{}}')",
                    (*(() if isinstance(observed_at, bytes) else (observed_at,)), SYSTEM_ID, slot),
                )
            connection.commit()

    def _event_batches(self, connection: sqlite3.Connection) -> list[tuple[int, int | None]]:
        # (rows removed, key of the scan floor) for each batch of one row.
        floors: dict = {}
        finished: set[str] = set()
        batches: list[tuple[int, int | None]] = []
        for _ in range(20):
            result = HistoryStore._maintain_retention_batch(
                connection,
                cutoffs={"metric": None, "event": CUTOFF, "hourly": None, "daily": None},
                batch_size=1,
                scan_floors=floors,
                finished_tables=frozenset(finished),
            )
            connection.commit()
            floors = result["scan_floors"]
            finished.update(result["finished_tables"])
            batches.append((result["events_removed"], floors.get("slot_events", (None, None))[1]))
            if not result["has_more"]:
                return batches
        self.fail(f"the pass did not end: {batches}")

    def test_the_scan_floor_names_the_stored_text_of_the_row_it_came_from(self) -> None:
        # The floor a batch keeps, bound into the statement the next batch
        # runs, must equal its own row's stored text: that row is not after
        # its floor, and the next row with the same text and a larger key is.
        values: list[object] = [
            "2026-10-01T00:00:00+00:00",
            "2026-10-01T00:00:00+00:00\u0100",
            "2026-10-01T00:00:00+00:00\U00010000",
            "2026-10-01T00:00:00+00:00\x00z",
        ]
        if self.ENCODING == "UTF-8":
            values.append(b"2026-10-01T00:00:00+00:0\xff")
        for value in values:
            with self.subTest(value=value), closing(sqlite3.connect(self.path)) as connection:
                connection.execute("DELETE FROM slot_events")
                connection.commit()
                self._insert_native([value, value, "2026-10-02T00:00:00+00:00"])
                first = connection.execute("SELECT min(id) FROM slot_events").fetchone()[0]
                result = HistoryStore._maintain_retention_batch(
                    connection,
                    cutoffs={"metric": None, "event": CUTOFF, "hourly": None, "daily": None},
                    batch_size=1,
                )
                connection.rollback()
                floor = result["scan_floors"].get("slot_events", (None, None))
                self.assertEqual(floor[1], first)
                after = connection.execute(
                    HistoryStore._expired_rows_subquery("slot_events", "observed_at", floored=True),
                    (*HistoryStore._retention_parameters(CUTOFF, floor), 10),
                ).fetchall()
                self.assertEqual([row[0] for row in after], [first + 1, first + 2])

    def test_a_full_pass_reads_rejected_window_rows_as_often_in_every_encoding(self) -> None:
        # Rows in the day before the cutoff whose exact time is not expired,
        # with mixed offsets, between expired rows; one row per batch. The
        # floor starts each batch after the last row deleted, so each rejected
        # row is parsed the same small number of times in every encoding.
        from history_service import store as store_module

        rejected = [
            text for minute in range(10) for text in (
                f"2026-11-13T23:{minute:02d}:00-14:00",  # 13:xx UTC on the 14th
                f"2026-11-14T08:{minute:02d}:00-05:00",  # 13:xx UTC
                f"2026-11-14T11:{minute:02d}:00-01:00",  # 12:xx UTC, at or after the cutoff
            )
        ]
        expired = [
            text for minute in range(10) for text in (
                f"2026-11-14T06:{minute:02d}:00-05:00",  # 11:xx UTC
                f"2026-11-14T10:{minute:02d}:00+00:00",
            )
        ]
        _insert_events(self.path, [(text, "kept") for text in rejected] + [(text, "expired") for text in expired])
        original = store_module._history_utc_instant
        parsed = [0]

        def counting(value: object) -> str | None:
            if value in rejected:
                parsed[0] += 1
            return original(value)

        with mock.patch.object(store_module, "_history_utc_instant", counting):
            result = _retain(self.store, event_retention_days=1, batch_size=1, max_batches=100)

        self.assertEqual(result["events_removed"], len(expired))
        self.assertFalse(result["has_more"])
        self.assertEqual(_event_labels(self.path), ["kept"] * len(rejected))
        self.assertEqual(parsed[0], 40)

    def test_a_floor_that_cannot_be_named_stays_lower_and_the_pass_still_ends(self) -> None:
        # Text bytes the database's encoding cannot decode (a lone surrogate
        # in UTF-16) cannot be bound back as the same text there, so a batch
        # that deletes such a row keeps the previous floor and the next batch
        # reads from it again. In UTF-8 the bytes themselves name the text.
        bad = {"UTF-8": b"\xff", "UTF-16le": b"\x00\xd8", "UTF-16be": b"\xd8\x00"}[self.ENCODING]
        rows: list[object] = [
            "2026-10-01T00:00:00+00:00",
            *((f"2026-10-03{letter}".encode(self.ENCODING) + bad) for letter in "abc"),
            "2026-10-05T00:00:00+00:00",
            "2026-11-14T00:00:00".encode(self.ENCODING) + bad,  # in the window: kept
            "2026-11-14T08:00:00-05:00",  # 13:00 UTC: kept
            "2026-11-14T10:00:00+00:00",
        ]
        self._insert_native(rows)
        with closing(sqlite3.connect(self.path)) as connection:
            first = connection.execute("SELECT min(id) FROM slot_events").fetchone()[0]
            batches = self._event_batches(connection)
            left = [row[0] - first for row in connection.execute("SELECT id FROM slot_events ORDER BY id")]
        named = self.ENCODING == "UTF-8"
        floor_keys = [0, 1 if named else 0, 2 if named else 0, 3 if named else 0, 4, 7]
        self.assertEqual(batches, [(1, first + key) for key in floor_keys])
        self.assertEqual(left, [5, 6])


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


class HotRetentionAbsoluteTimeUtf16Tests(HotRetentionAbsoluteTimeTests):
    """The same rules in a database whose text encoding is UTF-16.

    Retention hands stored text to the exact-time test as raw bytes, which
    are in the database's own encoding, not always UTF-8.
    """

    ENCODING = "UTF-16le"

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-utf16-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(f"PRAGMA encoding = '{self.ENCODING}'")
            connection.execute("CREATE TABLE encoding_marker (value)")
            connection.commit()
        self.store = HistoryStore(str(self.path))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA encoding").fetchone()[0], self.ENCODING)


class HotRetentionAbsoluteTimeUtf16BigEndianTests(HotRetentionAbsoluteTimeUtf16Tests):
    ENCODING = "UTF-16be"


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


HOURLY_CUTOFF_TWO_DAYS = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
# Raw text order is not bucket order: the first sample (13:00 UTC) rolls into
# an hourly bucket newer than a two-day hourly cutoff, the second (09:00 UTC)
# into one already older than it.
PASS_WRITTEN_SAMPLES = (("2026-11-13T08:00:00-05:00", 13), ("2026-11-13T09:00:00+00:00", 9))


def _expired_by_absolute_time(path: Path, bucket_seconds: int, cutoff: datetime) -> list[str]:
    """Rollup buckets older than the cutoff, read in Python, not by the store."""

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT bucket_start FROM metric_rollups WHERE bucket_seconds = ?", (bucket_seconds,)
        ).fetchall()
    return sorted(
        text for (text,) in rows if datetime.fromisoformat(text).astimezone(timezone.utc) < cutoff
    )


class RetentionPassWrittenRollupTests(unittest.TestCase):
    """has_more=false means no row this pass knows about is still eligible.

    A rollup the pass itself writes is known to it. When its bucket is already
    past that rollup table's cutoff, the same pass removes it, whether the
    table had nothing left to delete (finished) or the bucket lies behind the
    table's scan floor. If the batch budget ends first, has_more is true. The
    collector's next-due answer, the segmented backup authorization and the
    dashboard line all follow from that one flag.
    """

    RETENTION = {"raw_metric_retention_days": 1, "hourly_rollup_retention_days": 2}

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-written-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.path = self.root / "history.db"
        self.store = HistoryStore(str(self.path))

    def _insert_samples(self, samples=PASS_WRITTEN_SAMPLES) -> None:
        self.store.insert_metric_samples([_sample(text, value) for text, value in samples])

    def _expired_hourly(self) -> list[str]:
        return _expired_by_absolute_time(self.path, 3600, HOURLY_CUTOFF_TWO_DAYS)

    def test_an_expired_rollup_written_into_a_finished_table_is_removed_in_the_same_pass(self) -> None:
        self._insert_samples()
        result = _retain(self.store, batch_size=1, max_batches=100, **self.RETENTION)
        self.assertEqual(self._expired_hourly(), [])
        self.assertEqual(result["metric_samples_removed"], 2)
        self.assertEqual(result["hourly_rollups_removed"], 1)
        self.assertEqual(result["total_rows_removed"], 3)
        self.assertFalse(result["has_more"])
        self.assertFalse(result["interrupted"])
        with closing(sqlite3.connect(self.path)) as connection:
            kept = connection.execute(
                "SELECT bucket_start, sample_count, value_sum FROM metric_rollups "
                "WHERE bucket_seconds = 3600"
            ).fetchall()
        # The newer bucket stays, rolled up exactly once.
        self.assertEqual(kept, [("2026-11-13T13:00:00+00:00", 1, 13.0)])

    def test_an_expired_rollup_written_behind_the_scan_floor_is_removed_in_the_same_pass(self) -> None:
        # Batch 1 deletes an old hourly bucket, which sets the hourly floor.
        # Batch 2 writes an expired bucket (19:00 UTC) whose text sorts before
        # that floor, so a scan that starts after the floor cannot see it.
        # Control: the written bucket has the floor's own text and a newer key.
        cases = {
            "behind the floor": ("2026-11-12T20:00:00+00:00", "2026-11-13T09:00:00+14:00"),
            "floor text, newer key (control)": (
                "2026-11-12T19:00:00+00:00", "2026-11-13T09:00:00+14:00",
            ),
        }
        helper = RetentionCrossTableWorkTests()
        for label, (old_bucket, late_sample) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory(prefix="retention-floor-") as temporary:
                path = Path(temporary) / "history.db"
                store = HistoryStore(str(path))
                helper._insert(path, "3600", old_bucket, 1)
                store.insert_metric_samples(
                    [_sample("2026-11-13T08:00:00-05:00", 13), _sample(late_sample, 9)]
                )
                result = _retain(store, batch_size=1, max_batches=100, **self.RETENTION)
                self.assertEqual(_expired_by_absolute_time(path, 3600, HOURLY_CUTOFF_TWO_DAYS), [])
                self.assertEqual(result["metric_samples_removed"], 2)
                self.assertEqual(result["hourly_rollups_removed"], 2)
                self.assertFalse(result["has_more"])

    def test_a_budget_too_small_to_finish_reports_more_work(self) -> None:
        # Old hourly buckets beyond the budget, plus the pass-written expired one.
        RetentionCrossTableWorkTests()._insert(self.path, "3600", OLD_TEXT, 3)
        self._insert_samples()
        first = _retain(self.store, batch_size=1, max_batches=2, **self.RETENTION)
        self.assertTrue(first["has_more"])
        self.assertTrue(self._expired_hourly())
        second = _retain(self.store, batch_size=1, max_batches=100, **self.RETENTION)
        self.assertFalse(second["has_more"])
        self.assertEqual(self._expired_hourly(), [])
        self.assertEqual(first["hourly_rollups_removed"] + second["hourly_rollups_removed"], 4)

    def _collector(self, *, segmented: bool, max_batches: int):
        from history_service.collector import HistoryCollector
        from history_service.config import HistorySettings

        settings = HistorySettings(
            sqlite_path=str(self.path),
            segment_catalog_path=str(self.root / "segments" / "catalog.json") if segmented else None,
            raw_metric_retention_days=1,
            event_retention_days=0,
            hourly_rollup_retention_days=2,
            daily_rollup_retention_days=0,
            retention_batch_size=1,
            retention_max_batches_per_run=max_batches,
            retention_interval_seconds=3600,
        )
        return HistoryCollector(settings, self.store)

    def _authorization_state(self) -> str | None:
        from history_service.store import SEGMENTED_RETENTION_STATE_NAME

        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT state FROM history_maintenance_state WHERE name = ?",
                (SEGMENTED_RETENTION_STATE_NAME,),
            ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _rendered_pending(collector) -> str:
        from unittest.mock import MagicMock

        from history_service import main as history_main

        context = history_main.build_dashboard_context(
            request=MagicMock(),
            status=history_main.public_collector_status(collector.status()),
            counts={},
            scopes=[],
            app_version="test",
        )
        html = history_main.templates.get_template("dashboard.html").render(context)
        marker = 'id="status-last-retention-has-more">'
        start = html.index(marker) + len(marker)
        return html[start:html.index("</dd>", start)]

    def test_the_collector_dashboard_and_backup_authorization_follow_the_remaining_work(self) -> None:
        from datetime import timedelta

        for segmented in (False, True):
            for max_batches in (100, 1):
                with self.subTest(segmented=segmented, max_batches=max_batches):
                    self.setUp()
                    self._insert_samples()
                    collector = self._collector(segmented=segmented, max_batches=max_batches)
                    backup_at = NOW - timedelta(minutes=5)
                    collector._run_retention_if_due(NOW, backup_succeeded=True, backup_at=backup_at)

                    remaining = bool(self._expired_hourly()) or self.store.counts()["metric_sample_count"] > 0
                    status = collector.status()
                    # The public flag, the dashboard line and the next-due
                    # answer say exactly whether eligible rows remain.
                    self.assertIs(status["last_retention_has_more"], remaining)
                    self.assertEqual(self._rendered_pending(collector), "yes" if remaining else "no")
                    self.assertIs(collector._retention_due(NOW + timedelta(seconds=60)), remaining)
                    if segmented:
                        # The backup claim is kept for another pass while work remains.
                        self.assertEqual(self._authorization_state(), "ready" if remaining else "consumed")
                    # A sufficient budget finishes in one pass; one batch does not.
                    self.assertEqual(remaining, max_batches == 1)
                    if remaining:
                        collector._run_retention_if_due(
                            NOW + timedelta(seconds=60), backup_succeeded=True, backup_at=backup_at
                        )
                        if max_batches == 1:
                            # One raw row per pass: the second pass finishes it.
                            self.assertEqual(self.store.counts()["metric_sample_count"], 0)
                        self.assertEqual(self._expired_hourly(), [])
                        self.assertFalse(collector.status()["last_retention_has_more"])
                        if segmented:
                            self.assertEqual(self._authorization_state(), "consumed")

    def test_reopening_for_written_rollups_does_not_reread_rejected_rows(self) -> None:
        # The hourly table holds only window rows the exact-time test rejects,
        # so it is finished after the first batch. Every later batch of the
        # old raw backlog writes expired hourly rollups into it. Work stays
        # proportional to rows deleted plus rollups written: the rejected rows
        # are not read again for each batch that writes into the table.
        from history_service import store as store_module

        helper = RetentionCrossTableWorkTests()
        original = store_module._history_utc_instant
        parses: dict[int, int] = {}
        steps: dict[int, int] = {}
        for count in (200, 800):
            with tempfile.TemporaryDirectory(prefix="retention-reopen-") as temporary:
                path = Path(temporary) / "history.db"
                store = HistoryStore(str(path))
                helper._insert(path, "3600", REJECTED_WINDOW_TEXT, count)
                helper._insert(path, "metrics", OLD_TEXT, count)
                parsed = [0]

                def counting(value: object) -> str | None:
                    if value == REJECTED_WINDOW_TEXT:
                        parsed[0] += 1
                    return original(value)

                work = _count_store_work(store)
                with mock.patch.object(store_module, "_history_utc_instant", counting):
                    result = _retain(
                        store,
                        raw_metric_retention_days=1,
                        hourly_rollup_retention_days=1,
                        batch_size=10,
                        max_batches=1000,
                    )
                parses[count], steps[count] = parsed[0], work[0]
                self.assertFalse(result["has_more"])
                self.assertEqual(result["metric_samples_removed"], count)
                self.assertEqual(result["hourly_rollups_removed"], count)
                self.assertEqual(helper._count(path, "metrics"), 0)
                self.assertEqual(helper._count(path, "3600"), count)
                self.assertLessEqual(parsed[0], 2 * count, parses)
        self.assertLessEqual(parses[800] / parses[200], 1.5 * 800 / 200, parses)
        self.assertLessEqual(steps[800] / steps[200], 1.5 * 800 / 200, steps)


UNDECODABLE_OLD = b"2026-11-01T00:00:00+00:0\xff"  # sorts more than a day before the cutoff
UNDECODABLE_WINDOW = b"2026-11-14T00:00:00+00:0\xff"  # sorts in the day before the cutoff
UNREADABLE_WINDOW = "2026-11-14T00:00:00+25:00"  # decodable, but not a time


class RetentionUndecodableTimestampTests(unittest.TestCase):
    """A timestamp stored as TEXT that is not UTF-8 never stops retention.

    SQLite accepts such a value and so do the integrity and schema checks.
    It is treated like any other text that is not a time: expired once its
    text sorts more than a day before the cutoff, kept in the day before it.
    Every other expired row in every table is still removed in the same pass.
    """

    POPULATIONS = ("events", "valueless_samples", "3600", "86400")

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-undecodable-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    @staticmethod
    def _insert(path: Path, population: str, observed_at: object, slot: int) -> None:
        # bytes are written as a BLOB cast to TEXT: TEXT storage, raw bytes.
        value = "CAST(? AS TEXT)" if isinstance(observed_at, bytes) else "?"
        with closing(sqlite3.connect(path)) as connection:
            if population == "events":
                connection.execute(
                    "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, "
                    "slot_label, event_type, details_json) "
                    f"VALUES ({value}, 'synthetic-system', '', ?, '0', 'slot_state_changed', '{{}}')",
                    (observed_at, slot),
                )
            elif population in ("samples", "valueless_samples"):
                connection.execute(
                    "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, "
                    f"slot_label, metric_name, value_integer) VALUES ({value}, 'synthetic-system', "
                    "'', ?, '0', 'bytes_read', ?)",
                    (observed_at, slot, None if population == "valueless_samples" else 1),
                )
            else:
                connection.execute(
                    "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, "
                    "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                    "value_min, value_max, last_value, last_observed_at) "
                    f"VALUES ({value}, ?, 'synthetic-system', '', ?, '0', 'bytes_read', "
                    "1, 1, 1, 1, 1, '2026-01-01T00:00:00+00:00')",
                    (observed_at, int(population), slot),
                )
            connection.commit()

    @staticmethod
    def _left(path: Path) -> dict[str, list[bytes]]:
        with closing(sqlite3.connect(path)) as connection:
            connection.text_factory = bytes
            return {
                "events": [row[0] for row in connection.execute(
                    "SELECT observed_at FROM slot_events ORDER BY id")],
                "samples": [row[0] for row in connection.execute(
                    "SELECT observed_at FROM metric_samples ORDER BY id")],
                "3600": [row[0] for row in connection.execute(
                    "SELECT bucket_start FROM metric_rollups WHERE bucket_seconds = 3600 ORDER BY rowid")],
                "86400": [row[0] for row in connection.execute(
                    "SELECT bucket_start FROM metric_rollups WHERE bucket_seconds = 86400 ORDER BY rowid")],
            }

    def _case(self, name: str, population: str, undecodable: object, *, first: bool) -> tuple[Path, HistoryStore]:
        # One readable expired row in every population, and the odd row in one.
        # When first is false a readable row sorts before it, so a batch of
        # one reaches the odd row with the scan floor already set.
        path = self.root / f"{name}.db"
        store = HistoryStore(str(path))
        readable = "2026-11-12T00:00:00+00:00" if first else "2026-10-01T00:00:00+00:00"
        for other in ("events", "samples", "3600", "86400"):
            self._insert(path, other, readable, 1)
        self._insert(path, population, undecodable, 2)
        return path, store

    def _retain_without_error(self, store: HistoryStore, **days_and_batches: int) -> dict:
        try:
            return _retain(store, **days_and_batches)
        except sqlite3.Error as exc:
            self.fail(f"retention raised and rolled back: {exc!r}")

    def _retain_all(self, store: HistoryStore, batch_size: int) -> dict:
        return self._retain_without_error(
            store,
            raw_metric_retention_days=1,
            event_retention_days=1,
            hourly_rollup_retention_days=1,
            daily_rollup_retention_days=1,
            batch_size=batch_size,
            max_batches=50,
        )

    def _expected_left(self, population: str, kept: object) -> dict[str, list]:
        left: dict[str, list] = {"events": [], "samples": [], "3600": [], "86400": []}
        if kept is not None:
            left["samples" if population.endswith("samples") else population].append(kept)
        return left

    def _matrix(self, undecodable: bytes, kept: bytes | None) -> None:
        for population in self.POPULATIONS:
            for first, batch_size in ((True, 100), (True, 1), (False, 1)):
                name = f"{undecodable.hex()}-{population}-{first}-{batch_size}"
                with self.subTest(population=population, odd_row_first=first, batch_size=batch_size):
                    path, store = self._case(name, population, undecodable, first=first)
                    result = self._retain_all(store, batch_size)
                    self.assertEqual(self._left(path), self._expected_left(population, kept))
                    self.assertFalse(result["has_more"])

    def test_an_undecodable_timestamp_long_before_the_cutoff_is_deleted_like_any_old_text(self) -> None:
        self._matrix(UNDECODABLE_OLD, kept=None)

    def test_an_undecodable_timestamp_in_the_day_before_the_cutoff_is_kept_like_unreadable_text(self) -> None:
        # Same rule as the decodable row whose time cannot be read.
        self._matrix(UNREADABLE_WINDOW.encode(), kept=UNREADABLE_WINDOW.encode())
        self._matrix(UNDECODABLE_WINDOW, kept=UNDECODABLE_WINDOW)

    def test_the_pass_continues_after_an_undecodable_row_becomes_the_scan_floor(self) -> None:
        path = self.root / "floor.db"
        store = HistoryStore(str(path))
        self._insert(path, "events", UNDECODABLE_OLD, 1)
        for second in range(3):
            self._insert(path, "events", f"2026-11-02T00:00:0{second}+00:00", 2)

        result = self._retain_without_error(store, event_retention_days=1, batch_size=1, max_batches=10)

        self.assertEqual(self._left(path)["events"], [])
        self.assertEqual(result["events_removed"], 4)
        self.assertFalse(result["has_more"])

    def test_a_rollup_merge_treats_an_undecodable_last_time_like_unreadable_text(self) -> None:
        # The bucket is kept (rollup retention off); a newer raw sample merges
        # into it. An existing last time that is not a time keeps its value,
        # exactly as for the decodable unreadable time.
        outcomes = []
        for name, last_observed_at in (("unreadable", UNREADABLE_WINDOW), ("undecodable", UNDECODABLE_WINDOW)):
            with self.subTest(last_observed_at=name):
                path = self.root / f"merge-{name}.db"
                store = HistoryStore(str(path))
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(
                        "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, "
                        "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                        "value_min, value_max, last_value, last_observed_at, disk_identity_key) "
                        "VALUES ('2026-11-03T00:00:00+00:00', 3600, ?, 'enc-a', 5, '05', "
                        "'temperature_c', 1, 7, 7, 7, 7, CAST(? AS TEXT), '')",
                        (SYSTEM_ID, last_observed_at.encode() if isinstance(last_observed_at, str) else last_observed_at),
                    )
                    connection.commit()
                store.insert_metric_samples([_sample("2026-11-03T00:10:00+00:00", 40)])

                result = self._retain_without_error(store, raw_metric_retention_days=1)

                with closing(sqlite3.connect(path)) as connection:
                    connection.text_factory = bytes
                    rows = connection.execute(
                        "SELECT bucket_seconds, sample_count, last_value, last_observed_at "
                        "FROM metric_rollups ORDER BY bucket_seconds"
                    ).fetchall()
                    samples = connection.execute("SELECT count(*) FROM metric_samples").fetchone()[0]
                self.assertEqual(samples, 0)
                self.assertEqual(result["metric_samples_removed"], 1)
                self.assertFalse(result["has_more"])
                self.assertEqual(rows[0][:3], (3600, 2, 7.0))
                self.assertEqual(rows[1][:3], (86400, 1, 40.0))
                outcomes.append(rows[0][3])
        self.assertEqual(outcomes, [UNREADABLE_WINDOW.encode(), UNDECODABLE_WINDOW])

    def test_an_undecodable_raw_sample_with_a_value_still_fails_its_rollup_as_before(self) -> None:
        # No hourly bucket can be named for it, so its rollup insert fails and
        # the batch rolls back, as it did before #1204 (unchanged here).
        path = self.root / "valued.db"
        store = HistoryStore(str(path))
        self._insert(path, "samples", UNDECODABLE_OLD, 1)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "NOT NULL constraint failed: metric_rollups.bucket_start"):
            _retain(store, raw_metric_retention_days=1)

        self.assertEqual(self._left(path)["samples"], [UNDECODABLE_OLD])


# Text of any length is legal in a timestamp column of a restored database.
# Both sort more than a day before the cutoff, so both are expired as old text.
OVERSIZED_OLD = "2026-10-01" + "x" * 65_536
LONGEST_HANDED_BACK = 256  # bytes; a timestamp this code writes is 32 characters


class RetentionOversizedTimestampTests(unittest.TestCase):
    """A stored timestamp of any length never grows what a batch holds.

    Such a row is deleted like any other old text, but its text is neither
    handed back by the delete nor kept as the pass's lower bound: for each
    row a batch deletes it holds a short timestamp at most.
    """

    POPULATIONS = ("events", "valueless_samples", "3600", "86400")
    FLOOR_NAMES = {
        "events": "slot_events",
        "valueless_samples": "metric_samples",
        "3600": "rollups_3600",
        "86400": "rollups_86400",
    }
    ENCODING = "UTF-8"
    OVERSIZED_ROWS = 5

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-oversized-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _store(self, name: str) -> tuple[Path, HistoryStore]:
        path = self.root / f"{name}.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA encoding = '{self.ENCODING}'")
            connection.execute("CREATE TABLE encoding_marker (value)")
            connection.commit()
        return path, HistoryStore(str(path))

    def _watch_batches(self) -> tuple[list[int], dict[str, list[object]]]:
        """Record the bytes each delete handed back and every floor a pass set."""

        handed_back: list[int] = []
        floors_seen: dict[str, list[object]] = {}
        advance = HistoryStore._advance_scan_floor

        def watching(connection, floors, name, deleted):
            handed_back.extend(len(row[0]) for row in deleted if row[0] is not None)
            count = advance(connection, floors, name, deleted)
            if name in floors:
                floors_seen.setdefault(name, []).append(floors[name][0])
            return count

        patcher = mock.patch.object(HistoryStore, "_advance_scan_floor", staticmethod(watching))
        patcher.start()
        self.addCleanup(patcher.stop)
        return handed_back, floors_seen

    def _retain_all(self, store: HistoryStore, batch_size: int) -> dict:
        return _retain(
            store,
            raw_metric_retention_days=1,
            event_retention_days=1,
            hourly_rollup_retention_days=1,
            daily_rollup_retention_days=1,
            batch_size=batch_size,
            max_batches=200,
        )

    def _assert_short(self, handed_back: list[int], floors_seen: dict[str, list[object]]) -> None:
        self.assertLessEqual(max(handed_back, default=0), LONGEST_HANDED_BACK)
        for name, texts in floors_seen.items():
            for text in texts:
                size = len(text.encode(self.ENCODING) if isinstance(text, str) else text)
                self.assertLessEqual(size, LONGEST_HANDED_BACK, name)

    def test_oversized_old_timestamps_are_deleted_without_being_handed_back(self) -> None:
        insert = RetentionUndecodableTimestampTests._insert
        for population in self.POPULATIONS:
            for batch_size in (100, 1):
                with self.subTest(population=population, batch_size=batch_size):
                    path, store = self._store(f"{population}-{batch_size}")
                    # Readable expired rows sort on both sides of the oversized ones.
                    insert(path, population, "2026-09-30T00:00:00+00:00", 1)
                    for slot in range(self.OVERSIZED_ROWS):
                        insert(path, population, OVERSIZED_OLD, 10 + slot)
                    insert(path, population, "2026-10-02T00:00:00+00:00", 2)
                    handed_back, floors_seen = self._watch_batches()

                    result = self._retain_all(store, batch_size)

                    left = RetentionUndecodableTimestampTests._left(path)
                    self.assertEqual(left, {"events": [], "samples": [], "3600": [], "86400": []})
                    self.assertFalse(result["has_more"])
                    # The readable rows were handed back, so the watch saw the deletes.
                    self.assertGreaterEqual(len(handed_back), 2)
                    self._assert_short(handed_back, floors_seen)

    def test_the_floor_is_the_last_short_timestamp_of_a_batch_with_oversized_rows(self) -> None:
        insert = RetentionUndecodableTimestampTests._insert
        for population in self.POPULATIONS:
            with self.subTest(population=population):
                path, store = self._store(f"floor-{population}")
                # By text: first < second < the oversized row.
                first, second = "2026-09-30T00:00:00+00:00", "2026-10-01T00:00:00+00:00"
                insert(path, population, second, 2)
                insert(path, population, OVERSIZED_OLD, 3)
                insert(path, population, first, 1)
                _handed_back, floors_seen = self._watch_batches()

                self._retain_all(store, 100)

                floor = floors_seen[self.FLOOR_NAMES[population]][0]
                self.assertEqual(floor.decode() if isinstance(floor, bytes) else floor, second)

    def test_a_batch_of_only_oversized_rows_sets_no_floor_and_the_pass_finishes(self) -> None:
        insert = RetentionUndecodableTimestampTests._insert
        for population in self.POPULATIONS:
            with self.subTest(population=population):
                path, store = self._store(f"only-{population}")
                for slot in range(self.OVERSIZED_ROWS):
                    insert(path, population, OVERSIZED_OLD, slot)
                handed_back, floors_seen = self._watch_batches()

                result = self._retain_all(store, 2)

                left = RetentionUndecodableTimestampTests._left(path)
                self.assertEqual(left, {"events": [], "samples": [], "3600": [], "86400": []})
                self.assertFalse(result["has_more"])
                self.assertEqual(handed_back, [])
                self.assertNotIn(self.FLOOR_NAMES[population], floors_seen)

    def test_text_at_the_limit_is_handed_back_and_one_character_longer_is_not(self) -> None:
        insert = RetentionUndecodableTimestampTests._insert
        width = len("x".encode(self.ENCODING))
        at_limit = LONGEST_HANDED_BACK // width
        for characters, expected in ((at_limit, [LONGEST_HANDED_BACK]), (at_limit + 1, [])):
            with self.subTest(characters=characters):
                path, store = self._store(f"limit-{characters}")
                insert(path, "events", "2026-10-01" + "x" * (characters - 10), 1)
                handed_back, _floors_seen = self._watch_batches()

                result = self._retain_all(store, 100)

                self.assertEqual(handed_back, expected)
                self.assertEqual(result["events_removed"], 1)


class RetentionOversizedTimestampUtf16Tests(RetentionOversizedTimestampTests):
    ENCODING = "UTF-16le"


class RetentionOversizedTimestampUtf16BigEndianTests(RetentionOversizedTimestampTests):
    ENCODING = "UTF-16be"


# Text SQLite reads as a time (it names the row's bucket) that the exact
# reading rejects. Each is 00:30 UTC on 2026-11-03. A restored database can
# hold any of them; this service writes none.
SQLITE_ONLY_0030 = {
    "trailing spaces": "2026-11-03T00:30:00+00:00   ",
    "trailing tab and newline": "2026-11-03T00:30:00+00:00\t\n",
    "spaces after Z": "2026-11-03T00:30:00Z  ",
    "no offset": "2026-11-03T00:30:00",
    "text after a NUL": "2026-11-03T00:30:00+00:00\x00tail",
    # 19:30 at -05:00 the day before: its text sorts before 00:15 UTC.
    "another offset": "2026-11-02T19:30:00-05:00 ",
}
ORDINARY_0015 = "2026-11-03T00:15:00+00:00"
ORDINARY_0045 = "2026-11-03T00:45:00+00:00"


class RetentionSqliteReadableTimeTests(unittest.TestCase):
    """A counter's last value follows time order for every time that has a bucket."""

    ENCODING = "UTF-8"

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="retention-sqlite-time-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def _store(self, name: str) -> tuple[Path, HistoryStore]:
        path = self.root / f"{name}.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA encoding = '{self.ENCODING}'")
            connection.execute("CREATE TABLE encoding_marker (value)")
            connection.commit()
        return path, HistoryStore(str(path))

    @staticmethod
    def _insert(path: Path, rows: list[tuple[str, int]]) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.executemany(
                "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, "
                "slot_label, metric_name, value_integer) "
                "VALUES (?, 'synthetic-system', '', 5, '05', 'bytes_read', ?)",
                rows,
            )
            connection.commit()

    @staticmethod
    def _retain_counters(store: HistoryStore, batch_size: int) -> dict:
        return _retain(
            store,
            raw_metric_retention_days=1,
            hourly_rollup_retention_days=365,
            daily_rollup_retention_days=365,
            batch_size=batch_size,
            max_batches=50,
        )

    CASES = [
        (form, text, batch_size, f"{index}-{batch_size}")
        for index, (form, text) in enumerate(SQLITE_ONLY_0030.items())
        for batch_size in (100, 2, 1)
    ]

    def test_the_forms_are_read_by_sqlite_and_not_by_the_exact_reading(self) -> None:
        from history_service.store import _history_utc_instant

        with closing(sqlite3.connect(":memory:")) as connection:
            for name, text in SQLITE_ONLY_0030.items():
                with self.subTest(form=name):
                    self.assertIsNone(_history_utc_instant(text))
                    self.assertEqual(
                        connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%S', ?)", (text,)).fetchone()[0],
                        "2026-11-03T00:30:00",
                    )

    def test_the_latest_sample_gives_the_last_value_when_only_sqlite_reads_its_time(self) -> None:
        for form, text, batch_size, name in self.CASES:
            with self.subTest(form=form, batch_size=batch_size):
                path, store = self._store(f"latest-{name}")
                self._insert(path, [(ORDINARY_0015, 15), (text, 30)])

                result = self._retain_counters(store, batch_size)

                self.assertEqual(result["metric_samples_removed"], 2)
                self.assertEqual(_rollups(path), [(3600, 2, 30.0, text), (86400, 2, 30.0, text)])

    def test_an_ordinary_later_sample_still_gives_the_last_value(self) -> None:
        for form, text, batch_size, name in self.CASES:
            with self.subTest(form=form, batch_size=batch_size):
                path, store = self._store(f"earlier-{name}")
                self._insert(path, [(text, 30), (ORDINARY_0045, 45)])

                result = self._retain_counters(store, batch_size)

                self.assertEqual(result["metric_samples_removed"], 2)
                self.assertEqual(
                    _rollups(path),
                    [(3600, 2, 45.0, ORDINARY_0045), (86400, 2, 45.0, ORDINARY_0045)],
                )

    def test_a_merge_into_a_kept_bucket_follows_time_order_both_ways(self) -> None:
        # The first pass writes the bucket; the second sample arrives late and merges.
        for form, text, batch_size, name in self.CASES:
            for first, second, last in (
                ((ORDINARY_0015, 15), (text, 30), (30.0, text)),
                ((text, 30), (ORDINARY_0015, 15), (30.0, text)),
                ((text, 30), (ORDINARY_0045, 45), (45.0, ORDINARY_0045)),
                ((ORDINARY_0045, 45), (text, 30), (45.0, ORDINARY_0045)),
            ):
                with self.subTest(form=form, batch_size=batch_size, first=first[1], second=second[1]):
                    path, store = self._store(f"merge-{name}-{first[1]}-{second[1]}")
                    self._insert(path, [first])
                    self._retain_counters(store, batch_size)
                    self._insert(path, [second])

                    self._retain_counters(store, batch_size)

                    self.assertEqual(_rollups(path), [(3600, 2, *last), (86400, 2, *last)])

    def test_a_late_sample_at_the_same_instant_replaces_the_last_value_as_an_ordinary_one_does(self) -> None:
        # Two samples at one instant: the one that arrives later gives the last
        # value, whichever of the two readings names its instant.
        ordinary = "2026-11-03T00:30:00+00:00"
        padded = SQLITE_ONLY_0030["trailing spaces"]
        for first, second in ((ordinary, ordinary), (ordinary, padded), (padded, ordinary), (padded, padded)):
            with self.subTest(first=first, second=second):
                path, store = self._store(f"tie-{len(first)}-{len(second)}")
                self._insert(path, [(first, 30)])
                self._retain_counters(store, 100)
                self._insert(path, [(second, 31)])

                self._retain_counters(store, 100)

                self.assertEqual(_rollups(path), [(3600, 2, 31.0, second), (86400, 2, 31.0, second)])


class RetentionSqliteReadableTimeUtf16Tests(RetentionSqliteReadableTimeTests):
    ENCODING = "UTF-16le"


class RetentionSqliteReadableTimeUtf16BigEndianTests(RetentionSqliteReadableTimeTests):
    ENCODING = "UTF-16be"


class RetentionQueryPlanTests(unittest.TestCase):
    """Every retention statement reads the tables through the timestamp index."""

    TABLES = ("metric_samples", "slot_events", "metric_rollups")
    ENCODING = "UTF-8"

    def test_every_retention_statement_searches_the_timestamp_index(self) -> None:
        with tempfile.TemporaryDirectory(prefix="retention-plan-") as temporary:
            path = Path(temporary) / "history.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"PRAGMA encoding = '{self.ENCODING}'")
                connection.execute("CREATE TABLE encoding_marker (value)")
                connection.commit()
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
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA encoding").fetchone()[0], self.ENCODING)


class RetentionQueryPlanUtf16Tests(RetentionQueryPlanTests):
    ENCODING = "UTF-16le"


class RetentionQueryPlanUtf16BigEndianTests(RetentionQueryPlanTests):
    ENCODING = "UTF-16be"


if __name__ == "__main__":
    unittest.main()
