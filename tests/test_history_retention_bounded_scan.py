"""History retention reads a bounded page of rows per batch (#1268).

Rows whose text lies in the day before the cutoff but whose exact UTC instant
is not yet expired (for example a row stored with a negative offset) used to
be read by every batch, however many there were. A batch now reads at most a
page of candidate rows per table and moves past the rows it keeps. Which rows
are removed, and when, is unchanged. A restore refuses timestamps the
retention comparison cannot order. All rows are synthetic and live in a
temporary directory.
"""

from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from history_service import store as store_module
from history_service.store import HistoryStore
from history_service.system_backup import SystemBackupService

SYSTEM_ID = "synthetic-system"
NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
CUTOFF = "2026-11-14T12:00:00+00:00"  # one-day retention from NOW
# 13:00 UTC on the 14th: newer than the cutoff, but its text sorts before it.
KEPT_TEXT = "2026-11-14T08:00:00-05:00"
BATCH_SIZE = 10
# A batch reads at most twice its size from each table: room for a full batch
# of removals and as many rows the exact-time comparison keeps.
PAGE_SIZE = 2 * BATCH_SIZE
ALL_CUTOFFS = {"metric": CUTOFF, "event": CUTOFF, "hourly": CUTOFF, "daily": CUTOFF}


def _database(test: unittest.TestCase, name: str = "history.db") -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="retention-page-")
    test.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / name
    HistoryStore(str(path))
    return path


def _insert(path: Path, population: str, texts: list[str]) -> None:
    """Insert one row per text into events, raw samples or a rollup interval."""

    with closing(sqlite3.connect(path)) as connection:
        if population == "events":
            connection.executemany(
                "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, "
                "slot_label, event_type, details_json, current_value) "
                "VALUES (?, ?, '', 0, '0', 'slot_state_changed', '{}', ?)",
                ((text, SYSTEM_ID, str(index)) for index, text in enumerate(texts)),
            )
        elif population == "metrics":
            connection.executemany(
                "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, "
                "slot_label, metric_name, value_integer) "
                "VALUES (?, ?, '', ?, '0', 'bytes_read', 1)",
                ((text, SYSTEM_ID, index) for index, text in enumerate(texts)),
            )
        else:
            connection.executemany(
                "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, "
                "enclosure_key, slot, slot_label, metric_name, sample_count, value_sum, "
                "value_min, value_max, last_value, last_observed_at) "
                "VALUES (?, ?, ?, '', ?, '0', 'bytes_read', 1, 1, 1, 1, 1, ?)",
                ((text, int(population), SYSTEM_ID, index, text) for index, text in enumerate(texts)),
            )
        connection.commit()


def _texts(path: Path, population: str) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        if population == "events":
            rows = connection.execute("SELECT observed_at FROM slot_events")
        elif population == "metrics":
            rows = connection.execute("SELECT observed_at FROM metric_samples")
        else:
            rows = connection.execute(
                "SELECT bucket_start FROM metric_rollups WHERE bucket_seconds = ?", (int(population),)
            )
        return sorted(row[0] for row in rows)


def _retain(store: HistoryStore, now: datetime, *, batch_size: int, max_batches: int, **days: int) -> dict:
    return store.maintain_retention(
        now=now,
        raw_metric_retention_days=days.get("raw_metric_retention_days", 0),
        event_retention_days=days.get("event_retention_days", 0),
        hourly_rollup_retention_days=days.get("hourly_rollup_retention_days", 0),
        daily_rollup_retention_days=days.get("daily_rollup_retention_days", 0),
        batch_size=batch_size,
        max_batches=max_batches,
    )


def _removed(result: dict) -> int:
    return sum(
        result[key]
        for key in ("events_removed", "metric_samples_removed", "hourly_rollups_removed", "daily_rollups_removed")
    )


POPULATION_DAYS = {
    "events": {"event_retention_days": 1},
    "metrics": {"raw_metric_retention_days": 1, "hourly_rollup_retention_days": 365,
                "daily_rollup_retention_days": 365},
    "3600": {"hourly_rollup_retention_days": 1},
    "86400": {"daily_rollup_retention_days": 1},
}
POPULATION_CUTOFFS = {
    "events": {"metric": None, "event": CUTOFF, "hourly": None, "daily": None},
    "metrics": {"metric": CUTOFF, "event": None, "hourly": None, "daily": None},
    "3600": {"metric": None, "event": None, "hourly": CUTOFF, "daily": None},
    "86400": {"metric": None, "event": None, "hourly": None, "daily": CUTOFF},
}


class BatchReadBoundTests(unittest.TestCase):
    """One batch reads at most its page, whatever the kept set holds."""

    SMALL = 300
    LARGE = 3000
    # B-tree depth only; the kept rows themselves must not add work.
    SLACK = 200

    def _one_batch(self, path: Path, cutoffs: dict) -> tuple[int, int, dict]:
        parsed = [0]
        original = store_module._history_utc_instant

        def counting(value: object) -> str | None:
            if value == KEPT_TEXT:
                parsed[0] += 1
            return original(value)

        steps = [0]

        def progress() -> int:
            steps[0] += 1
            return 0

        with closing(sqlite3.connect(path)) as connection, mock.patch.object(
            store_module, "_history_utc_instant", counting
        ):
            connection.set_progress_handler(progress, 1)
            result = HistoryStore._maintain_retention_batch(
                connection, cutoffs=cutoffs, batch_size=BATCH_SIZE
            )
            connection.set_progress_handler(None, 0)
            connection.rollback()
        return parsed[0], steps[0], result

    def test_one_batch_reads_no_more_than_its_page_size_with_a_large_kept_set(self) -> None:
        for population, cutoffs in POPULATION_CUTOFFS.items():
            with self.subTest(population=population):
                parsed: dict[int, int] = {}
                steps: dict[int, int] = {}
                results: dict[int, dict] = {}
                for count in (self.SMALL, self.LARGE):
                    path = _database(self, f"{population}-{count}.db")
                    # The kept set first, then expired rows the batch has not reached.
                    _insert(path, population, [KEPT_TEXT] * count + ["2026-11-14T10:00:00+00:00"] * 3)
                    parsed[count], steps[count], results[count] = self._one_batch(path, cutoffs)
                self.assertLessEqual(max(parsed.values()), PAGE_SIZE, parsed)
                self.assertLessEqual(steps[self.LARGE] - steps[self.SMALL], self.SLACK, steps)
                # The batch has not reached the expired rows: more work, shown.
                for result in results.values():
                    self.assertEqual(_removed(result), 0)
                    self.assertTrue(result["has_more"], result)

    def test_the_page_is_spent_on_removals_before_kept_rows_beyond_them(self) -> None:
        # Expired rows ahead of the kept set fill the batch: it removes
        # exactly its size, and still reads no more than its page.
        for population, cutoffs in POPULATION_CUTOFFS.items():
            with self.subTest(population=population):
                path = _database(self, f"{population}-control.db")
                _insert(path, population, ["2026-11-14T07:00:00+00:00"] * 15 + [KEPT_TEXT] * 50)
                parsed, _, result = self._one_batch(path, cutoffs)
                self.assertEqual(_removed(result), BATCH_SIZE)
                self.assertTrue(result["has_more"])
                self.assertLessEqual(parsed, PAGE_SIZE)


class KeptSetProgressTests(unittest.TestCase):
    """Later passes move past a kept set larger than one pass can read."""

    def test_passes_reach_expired_rows_behind_a_kept_set_larger_than_their_budget(self) -> None:
        for population, days in POPULATION_DAYS.items():
            with self.subTest(population=population):
                path = _database(self, f"{population}.db")
                store = HistoryStore(str(path))
                before = ["2026-11-14T00:30:00+00:00"] * 3
                after = ["2026-11-14T10:00:00+00:00"] * 7
                _insert(path, population, before + [KEPT_TEXT] * 300 + after)
                # Four batches of five: one pass reads at most 40 rows.
                results = []
                for _ in range(40):
                    results.append(_retain(store, NOW, batch_size=5, max_batches=4, **days))
                    if not results[-1]["has_more"]:
                        break
                self.assertFalse(results[-1]["has_more"], len(results))
                self.assertTrue(all(result["has_more"] for result in results[:-1]))
                self.assertGreater(len(results), 1)
                self.assertEqual(_texts(path, population), [KEPT_TEXT] * 300)
                self.assertEqual(sum(result["total_rows_removed"] for result in results), 10)

    def test_kept_rows_that_expire_behind_a_carried_position_go_on_the_next_fresh_pass(self) -> None:
        # A short pass leaves its position inside the kept set. Two hours
        # later every kept row (13:00 UTC) is older than the cutoff. The
        # carried sweep removes those ahead of its position and ends; the rows
        # behind it wait, like a row added behind the floor, for the next pass,
        # which starts from the beginning. None is removed before its time.
        later = NOW + timedelta(hours=2)
        for population, days in POPULATION_DAYS.items():
            with self.subTest(population=population):
                path = _database(self, f"{population}-carried.db")
                store = HistoryStore(str(path))
                _insert(path, population, [KEPT_TEXT] * 60 + ["2026-11-14T10:00:00+00:00"])
                first = _retain(store, NOW, batch_size=5, max_batches=2, **days)
                self.assertTrue(first["has_more"])
                self.assertEqual(first["total_rows_removed"], 0)
                self.assertEqual(len(_texts(path, population)), 61)
                carried = _retain(store, later, batch_size=5, max_batches=1000, **days)
                self.assertFalse(carried["has_more"])
                # The first pass read two pages of ten rows: twenty stay behind.
                self.assertEqual(_texts(path, population), [KEPT_TEXT] * 20)
                fresh = _retain(store, later, batch_size=5, max_batches=1000, **days)
                self.assertFalse(fresh["has_more"])
                self.assertEqual(_texts(path, population), [])
                self.assertEqual(carried["total_rows_removed"] + fresh["total_rows_removed"], 61)


class UnnamedFloorProgressTests(unittest.TestCase):
    """A page of kept rows whose text is too long to hand back still moves the floor."""

    # 13:00 UTC with 300 fraction digits: a valid, kept time longer than the
    # 256 bytes a batch reads back for its floor.
    LONG_KEPT = "2026-11-14T08:00:00." + "1" * 300 + "-05:00"

    def test_expired_rows_behind_kept_rows_too_long_to_name_are_reached(self) -> None:
        for population, days in POPULATION_DAYS.items():
            for batch_size in (1, 2):
                with self.subTest(population=population, batch_size=batch_size):
                    path = _database(self, f"{population}-{batch_size}-long.db")
                    store = HistoryStore(str(path))
                    _insert(path, population, [self.LONG_KEPT] * 6 + ["2026-11-14T10:00:00+00:00"])
                    result = _retain(store, NOW, batch_size=batch_size, max_batches=20, **days)
                    self.assertEqual(result["total_rows_removed"], 1)
                    self.assertFalse(result["has_more"])
                    self.assertEqual(_texts(path, population), [self.LONG_KEPT] * 6)


def _encoded_database(test: unittest.TestCase, name: str, encoding: str) -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="retention-page-")
    test.addCleanup(temporary.cleanup)
    path = Path(temporary.name) / name
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA encoding = '{encoding}'")
        connection.execute("CREATE TABLE encoding_marker (value)")
        connection.commit()
    HistoryStore(str(path))
    return path


def _batch_steps(connection: sqlite3.Connection, cutoffs: dict, batch_size: int, floors: dict) -> tuple[int, dict]:
    """Run one real batch from the given floors; return (SQLite VM steps, result)."""

    steps = [0]

    def progress() -> int:
        steps[0] += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        result = HistoryStore._maintain_retention_batch(
            connection, cutoffs=cutoffs, batch_size=batch_size, scan_floors=floors
        )
    finally:
        connection.set_progress_handler(None, 0)
    connection.commit()
    return steps[0], result


class EqualTimestampLaterPageTests(unittest.TestCase):
    """A batch after a floor inside a run of equal timestamps seeks past the run.

    The floor names a (timestamp, key) position. Its work must not grow with
    the number of kept rows that share its timestamp and lie behind it, for
    a floor named by text or by key, in every encoding. Floors come only from
    real earlier batches.
    """

    SMALL = 300
    LARGE = 3000
    SLACK = 300
    EXPIRED = "2026-11-14T10:00:00+00:00"

    def _later_batch_steps(self, population: str, text: str, count: int, encoding: str) -> int:
        path = _encoded_database(self, f"{population}-{count}-{encoding}.db", encoding)
        _insert(path, population, [text] * count + [self.EXPIRED])
        cutoffs = POPULATION_CUTOFFS[population]
        with closing(sqlite3.connect(path)) as connection:
            # A real first batch reads the whole run and leaves its floor on the last kept row.
            _, first = _batch_steps(connection, cutoffs, count // 2, {})
            self.assertEqual(_removed(first), 0)
            self.assertTrue(first["has_more"])
            (floor,) = first["scan_floors"].values()
            # Too long to hand back: the floor names the kept row by its key.
            self.assertEqual(floor[0] is None, text == UnnamedFloorProgressTests.LONG_KEPT, floor)
            steps, result = _batch_steps(connection, cutoffs, 1, first["scan_floors"])
        self.assertEqual(_removed(result), 1)
        self.assertEqual(_texts(path, population), [text] * count)
        return steps

    def test_a_batch_after_a_floor_in_an_equal_timestamp_run_does_not_reread_the_run(self) -> None:
        forms = {"text": KEPT_TEXT, "key": UnnamedFloorProgressTests.LONG_KEPT}
        for encoding in ("UTF-8", "UTF-16le"):
            for population in POPULATION_CUTOFFS:
                for form, text in forms.items():
                    with self.subTest(encoding=encoding, population=population, floor=form):
                        steps = {
                            count: self._later_batch_steps(population, text, count, encoding)
                            for count in (self.SMALL, self.LARGE)
                        }
                        self.assertLessEqual(steps[self.LARGE] - steps[self.SMALL], self.SLACK, steps)

    def test_fixed_size_batches_through_an_equal_timestamp_run_do_constant_work(self) -> None:
        # Batches of one, each starting from the floor the one before left:
        # the thousandth costs what the second (the first with a floor) does.
        for population, cutoffs in POPULATION_CUTOFFS.items():
            with self.subTest(population=population):
                path = _database(self, f"{population}-fixed.db")
                _insert(path, population, [KEPT_TEXT] * 2100 + [self.EXPIRED])
                floors: dict = {}
                steps: dict[int, int] = {}
                with closing(sqlite3.connect(path)) as connection:
                    for number in range(1, 1001):
                        steps[number], result = _batch_steps(connection, cutoffs, 1, floors)
                        self.assertTrue(result["has_more"])
                        self.assertEqual(_removed(result), 0)
                        floors = result["scan_floors"]
                sampled = {number: steps[number] for number in (1, 2, 100, 1000)}
                self.assertLessEqual(max(steps[100], steps[1000]) - steps[2], self.SLACK, sampled)
                self.assertEqual(len(_texts(path, population)), 2101)


class BoundedPagePlanTests(unittest.TestCase):
    """The page, the key deletes and the remaining check never scan a table."""

    def test_every_statement_of_a_paged_pass_searches_an_index_or_the_key(self) -> None:
        path = _database(self)
        store = HistoryStore(str(path))
        texts = (
            [KEPT_TEXT] * 30 + ["2026-11-14T07:00:00+00:00"] * 30 + ["2026-11-01T00:00:00+00:00"] * 30
            + [UnnamedFloorProgressTests.LONG_KEPT] * 10
        )
        for population in POPULATION_DAYS:
            _insert(path, population, texts)
        statements: list[str] = []
        connect = store._connect_locked

        def tracing_connect() -> sqlite3.Connection:
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection

        store._connect_locked = tracing_connect  # type: ignore[method-assign]
        result = _retain(
            store, NOW, batch_size=4, max_batches=1000,
            raw_metric_retention_days=1, event_retention_days=1,
            hourly_rollup_retention_days=1, daily_rollup_retention_days=1,
        )
        self.assertFalse(result["has_more"])
        retained = ("FROM slot_events", "FROM metric_samples", "FROM metric_rollups")
        paged = [
            sql for sql in dict.fromkeys(statements)
            if sql.lstrip().upper().startswith(("SELECT", "DELETE")) and any(name in sql for name in retained)
        ]
        kinds = {" ".join(sql.split()[:4]) for sql in paged}
        self.assertTrue(any("json_each" in sql for sql in paged), kinds)
        self.assertTrue(any(sql.startswith("SELECT EXISTS(SELECT 1 FROM") for sql in paged), kinds)
        # A floor named by key: the kept rows too long to hand back.
        self.assertTrue(any("FROM slot_events WHERE id = " in sql for sql in paged), kinds)
        with closing(sqlite3.connect(path)) as connection:
            HistoryStore._register_retention_functions(connection)
            for sql in paged:
                parameters = ["2026-11-14T00:00:00+00:00"] * sql.count("?")
                plan = [row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)]
                with self.subTest(statement=" ".join(sql.split())[:70]):
                    for table in ("slot_events", "metric_samples", "metric_rollups"):
                        self.assertFalse(any(line.startswith(f"SCAN {table}") for line in plan), plan)
                    self.assertFalse(any("TEMP B-TREE" in line for line in plan), plan)


OFFSETS = ("-14:59", "-12:00", "-05:00", "-01:00", "+00:00", "Z", "+05:30", "+09:00", "+14:59")


def _mixed_offset_texts(seed: int, count: int) -> list[str]:
    """Timestamps around the cutoff window, in many offsets and spellings."""

    generator = random.Random(seed)
    texts = []
    for _ in range(count):
        local = datetime(2026, 11, 12) + timedelta(seconds=generator.randrange(4 * 86400))
        separator = generator.choice("T ")
        fraction = generator.choice(("", f".{generator.randrange(10**6):06d}"))
        texts.append(f"{local:%Y-%m-%d}{separator}{local:%H:%M:%S}{fraction}{generator.choice(OFFSETS)}")
    return texts


def _expired(text: str, now: datetime) -> bool:
    """The exact-time rule, read in Python, not by the store."""

    cutoff = now - timedelta(days=1)
    cutoff_text = cutoff.isoformat()
    settled = (cutoff - timedelta(days=1)).date().isoformat()
    if not text < cutoff_text:
        return False
    if text < settled:
        return True
    return datetime.fromisoformat(text) < cutoff


class MixedOffsetResultTests(unittest.TestCase):
    """Which rows are removed, and by which pass, does not depend on paging."""

    NOWS = (NOW, NOW + timedelta(hours=3), NOW + timedelta(hours=11), NOW + timedelta(days=1, hours=1))

    def test_each_completed_pass_removes_exactly_the_rows_the_exact_time_rule_expires(self) -> None:
        for population, days in POPULATION_DAYS.items():
            texts = _mixed_offset_texts(seed=1268 + len(population), count=150)
            for batch_size in (1, 3, 50):
                with self.subTest(population=population, batch_size=batch_size):
                    path = _database(self, f"{population}-{batch_size}.db")
                    store = HistoryStore(str(path))
                    _insert(path, population, texts)
                    remaining = sorted(texts)
                    for now in self.NOWS:
                        result = _retain(store, now, batch_size=batch_size, max_batches=1000, **days)
                        self.assertFalse(result["has_more"])
                        expected = [text for text in remaining if not _expired(text, now)]
                        self.assertEqual(_texts(path, population), expected, now)
                        self.assertEqual(result["total_rows_removed"], len(remaining) - len(expected))
                        remaining = expected
                    if population == "metrics":
                        with closing(sqlite3.connect(path)) as connection:
                            rolled = connection.execute(
                                "SELECT bucket_seconds, SUM(sample_count) FROM metric_rollups "
                                "GROUP BY bucket_seconds ORDER BY bucket_seconds"
                            ).fetchall()
                        self.assertEqual(
                            rolled, [(3600, len(texts) - len(remaining)), (86400, len(texts) - len(remaining))]
                        )


def _candidate(root: Path, name: str, column: str, value: object) -> Path:
    """A valid history database with one timestamp value in one retained column."""

    path = root / name
    HistoryStore(str(path))
    literal = f"CAST(x'{value.hex()}' AS TEXT)" if isinstance(value, bytes) else "?"
    parameters = () if isinstance(value, bytes) else (value,)
    with closing(sqlite3.connect(path)) as connection:
        if column == "slot_events.observed_at":
            connection.execute(
                "INSERT INTO slot_events (observed_at, system_id, enclosure_key, slot, slot_label, "
                f"event_type, details_json) VALUES ({literal}, ?, '', 0, '0', 'slot_state_changed', '{{}}')",
                (*parameters, SYSTEM_ID),
            )
        elif column == "metric_samples.observed_at":
            connection.execute(
                "INSERT INTO metric_samples (observed_at, system_id, enclosure_key, slot, slot_label, "
                f"metric_name, value_integer) VALUES ({literal}, ?, '', 0, '0', 'bytes_read', 1)",
                (*parameters, SYSTEM_ID),
            )
        else:
            connection.execute(
                "INSERT INTO metric_rollups (bucket_start, bucket_seconds, system_id, enclosure_key, "
                "slot, slot_label, metric_name, sample_count, value_sum, value_min, value_max, "
                f"last_value, last_observed_at) VALUES ({literal}, 3600, ?, '', 0, '0', 'bytes_read', "
                "1, 1, 1, 1, 1, '2026-11-14T08:00:00+00:00')",
                (*parameters, SYSTEM_ID),
            )
        connection.commit()
    return path


RETAINED_COLUMNS = ("slot_events.observed_at", "metric_samples.observed_at", "metric_rollups.bucket_start")
UNORDERABLE = {
    "no offset": "2026-11-14T08:00:00",
    "offset of a day or more": "2026-11-14T08:00:00+25:00",
    "basic format": "20261114T080000-0500",
    "week date": "2026-W46-6T08:00:00+00:00",
    "not a time": "yesterday",
    "not text in the database encoding": b"2026-11-14T08:00:00+00:0\xff",
}
ORDERABLE = (
    "2026-11-14T08:00:00-05:00",
    "2026-11-14 13:00:00Z",
    "2026-11-13T21:01:01-14:59",
    "2026-11-15T02:58:59+14:59",
    "2025-01-01T00:00:00.123456789+00:00",
    "2026-11-14T12:00:00.000001+00:00",
)


class RestoreOrderableTimestampTests(unittest.TestCase):
    """A restore accepts only timestamps retention can order (#1268)."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="restore-order-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        live = self.root / "live"
        live.mkdir()
        self.live = live / "history.sqlite3"
        self.store = HistoryStore(str(self.live), recover_unreadable_database=False)
        _insert(self.live, "events", ["2026-11-14T10:00:00+00:00"])

    def _live_events(self) -> list[str]:
        return _texts(self.live, "events")

    def test_restore_and_member_validation_refuse_a_timestamp_retention_cannot_order(self) -> None:
        for column in RETAINED_COLUMNS:
            for label, value in UNORDERABLE.items():
                with self.subTest(column=column, value=label):
                    candidate = _candidate(self.root, f"{column}-{abs(hash(label))}.sqlite3", column, value)
                    for check in (
                        lambda: SystemBackupService._validate_history_member(candidate),
                        lambda: self.store.restore_backup(candidate),
                    ):
                        with self.assertRaises(ValueError) as refused:
                            check()
                        message = str(refused.exception)
                        self.assertIn(f"1 {column} timestamp", message)
                        self.assertIn("cannot order", message)
                        self.assertIn("Nothing was restored", message)
                    self.assertEqual(self._live_events(), ["2026-11-14T10:00:00+00:00"])

    def test_every_offending_row_is_counted(self) -> None:
        candidate = _candidate(self.root, "two.sqlite3", "slot_events.observed_at", "2026-11-14T08:00:00")
        _insert(candidate, "events", ["2026-11-14T09:00:00", "2026-11-14T09:00:00+00:00"])
        with self.assertRaises(ValueError) as refused:
            self.store.restore_backup(candidate)
        self.assertIn("2 slot_events.observed_at timestamps", str(refused.exception))

    def test_a_timestamp_stored_as_a_blob_is_refused_and_the_same_text_is_admitted(self) -> None:
        # A BLOB never sorts before a TEXT cutoff, so retention would keep it
        # forever, however valid the ISO text its bytes hold.
        text = "2020-01-01T00:00:00+00:00"
        for column in RETAINED_COLUMNS:
            table, name = column.split(".")
            for storage in ("blob", "text"):
                with self.subTest(column=column, storage=storage):
                    candidate = _candidate(self.root, f"{column}-{storage}.sqlite3", column, text)
                    with closing(sqlite3.connect(candidate)) as connection:
                        connection.execute(f"UPDATE {table} SET {name} = CAST({name} AS {storage.upper()})")
                        connection.commit()
                        stored = connection.execute(f"SELECT typeof({name}), count(*) FROM {table}").fetchone()
                    self.assertEqual(stored, (storage, 1))
                    checks = (
                        lambda: SystemBackupService._validate_history_member(candidate),
                        lambda: self.store.restore_backup(candidate),
                    )
                    if storage == "text":
                        for check in checks:
                            check()
                        with closing(sqlite3.connect(self.live)) as connection:
                            restored = connection.execute(f"SELECT typeof({name}), {name} FROM {table}").fetchall()
                        self.assertEqual(restored, [("text", text)])
                        continue
                    before = self._live_events()
                    for check in checks:
                        with self.assertRaises(ValueError) as refused:
                            check()
                        message = str(refused.exception)
                        self.assertIn(f"1 {column} timestamp", message)
                        self.assertIn("cannot order", message)
                        self.assertIn("stored as text", message)
                        self.assertIn("Nothing was restored", message)
                    self.assertEqual(self._live_events(), before)

    def test_mixed_offset_timestamps_are_restored(self) -> None:
        # Control: every orderable spelling, in every retained column.
        candidate = _candidate(self.root, "orderable.sqlite3", "slot_events.observed_at", ORDERABLE[0])
        _insert(candidate, "events", list(ORDERABLE[1:]))
        _insert(candidate, "metrics", list(ORDERABLE))
        _insert(candidate, "3600", list(ORDERABLE))
        SystemBackupService._validate_history_member(candidate)
        self.store.restore_backup(candidate)
        self.assertEqual(self._live_events(), sorted(ORDERABLE))


if __name__ == "__main__":
    unittest.main()
