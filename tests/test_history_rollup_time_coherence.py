"""Rollup buckets are named from the same SQLite reading that orders samples.

Retention ranks raw samples inside a rollup bucket by SQLite's julianday()
reading of `observed_at`. Some SQLite builds (3.45.1, the one CI and the
image ship) read a time in the last half millisecond of an hour as the next
hour, while strftime() on the same text still names the earlier hour unless
the text carries a non-zero UTC offset. A bucket named from the text then
disagrees with the reading that orders it, and one instant spelled with two
offsets lands in two buckets.

Each expectation here is computed with julianday() on the SQLite build that
runs the test, so the tests hold on every build: what they require is that
naming, ordering and grouping all come from one reading.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from history_service.domain import MetricSample
from history_service.store import HistoryStore

SYSTEM_ID = "synthetic-system"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
# One-day retention from NOW: the cutoff is 2026-11-14T12:00:00+00:00.
CUTOFF = "2026-11-14T12:00:00+00:00"

# One instant in the last half millisecond of a day, spelled two ways.
BOUNDARY_UTC = "2026-11-03T23:59:59.999999+00:00"
BOUNDARY_OFFSET = "2026-11-03T18:59:59.999999-05:00"
# Control: early enough in the last millisecond that every build reads it
# inside the hour its text names.
EARLY_FRACTION = "2026-11-03T23:59:59.9994+00:00"

BUCKET_FORMATS = {
    3600: "%Y-%m-%dT%H:00:00+00:00",
    86400: "%Y-%m-%dT00:00:00+00:00",
}


def _sample(observed_at: str, value: int, *, metric_name: str = "bytes_read") -> MetricSample:
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


def _bucket_of(observed_at: str, bucket_seconds: int) -> str:
    """The bucket SQLite's own reading of the time puts it in, on this build."""

    with closing(sqlite3.connect(":memory:")) as connection:
        return connection.execute(
            "SELECT strftime(?, julianday(?))",
            (BUCKET_FORMATS[bucket_seconds], observed_at),
        ).fetchone()[0]


class RollupTimeCoherenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="rollup-time-")
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "history.db"
        self.store = HistoryStore(str(self.path))

    def _retain(self, *, rollup_days: int = 365, batch_size: int = 100) -> dict:
        return self.store.maintain_retention(
            now=NOW,
            raw_metric_retention_days=1,
            event_retention_days=0,
            hourly_rollup_retention_days=rollup_days,
            daily_rollup_retention_days=rollup_days,
            batch_size=batch_size,
            max_batches=20,
        )

    def _rollups(self, metric_name: str) -> list[tuple]:
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT bucket_seconds, bucket_start, sample_count, last_value, last_observed_at "
                "FROM metric_rollups WHERE metric_name = ? "
                "ORDER BY bucket_seconds, bucket_start",
                (metric_name,),
            ).fetchall()

    def test_one_instant_lands_in_one_bucket_whatever_its_offset_spelling(self) -> None:
        for batch_size in (100, 1):
            with self.subTest(batch_size=batch_size):
                metric = f"bytes_read_{batch_size}"
                self.store.insert_metric_samples(
                    [
                        _sample(BOUNDARY_UTC, 10, metric_name=metric),
                        _sample(BOUNDARY_OFFSET, 20, metric_name=metric),
                    ]
                )

                self._retain(batch_size=batch_size)

                rollups = self._rollups(metric)
                self.assertEqual(
                    [(seconds, start, count) for seconds, start, count, _, _ in rollups],
                    [
                        (3600, _bucket_of(BOUNDARY_UTC, 3600), 2),
                        (86400, _bucket_of(BOUNDARY_UTC, 86400), 2),
                    ],
                )

    def test_every_bucket_holds_the_reading_that_ordered_its_last_sample(self) -> None:
        self.store.insert_metric_samples(
            [
                _sample(EARLY_FRACTION, 1),
                _sample(BOUNDARY_UTC, 2),
                _sample("2026-11-04T00:00:00.000500+00:00", 3),
            ]
        )

        self._retain()

        rollups = self._rollups("bytes_read")
        self.assertTrue(rollups)
        with closing(sqlite3.connect(":memory:")) as connection:
            for bucket_seconds, bucket_start, _, _, last_observed_at in rollups:
                with self.subTest(bucket_seconds=bucket_seconds, bucket_start=bucket_start):
                    inside = connection.execute(
                        "SELECT julianday(?) >= julianday(?) "
                        "AND julianday(?) < julianday(?) + ? / 86400.0",
                        (last_observed_at, bucket_start, last_observed_at, bucket_start, bucket_seconds),
                    ).fetchone()[0]
                    self.assertEqual(inside, 1, (bucket_start, last_observed_at))
        # The control is named from its text on every build.
        hourly = {start: count for seconds, start, count, _, _ in rollups if seconds == 3600}
        self.assertEqual(_bucket_of(EARLY_FRACTION, 3600), "2026-11-03T23:00:00+00:00")
        self.assertIn("2026-11-03T23:00:00+00:00", hourly)

    def test_an_existing_bucket_keeps_its_name_and_unrelated_counts(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, enclosure_key, slot,
                    slot_label, metric_name, sample_count, value_sum, value_min,
                    value_max, last_value, last_observed_at, disk_identity_key
                ) VALUES (
                    '2026-11-03T23:00:00+00:00', 3600, ?, 'enc-a', 5, '05', 'bytes_read',
                    1, 5, 5, 5, 5, '2026-11-03T23:30:00+00:00', ''
                )
                """,
                (SYSTEM_ID,),
            )
            connection.commit()
        self.store.insert_metric_samples([_sample(BOUNDARY_UTC, 7)])

        self._retain()

        hourly = [row for row in self._rollups("bytes_read") if row[0] == 3600]
        boundary_bucket = _bucket_of(BOUNDARY_UTC, 3600)
        if boundary_bucket == "2026-11-03T23:00:00+00:00":
            expected = [(3600, boundary_bucket, 2, 7.0, BOUNDARY_UTC)]
        else:
            expected = [
                (3600, "2026-11-03T23:00:00+00:00", 1, 5.0, "2026-11-03T23:30:00+00:00"),
                (3600, boundary_bucket, 1, 7.0, BOUNDARY_UTC),
            ]
        self.assertEqual(hourly, expected)

    def test_nothing_newer_than_the_retention_cut_is_removed(self) -> None:
        # One microsecond before the cut, in the last half millisecond of
        # its hour, and samples at and after the cut.
        just_before = "2026-11-14T11:59:59.999999+00:00"
        self.store.insert_metric_samples(
            [
                _sample(just_before, 1),
                _sample(CUTOFF, 2),
                _sample("2026-11-14T07:00:00.000001-05:00", 3),
                _sample("2026-11-15T00:00:00+00:00", 4),
            ]
        )

        result = self._retain(rollup_days=1, batch_size=1)

        with closing(sqlite3.connect(self.path)) as connection:
            raw = sorted(row[0] for row in connection.execute("SELECT value_integer FROM metric_samples"))
            hourly = connection.execute(
                "SELECT bucket_start, sample_count FROM metric_rollups WHERE bucket_seconds = 3600"
            ).fetchall()
        self.assertEqual(raw, [2, 3, 4])
        self.assertEqual(result["metric_samples_removed"], 1)
        self.assertFalse(result["has_more"])
        # The hourly rollup of the expired sample is kept exactly when its
        # bucket is not older than the hourly cut, which is the same instant.
        bucket = _bucket_of(just_before, 3600)
        self.assertEqual(hourly, [(bucket, 1)] if bucket >= CUTOFF else [])


if __name__ == "__main__":
    unittest.main()
