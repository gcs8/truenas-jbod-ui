"""Read-only segment census and conversion-candidate validator (#864)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock, patch

from history_service import main as history_main
from history_service import segment_census
from history_service.segment_catalog import MIGRATION_PENDING_MARKER
from history_service.segment_census import (
    CHRONOLOGICAL_INDEX_COLUMNS,
    LIMITED_READ_SHAPES,
    SegmentCensus,
    limited_read_plan_failures,
    released_index_sql,
    run_segment_census,
    table_content_digests,
    validate_conversion_candidate,
)
from history_service.segment_reader import SegmentedHistoryReader
from history_service.segment_sealer import HISTORY_TABLE_TIMESTAMPS
from history_service.store import CHRONOLOGICAL_INDEXES, SCHEMA, HistoryStore

ALL_INDEXES = tuple(name for name, _ in CHRONOLOGICAL_INDEXES)
NON_DISK_INDEXES = ALL_INDEXES[:3]


def _stamp(day: int, index: int) -> str:
    # Mixed offsets: the -12:00 rows sort earlier as text but are later instants.
    if index % 2:
        return f"2025-01-{day:02d}T{index % 12:02d}:{index % 60:02d}:00-12:00"
    return f"2025-01-{day:02d}T{index % 24:02d}:{index % 60:02d}:30+00:00"


def build_segment(
    path: Path,
    *,
    day: int = 1,
    rows: int = 40,
    indexes: tuple[str, ...] = ALL_INDEXES,
    legacy_columns: bool = False,
) -> None:
    """A synthetic sealed segment with exactly ``indexes`` of the chronological set."""

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        if legacy_columns:
            connection.execute("ALTER TABLE metric_samples DROP COLUMN disk_identity_key")
        disk_column = "" if legacy_columns else ", disk_identity_key"
        disk_value = "" if legacy_columns else ", ?"
        for index in range(rows):
            stamp = _stamp(day, index)
            slot = 1 + index % 2
            connection.execute(
                """INSERT INTO slot_events (
                    observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                ) VALUES (?, 'system-1', 'enclosure-1', ?, 'slot', 'state', '{}')""",
                (stamp, slot),
            )
            connection.execute(
                f"""INSERT INTO metric_samples (
                    observed_at, system_id, enclosure_key, slot, slot_label, metric_name,
                    value_integer, value_real{disk_column}
                ) VALUES (?, 'system-1', 'enclosure-1', ?, 'slot', ?, ?, ?{disk_value})""",
                (
                    stamp, slot, "temperature" if index % 3 else "read_errors",
                    index if index % 4 else None, None if index % 4 else index / 7,
                    *(() if legacy_columns else (f"disk-{'ab'[index % 2]}",)),
                ),
            )
            connection.execute(
                """INSERT INTO metric_rollups (
                    bucket_start, bucket_seconds, system_id, enclosure_key, slot, slot_label,
                    metric_name, sample_count, value_sum, value_min, value_max, last_value,
                    last_observed_at, disk_identity_key
                ) VALUES (?, 3600, 'system-1', 'enclosure-1', ?, 'slot', 'temperature',
                          2, 61.5, 30, 31.5, 31.5, ?, ?)""",
                (stamp, slot, stamp, f"disk-{'ab'[index % 2]}"),
            )
        existing = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        statements = dict(CHRONOLOGICAL_INDEXES)
        for name in ALL_INDEXES:
            if name in indexes and name not in existing:
                connection.execute(statements[name])
            elif name not in indexes and name in existing:
                connection.execute(f"DROP INDEX {name}")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("VACUUM")


def catalog_entry(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        stamps = [
            row[0]
            for table, column in HISTORY_TABLE_TIMESTAMPS.items()
            for row in connection.execute(f"SELECT {column} FROM {table}")
        ]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in HISTORY_TABLE_TIMESTAMPS
        }
    ordered = sorted(stamps, key=datetime.fromisoformat)
    return {
        "segment_id": path.stem,
        "file_name": path.name,
        "sequence": int(path.stem.rsplit("-", 1)[1]),
        "coverage_start": ordered[0] if ordered else None,
        "coverage_end": ordered[-1] if ordered else None,
        "sealed_at": "2025-02-01T00:00:00+00:00",
        "key_id": "synthetic-key",
        "row_counts": counts,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def write_catalog(catalog: Path, entries: list[dict[str, Any]]) -> None:
    catalog.write_text(
        json.dumps({
            "catalog_version": 1, "generation_id": "generation-0001", "complete": True,
            "segments": entries,
        }),
        encoding="utf-8",
    )


def build_candidate(original: Path, candidate: Path, *, statements: tuple[str, ...] | None = None) -> None:
    """What a later publisher would build: a backup copy plus the missing released indexes."""

    with (
        closing(sqlite3.connect(f"{original.as_uri()}?mode=ro&immutable=1", uri=True)) as source,
        closing(sqlite3.connect(candidate)) as destination,
    ):
        source.backup(destination)
        destination.execute("PRAGMA journal_mode = DELETE")
        present = {
            row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        columns = {row[1] for row in destination.execute("PRAGMA table_info(metric_samples)")}
        if statements is None:
            statements = tuple(
                statement
                for name, statement in CHRONOLOGICAL_INDEXES
                if name not in present and CHRONOLOGICAL_INDEX_COLUMNS[name][1] <= columns | {"id"}
            )
        with destination:
            for statement in statements:
                destination.execute(statement)


def mutate(path: Path, *statements: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in statements:
            connection.execute(statement)


def tree_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    snapshot: dict[str, tuple[Any, ...]] = {}
    for directory, names, files in os.walk(root):
        for name in sorted(names + files):
            path = Path(directory) / name
            metadata = path.lstat()
            content = path.read_bytes() if path.is_file() else b""
            snapshot[str(path.relative_to(root))] = (
                metadata.st_mode, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns, metadata.st_ctime_ns, hashlib.sha256(content).hexdigest(),
            )
    return snapshot


class SegmentTreeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.segments = self.root / "segments"
        self.segments.mkdir()
        self.catalog = self.segments / "catalog.json"
        self.hot = self.root / "history.sqlite3"
        with closing(sqlite3.connect(self.hot)) as connection:
            connection.executescript(SCHEMA)
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def segment(self, number: int, **kwargs: Any) -> Path:
        path = self.segments / f"segment-{number:04d}.sqlite3"
        build_segment(path, day=number, **kwargs)
        return path

    def validate(self, segment_id: str, candidate: Path) -> segment_census.CandidateValidation:
        return validate_conversion_candidate(
            catalog_path=self.catalog, segment_id=segment_id, candidate_path=candidate,
            hot_path=self.hot,
        )


class ReleasedIndexContractTests(unittest.TestCase):
    def test_column_map_covers_exactly_the_released_indexes(self) -> None:
        self.assertEqual(set(CHRONOLOGICAL_INDEX_COLUMNS), set(ALL_INDEXES))
        for name, statement in CHRONOLOGICAL_INDEXES:
            table, columns = CHRONOLOGICAL_INDEX_COLUMNS[name]
            self.assertIn(f"ON {table} (", " ".join(statement.split()))
            for column in columns:
                self.assertIn(column, statement)

    def test_released_sql_is_what_sqlite_stores(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript(SCHEMA)
            for _, statement in CHRONOLOGICAL_INDEXES:
                connection.execute(statement)
            stored = dict(connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'"))
        for name in ALL_INDEXES:
            self.assertEqual(stored[name], released_index_sql(name))

    def test_shapes_match_the_reader_limited_read_order(self) -> None:
        # The validator's plan check must name the reader's own ORDER BY.
        source = Path(segment_census.__file__).with_name("segment_reader.py").read_text(encoding="utf-8")
        self.assertIn("ORDER BY julianday(observed_at) DESC, id DESC", source)
        self.assertIn("julianday(observed_at) >= julianday(?)", source)
        self.assertEqual(
            {table for table, _ in LIMITED_READ_SHAPES.values()}, {"slot_events", "metric_samples"},
        )


class SegmentCensusTests(SegmentTreeFixture):
    def test_census_authenticates_and_classifies_each_catalog_segment(self) -> None:
        legacy = self.segment(1, indexes=())
        indexed = self.segment(2)
        legacy_shape = self.segment(3, indexes=NON_DISK_INDEXES, legacy_columns=True)
        corrupted = self.segment(4, indexes=())
        mismatched = self.segment(5, indexes=())
        missing = self.segment(6, indexes=())
        entries = [catalog_entry(path) for path in (legacy, indexed, legacy_shape, corrupted, mismatched, missing)]
        entries[4]["size_bytes"] += 4096
        write_catalog(self.catalog, entries)
        size_before = corrupted.stat().st_size
        # Corrupt one metric value; every count and the coverage stay equal.
        mutate(corrupted, "UPDATE metric_samples SET value_integer = value_integer + 1 WHERE id = 2")
        self.assertEqual(corrupted.stat().st_size, size_before)
        missing.unlink()

        report = run_segment_census(self.catalog, hot_path=self.hot)

        by_id = {entry.segment_id: entry for entry in report.segments}
        self.assertIsNone(report.catalog_error)
        self.assertEqual(by_id["segment-0001"].missing_indexes, ALL_INDEXES)
        self.assertTrue(by_id["segment-0001"].candidate)
        self.assertFalse(by_id["segment-0001"].indexed)
        self.assertEqual(by_id["segment-0001"].coverage_end, entries[0]["coverage_end"])
        self.assertEqual(by_id["segment-0001"].size_bytes, entries[0]["size_bytes"])
        self.assertTrue(by_id["segment-0002"].indexed)
        self.assertFalse(by_id["segment-0002"].candidate)
        # Without disk_identity_key the two disk indexes are not expected.
        self.assertTrue(by_id["segment-0003"].indexed)
        self.assertEqual(by_id["segment-0003"].missing_indexes, ())
        self.assertEqual(by_id["segment-0004"].error, "segment_sha256_mismatch")
        self.assertEqual(by_id["segment-0005"].error, "segment_size_mismatch")
        self.assertEqual(by_id["segment-0006"].error, "segment_missing")
        for segment_id in ("segment-0004", "segment-0005", "segment-0006"):
            self.assertFalse(by_id[segment_id].authenticated)
            self.assertFalse(by_id[segment_id].candidate)
        summary = report.summary()
        self.assertEqual(
            {key: summary[key] for key in ("state", "indexed", "unindexed", "candidates")},
            {"state": "census", "indexed": 2, "unindexed": 1, "candidates": 1},
        )
        self.assertEqual(summary["candidate_bytes"], entries[0]["size_bytes"])
        self.assertEqual(
            summary["errors"],
            [
                {"segment_id": "segment-0004", "reason": "segment_sha256_mismatch"},
                {"segment_id": "segment-0005", "reason": "segment_size_mismatch"},
                {"segment_id": "segment-0006", "reason": "segment_missing"},
            ],
        )
        self.assertEqual(summary["last_census_at"], report.completed_at)
        self.assertNotIn(str(self.root), json.dumps(summary))

    def test_partial_or_nonstandard_indexes_are_reported_but_not_candidates(self) -> None:
        partial = self.segment(1, indexes=NON_DISK_INDEXES)
        nonstandard = self.segment(2, indexes=())
        mutate(
            nonstandard,
            "CREATE INDEX idx_slot_events_scope_chronological ON slot_events (system_id, observed_at)",
        )
        write_catalog(self.catalog, [catalog_entry(partial), catalog_entry(nonstandard)])

        by_id = {entry.segment_id: entry for entry in run_segment_census(self.catalog).segments}

        self.assertEqual(by_id["segment-0001"].missing_indexes, ALL_INDEXES[3:])
        self.assertTrue(by_id["segment-0001"].candidate)
        self.assertEqual(by_id["segment-0002"].error, "segment_nonstandard_index")
        self.assertTrue(by_id["segment-0002"].authenticated)
        self.assertFalse(by_id["segment-0002"].candidate)
        self.assertFalse(by_id["segment-0002"].indexed)

    def test_catalog_states_are_reported_without_scanning(self) -> None:
        self.assertEqual(run_segment_census(self.catalog).catalog_error, "catalog_missing")
        self.catalog.write_text('{"catalog_version": 1, "complete": true, "complete": true}', encoding="utf-8")
        self.assertEqual(run_segment_census(self.catalog).catalog_error, "catalog_invalid")
        write_catalog(self.catalog, [])
        (self.segments / MIGRATION_PENDING_MARKER).write_text("{}", encoding="utf-8")
        report = run_segment_census(self.catalog)
        self.assertEqual(report.catalog_error, "recovery_pending")
        self.assertEqual(
            report.summary()["errors"], [{"segment_id": None, "reason": "recovery_pending"}],
        )

    def test_sidecar_and_hard_link_segments_fail_authentication(self) -> None:
        sidecar = self.segment(1, indexes=())
        linked = self.segment(2, indexes=())
        write_catalog(self.catalog, [catalog_entry(sidecar), catalog_entry(linked)])
        Path(f"{sidecar}-wal").write_bytes(b"")
        os.link(linked, self.staging / "extra-link.sqlite3")

        by_id = {entry.segment_id: entry for entry in run_segment_census(self.catalog).segments}

        self.assertEqual(by_id["segment-0001"].error, "segment_sidecar_state")
        self.assertEqual(by_id["segment-0002"].error, "segment_not_regular")

    def test_sidecar_appearing_or_disappearing_refreshes_the_cached_census(self) -> None:
        legacy = self.segment(1, indexes=())
        write_catalog(self.catalog, [catalog_entry(legacy)])
        wal = Path(f"{legacy}-wal")
        wal.write_bytes(b"")
        started: list[Callable[[], None]] = []
        census = SegmentCensus(catalog_path=self.catalog, min_interval_seconds=0, start_background=started.append)
        census.summary()
        started.pop()()
        self.assertEqual(
            census.summary()["errors"], [{"segment_id": "segment-0001", "reason": "segment_sidecar_state"}],
        )
        self.assertEqual(started, [])
        wal.unlink()
        census.summary()
        self.assertEqual(len(started), 1, "a removed sidecar makes the census stale")
        started.pop()()
        self.assertEqual(census.summary()["candidates"], 1)
        Path(f"{legacy}-shm").write_bytes(b"")
        census.summary()
        self.assertEqual(len(started), 1, "a new sidecar makes the census stale")

    def test_segment_changed_later_in_the_same_pass_is_not_cached_as_fresh(self) -> None:
        first = self.segment(1, indexes=())
        second = self.segment(2, indexes=())
        write_catalog(self.catalog, [catalog_entry(first), catalog_entry(second)])
        inspect = segment_census._census_entry

        def corrupt_first_after_second(directory: Path, entry: Any) -> Any:
            result = inspect(directory, entry)
            if entry.segment_id == "segment-0002":
                mutate(first, "UPDATE metric_samples SET value_integer = 0 WHERE id = 2")
            return result

        started: list[Callable[[], None]] = []
        census = SegmentCensus(catalog_path=self.catalog, min_interval_seconds=0, start_background=started.append)
        census.summary()
        with patch.object(segment_census, "_census_entry", side_effect=corrupt_first_after_second):
            started.pop()()
        self.assertEqual(census.summary()["candidates"], 2, "the pass itself saw two good segments")
        self.assertEqual(len(started), 1, "the change after inspection makes the result stale")
        started.pop()()
        self.assertEqual(
            census.summary()["errors"], [{"segment_id": "segment-0001", "reason": "segment_sha256_mismatch"}],
        )

    def test_sidecar_created_during_the_census_pass_fails_authentication(self) -> None:
        legacy = self.segment(1, indexes=())
        write_catalog(self.catalog, [catalog_entry(legacy)])
        inventory = segment_census.chronological_index_inventory

        def create_wal(connection: sqlite3.Connection) -> Any:
            Path(f"{legacy}-wal").write_bytes(b"")
            return inventory(connection)

        with patch.object(segment_census, "chronological_index_inventory", side_effect=create_wal):
            report = run_segment_census(self.catalog)
        self.assertEqual(report.segments[0].error, "segment_sidecar_state")
        self.assertFalse(report.segments[0].authenticated)

    def test_cached_summary_never_hashes_and_refreshes_only_after_change(self) -> None:
        legacy = self.segment(1, indexes=())
        write_catalog(self.catalog, [catalog_entry(legacy)])
        clock = [1000.0]
        started: list[Callable[[], None]] = []
        census = SegmentCensus(
            catalog_path=self.catalog, hot_path=self.hot, min_interval_seconds=300,
            monotonic=lambda: clock[0], start_background=started.append,
        )

        first = census.summary()
        self.assertEqual(first["state"], "census")
        self.assertIsNone(first["indexed"])
        self.assertIsNone(first["last_census_at"])
        self.assertEqual(len(started), 1)
        self.assertEqual(census.summary()["indexed"], None)
        self.assertEqual(len(started), 1, "one census pass at a time")
        started.pop()()

        summary: dict[str, Any] = {}
        with patch.object(segment_census, "_hash_descriptor", side_effect=AssertionError("hashed")):
            for _ in range(5):
                summary = census.summary()
        self.assertEqual((summary["unindexed"], summary["candidates"]), (1, 1))
        self.assertEqual(started, [])

        indexed = self.segment(2)
        write_catalog(self.catalog, [catalog_entry(legacy), catalog_entry(indexed)])
        census.summary()
        self.assertEqual(started, [], "the minimum interval holds a changed catalog")
        clock[0] += 301
        self.assertEqual(census.summary()["indexed"], 0, "the cached result is served meanwhile")
        self.assertEqual(len(started), 1)
        started.pop()()
        self.assertEqual(census.summary()["indexed"], 1)

        # A segment file replaced in place, under an unchanged catalog, is noticed.
        clock[0] += 301
        mutate(legacy, "UPDATE metric_samples SET value_integer = 7 WHERE id = 3")
        census.summary()
        self.assertEqual(len(started), 1)
        started.pop()()
        self.assertEqual(
            census.summary()["errors"], [{"segment_id": "segment-0001", "reason": "segment_sha256_mismatch"}],
        )

    def test_background_failure_is_informational(self) -> None:
        write_catalog(self.catalog, [])
        started: list[Callable[[], None]] = []
        census = SegmentCensus(catalog_path=self.catalog, start_background=started.append)
        census.summary()
        with (
            patch.object(segment_census, "run_segment_census", side_effect=RuntimeError("boom")),
            self.assertLogs(segment_census.logger, "WARNING"),
        ):
            started.pop()()
        summary = census.summary()
        self.assertEqual(summary["errors"], [{"segment_id": None, "reason": "census_failed"}])
        self.assertEqual(summary["state"], "census")


class CandidateValidationTests(SegmentTreeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.legacy = self.segment(1, indexes=())
        self.indexed = self.segment(2)
        self.legacy_shape = self.segment(3, indexes=(), legacy_columns=True)
        write_catalog(self.catalog, [catalog_entry(path) for path in (self.legacy, self.indexed, self.legacy_shape)])

    def candidate_for(self, original: Path, name: str = "candidate.sqlite3", **kwargs: Any) -> Path:
        candidate = self.staging / name
        build_candidate(original, candidate, **kwargs)
        return candidate

    def test_valid_candidate_passes_every_check(self) -> None:
        candidate = self.candidate_for(self.legacy)

        result = self.validate("segment-0001", candidate)

        self.assertEqual(result.failures, ())
        self.assertTrue(result.ok)
        self.assertEqual(result.added_indexes, ALL_INDEXES)
        self.assertEqual(result.candidate_sha256, hashlib.sha256(candidate.read_bytes()).hexdigest())
        self.assertEqual(result.candidate_size_bytes, candidate.stat().st_size)
        self.assertIsNotNone(result.candidate_identity)
        assert result.candidate_identity is not None
        self.assertEqual(result.candidate_identity.inode, candidate.stat().st_ino)
        self.assertEqual(result.original_sha256, catalog_entry(self.legacy)["sha256"])
        self.assertEqual(
            {table: result.table_counts[table] for table in HISTORY_TABLE_TIMESTAMPS},
            catalog_entry(self.legacy)["row_counts"],
        )
        self.assertRegex(result.content_digest or "", r"\A[0-9a-f]{64}\Z")

    def test_legacy_column_shape_expects_only_the_indexes_its_columns_allow(self) -> None:
        candidate = self.candidate_for(self.legacy_shape)
        result = self.validate("segment-0003", candidate)
        self.assertEqual(result.failures, ())
        self.assertEqual(result.added_indexes, NON_DISK_INDEXES)

    def test_corrupted_original_with_matching_counts_fails_authentication(self) -> None:
        # Sol review #1: a copy of a corrupted original has equal counts,
        # coverage and content digests; only the catalog entry catches it.
        mutate(self.legacy, "UPDATE metric_samples SET value_integer = value_integer + 1 WHERE id = 2")
        candidate = self.candidate_for(self.legacy)
        with (
            closing(sqlite3.connect(f"{self.legacy.as_uri()}?mode=ro&immutable=1", uri=True)) as original,
            closing(sqlite3.connect(f"{candidate.as_uri()}?mode=ro&immutable=1", uri=True)) as copy,
        ):
            self.assertEqual(table_content_digests(original), table_content_digests(copy))

        result = self.validate("segment-0001", candidate)

        self.assertEqual(result.failures, ("original_sha256_mismatch",))
        self.assertFalse(result.ok)
        self.assertIsNone(result.candidate_sha256)

    def test_original_catalog_mismatch_and_missing_file_fail(self) -> None:
        candidate = self.candidate_for(self.legacy)
        entries = json.loads(self.catalog.read_text(encoding="utf-8"))["segments"]
        entries[0]["size_bytes"] -= 1
        write_catalog(self.catalog, entries)
        self.assertEqual(self.validate("segment-0001", candidate).failures, ("original_size_mismatch",))
        entries[0]["size_bytes"] += 1
        entries[0]["sha256"] = "0" * 64
        write_catalog(self.catalog, entries)
        self.assertEqual(self.validate("segment-0001", candidate).failures, ("original_sha256_mismatch",))
        self.assertEqual(self.validate("segment-0009", candidate).failures, ("catalog_entry_missing",))
        self.indexed.unlink()
        self.assertEqual(self.validate("segment-0002", candidate).failures, ("original_missing",))

    def test_already_indexed_original_has_nothing_to_validate(self) -> None:
        candidate = self.candidate_for(self.indexed, statements=())
        self.assertEqual(self.validate("segment-0002", candidate).failures, ("original_already_indexed",))

    def test_each_candidate_difference_is_refused(self) -> None:
        cases: dict[str, tuple[Callable[[Path], None], tuple[str, ...]]] = {
            "value": (
                lambda path: mutate(path, "UPDATE metric_samples SET value_real = 0.5 WHERE id = 4"),
                ("content_digest_mismatch:metric_samples",),
            ),
            "typed_value": (
                # The same bytes stored as a BLOB instead of TEXT.
                lambda path: mutate(path, "UPDATE slot_events SET details_json = CAST('{}' AS BLOB) WHERE id = 3"),
                ("content_digest_mismatch:slot_events",),
            ),
            "row": (
                lambda path: mutate(path, "DELETE FROM slot_events WHERE id = 5"),
                ("row_count_mismatch:slot_events", "catalog_row_counts_mismatch"),
            ),
            "user_version": (
                lambda path: mutate(path, "PRAGMA user_version = 99"),
                ("user_version_mismatch",),
            ),
            "application_id": (
                lambda path: mutate(path, "PRAGMA application_id = 7"),
                ("application_id_mismatch",),
            ),
            "extra_index": (
                lambda path: mutate(path, "CREATE INDEX idx_extra ON slot_events (slot)"),
                ("unexpected_index_set",),
            ),
            "schema": (
                lambda path: mutate(path, "CREATE TABLE extra_table (id INTEGER PRIMARY KEY)"),
                ("schema_mismatch", "table_set_mismatch"),
            ),
            "original_index": (
                lambda path: mutate(path, "DROP INDEX idx_slot_events_scope"),
                ("original_index_changed",),
            ),
        }
        for label, (change, expected) in cases.items():
            with self.subTest(label):
                candidate = self.candidate_for(self.legacy, name=f"{label}.sqlite3")
                change(candidate)
                self.assertEqual(self.validate("segment-0001", candidate).failures, expected)

    def test_index_text_or_missing_index_is_refused_with_its_query_plan(self) -> None:
        statements = dict(CHRONOLOGICAL_INDEXES)
        wrong_text = statements["idx_slot_events_scope_chronological"].replace(", id DESC", "")
        candidate = self.candidate_for(
            self.legacy, name="wrong.sqlite3",
            statements=(wrong_text, *(statements[name] for name in ALL_INDEXES[1:])),
        )
        failures = self.validate("segment-0001", candidate).failures
        self.assertIn("unexpected_index_set", failures)
        candidate = self.candidate_for(
            self.legacy, name="missing.sqlite3",
            statements=tuple(statements[name] for name in ALL_INDEXES if name != ALL_INDEXES[0]),
        )
        failures = self.validate("segment-0001", candidate).failures
        self.assertEqual(
            failures,
            ("unexpected_index_set", "query_plan:slot_events_by_slot", "query_plan:slot_events_by_slot:since"),
        )

    def test_plan_check_rejects_the_unindexed_original(self) -> None:
        with closing(sqlite3.connect(f"{self.legacy.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
            failures = limited_read_plan_failures(connection)
        self.assertEqual(
            failures, [f"query_plan:{shape}{suffix}" for shape in LIMITED_READ_SHAPES for suffix in ("", ":since")],
        )

    def test_unusable_candidates_are_refused(self) -> None:
        sidecar = self.candidate_for(self.legacy, name="sidecar.sqlite3")
        Path(f"{sidecar}-journal").write_bytes(b"")
        self.assertEqual(self.validate("segment-0001", sidecar).failures, ("candidate_sidecar_state",))
        garbage = self.staging / "garbage.sqlite3"
        garbage.write_bytes(b"not a database" * 100)
        self.assertEqual(self.validate("segment-0001", garbage).failures, ("candidate_integrity_check",))
        symlink = self.staging / "symlink.sqlite3"
        symlink.symlink_to(self.candidate_for(self.legacy, name="target.sqlite3"))
        self.assertEqual(self.validate("segment-0001", symlink).failures, ("candidate_not_regular",))
        self.assertEqual(
            self.validate("segment-0001", self.staging / "absent.sqlite3").failures, ("candidate_missing",),
        )
        self.assertEqual(self.validate("segment-0001", self.legacy).failures, ("candidate_same_file",))

    def test_damaged_candidate_pages_fail_integrity(self) -> None:
        candidate = self.candidate_for(self.legacy)
        with closing(sqlite3.connect(candidate)) as connection:
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        data = bytearray(candidate.read_bytes())
        last_page = (page_count - 1) * page_size
        data[last_page + 8:last_page + page_size] = b"\xff" * (page_size - 8)
        candidate.write_bytes(bytes(data))
        result = self.validate("segment-0001", candidate)
        self.assertEqual(result.failures, ("candidate_integrity_check",))
        self.assertIsNone(result.content_digest)

    def test_original_or_catalog_changed_during_validation_is_refused(self) -> None:
        candidate = self.candidate_for(self.legacy)
        compare = segment_census._compare_candidate

        def replace_original(*args: Any) -> Any:
            result = compare(*args)
            shutil.copy2(self.legacy, self.staging / "copy.sqlite3")
            os.replace(self.staging / "copy.sqlite3", self.legacy)
            return result

        with patch.object(segment_census, "_compare_candidate", side_effect=replace_original):
            self.assertEqual(self.validate("segment-0001", candidate).failures, ("original_changed",))

        write_catalog(self.catalog, [catalog_entry(path) for path in (self.legacy, self.indexed, self.legacy_shape)])

        def rewrite_catalog(*args: Any) -> Any:
            result = compare(*args)
            entries = json.loads(self.catalog.read_text(encoding="utf-8"))["segments"]
            entries[0]["sealed_at"] = "2025-03-01T00:00:00+00:00"
            write_catalog(self.catalog, entries)
            return result

        with patch.object(segment_census, "_compare_candidate", side_effect=rewrite_catalog):
            self.assertEqual(self.validate("segment-0001", candidate).failures, ("catalog_changed",))

    def test_sidecar_created_during_validation_is_refused(self) -> None:
        compare = segment_census._compare_candidate
        for owner in ("candidate", "original"):
            with self.subTest(owner):
                candidate = self.candidate_for(self.legacy, name=f"{owner}-sidecar.sqlite3")
                target = candidate if owner == "candidate" else self.legacy
                wal = Path(f"{target}-wal")

                def create_wal(*args: Any) -> Any:
                    result = compare(*args)
                    wal.write_bytes(b"")
                    return result

                with patch.object(segment_census, "_compare_candidate", side_effect=create_wal):
                    result = self.validate("segment-0001", candidate)
                wal.unlink()
                self.assertEqual(result.failures, (f"{owner}_sidecar_state",))
                self.assertFalse(result.ok)

    def test_rollup_and_raw_reads_are_equal_on_original_and_candidate(self) -> None:
        # Rollup reads stay on their scan path (Sol review #5); only full-result
        # equality is promised for them.
        candidate_dir = self.root / "converted"
        candidate_dir.mkdir()
        candidate = candidate_dir / self.legacy.name
        build_candidate(self.legacy, candidate)
        readers = [
            SegmentedHistoryReader(hot_path=self.hot, segment_paths=[path]) for path in (self.legacy, candidate)
        ]
        for limit in range(1, 12):
            for since in (None, "2025-01-01T06:00:00+00:00", "2025-01-02T00:00:00+00:00"):
                for read in (
                    lambda reader: reader.list_slot_events("system-1", "enclosure-1", 1, limit=limit, since=since),
                    lambda reader: reader.list_raw_metric_samples(
                        "system-1", "enclosure-1", 1, limit=limit, since=since),
                    lambda reader: reader.list_raw_metric_samples(
                        "system-1", "enclosure-1", 2, metric_name="temperature", limit=limit, since=since),
                    lambda reader: reader.list_metric_samples(
                        "system-1", "enclosure-1", 1, metric_name="temperature", limit=limit, since=since),
                    lambda reader: reader.list_disk_metric_samples("disk-a", limit=limit, since=since),
                    lambda reader: reader.list_disk_metric_samples(
                        "disk-b", metric_name="temperature", limit=limit, since=since),
                ):
                    with self.subTest(limit=limit, since=since):
                        self.assertEqual(read(readers[0]), read(readers[1]))

    def test_census_and_validation_leave_every_file_byte_identical(self) -> None:
        corrupted = self.segment(4, indexes=())
        entries = [catalog_entry(path) for path in (self.legacy, self.indexed, self.legacy_shape, corrupted)]
        write_catalog(self.catalog, entries)
        mutate(corrupted, "UPDATE metric_samples SET value_integer = 0 WHERE id = 2")
        good = self.candidate_for(self.legacy, name="good.sqlite3")
        bad = self.candidate_for(self.legacy, name="bad.sqlite3")
        mutate(bad, "UPDATE slot_events SET slot_label = 'other' WHERE id = 1")
        before = tree_snapshot(self.root)
        real_connect = sqlite3.connect
        uris: list[str] = []

        def read_only_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            uris.append(str(database))
            self.assertTrue(kwargs.get("uri"), database)
            self.assertTrue(str(database).endswith("?mode=ro&immutable=1"), database)
            return real_connect(database, *args, **kwargs)

        with (
            patch.object(HistoryStore, "__init__", side_effect=AssertionError("HistoryStore constructed")),
            patch.object(segment_census.sqlite3, "connect", side_effect=read_only_connect),
        ):
            census = SegmentCensus(catalog_path=self.catalog, hot_path=self.hot, start_background=lambda run: run())
            census.summary()
            summary = census.summary()
            results = [
                self.validate("segment-0001", good),
                self.validate("segment-0001", bad),
                self.validate("segment-0004", good),
                self.validate("segment-0002", good),
            ]

        self.assertEqual(summary["candidates"], 2)
        self.assertEqual([result.ok for result in results], [True, False, False, False])
        self.assertTrue(uris)
        self.assertEqual(tree_snapshot(self.root), before)


class SegmentCensusSurfaceTests(unittest.TestCase):
    CENSUS = {
        "state": "census", "indexed": 3, "unindexed": 2, "candidates": 1, "candidate_bytes": 2048,
        "last_census_at": "2025-02-01T00:00:00+00:00",
        "errors": [{"segment_id": "segment-0004", "reason": "segment_sha256_mismatch"}],
        "errors_omitted": 0,
    }

    def _healthz(self, census: SegmentCensus | None) -> dict[str, Any]:
        with (
            patch.object(history_main, "startup_failure_reason", None),
            patch.object(history_main, "segment_census", census),
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.collector, "degraded_reason", return_value=None),
            patch.object(history_main.store, "database_size_bytes", return_value=4096),
        ):
            response = asyncio.run(history_main.healthz())
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    def test_healthz_reports_the_census_without_degrading(self) -> None:
        census = SegmentCensus(catalog_path=Path("/nonexistent/catalog.json"))
        with patch.object(census, "summary", return_value=dict(self.CENSUS)):
            payload = self._healthz(census)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNone(payload["detail"])
        self.assertEqual(payload["segment_conversion"], self.CENSUS)

    def test_healthz_census_failure_and_unsegmented_history_report_null(self) -> None:
        self.assertIsNone(self._healthz(None)["segment_conversion"])
        census = SegmentCensus(catalog_path=Path("/nonexistent/catalog.json"))
        with (
            patch.object(census, "summary", side_effect=RuntimeError("synthetic")),
            self.assertLogs(history_main.logger, "WARNING"),
        ):
            payload = self._healthz(census)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNone(payload["segment_conversion"])

    def test_dashboard_shows_the_census_as_informational(self) -> None:
        def render(census: dict[str, Any] | None) -> str:
            context = history_main.build_dashboard_context(
                request=MagicMock(), status={}, counts={}, scopes=[], app_version="test",
                segment_conversion=census,
            )
            return history_main.templates.env.get_template("dashboard.html").render(context)

        page = render(dict(self.CENSUS))
        self.assertIn('id="segment-census"', page)
        self.assertIn("not migration progress", page)
        self.assertIn('<dd id="segment-census-unindexed">2</dd>', page)
        self.assertIn('<dd id="segment-census-candidates">1 (2.0 KiB)</dd>', page)
        self.assertIn("segment-0004: segment_sha256_mismatch", page)
        self.assertNotIn('id="segment-census"', render(None))
        pending = render({**self.CENSUS, "indexed": None, "unindexed": None, "candidates": None,
                          "candidate_bytes": None, "last_census_at": None, "errors": []})
        self.assertIn("not run yet", pending)
        self.assertIn("not counted yet", pending)


if __name__ == "__main__":
    unittest.main()
