"""The retention SQL helpers live in `history_service.store_retention`.

`HistoryStore` keeps a method under each moved name, with the same kind,
signature and defaults, that passes the store module (and its class for the
class methods) to the helper. These tests pin that seam: the new module loads
without the store module and opens, ends or locks nothing; every name stays
on `HistoryStore`; a replacement made on the store module or on the class
still reaches the moved code; and the SQL text the helpers build and run is
byte-for-byte the released text. All data is synthetic and lives in a
temporary directory.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
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
from unittest import mock

import history_service.store as store_module
from history_service.domain import MetricSample
from history_service.store import HistoryStore

REPOSITORY = Path(__file__).resolve().parents[1]

# name -> (kind on HistoryStore, released signature without the receiver)
MOVED = {
    "_register_retention_functions": ("static", "(connection: 'sqlite3.Connection') -> 'None'"),
    "_newer_last_sample": ("static", "(new: 'str', old: 'str') -> 'str'"),
    "_retention_bounds": ("static", "(cutoff: 'str') -> 'tuple[str, str, str | None]'"),
    "_expired_timestamp_predicate": (
        "static",
        "(timestamp_column: 'str', key_column: 'str' = 'id', *, floored: 'bool') -> 'str'",
    ),
    "_retention_parameters": (
        "class",
        "(cutoff: 'str', floor: 'tuple[bytes | str, int] | None') -> 'tuple[Any, ...]'",
    ),
    "_expired_rows_subquery": (
        "class",
        "(table_name: 'str', timestamp_column: 'str', *, floored: 'bool') -> 'str'",
    ),
    "_deleted_floor_text": ("static", "(timestamp_column: 'str') -> 'str'"),
    "_advance_scan_floor": (
        "static",
        "(connection: 'sqlite3.Connection', floors: 'dict[str, tuple[bytes | str, int]]', name: 'str', "
        "deleted: 'list[Any]') -> 'int'",
    ),
    "_delete_expired_rows": (
        "class",
        "(connection: 'sqlite3.Connection', *, table_name: 'str', timestamp_column: 'str', "
        "cutoff: 'str | None', batch_size: 'int', floors: 'dict[str, tuple[bytes | str, int]]') -> 'int'",
    ),
    "_roll_up_metric_rows": (
        "class",
        "(connection: 'sqlite3.Connection', *, cutoff: 'str', batch_size: 'int', bucket_seconds: 'int', "
        "floor: 'tuple[bytes | str, int] | None', rollup_cutoff: 'str | None' = None) -> 'list[int]'",
    ),
    # Added with the bounded retention page (#1268), as is _remove_retention_page.
    "_roll_up_selected_rows": (
        "class",
        "(connection: 'sqlite3.Connection', *, selected_rows: 'str', selected_parameters: 'tuple[Any, ...]', "
        "bucket_seconds: 'int', rollup_cutoff: 'str | None' = None) -> 'list[int]'",
    ),
    "_delete_rollup_batch": (
        "class",
        "(connection: 'sqlite3.Connection', *, bucket_seconds: 'int', cutoff: 'str | None', batch_size: 'int', "
        "floors: 'dict[str, tuple[bytes | str, int]]') -> 'int'",
    ),
    "_retention_rows_exist": (
        "class",
        "(connection: 'sqlite3.Connection', table_name: 'str', timestamp_column: 'str', cutoff: 'str | None', "
        "*, floor: 'tuple[bytes | str, int] | None' = None) -> 'bool'",
    ),
    "_rollups_exist": (
        "class",
        "(connection: 'sqlite3.Connection', bucket_seconds: 'int', cutoff: 'str | None', *, "
        "floor: 'tuple[bytes | str, int] | None' = None) -> 'bool'",
    ),
    "_remove_retention_page": (
        "class",
        "(connection: 'sqlite3.Connection', *, name: 'str', table_name: 'str', timestamp_column: 'str', "
        "key_column: 'str', cutoff: 'str', batch_size: 'int', floors: 'dict[str, tuple[bytes | str | None, int]]', "
        "scope: 'tuple[str, tuple[Any, ...]]' = ('', ()), "
        "roll_up: 'Callable[[str, tuple[Any, ...]], None] | None' = None) -> 'tuple[int, bool]'",
    ),
}

BASE = datetime(2026, 1, 10, tzinfo=timezone.utc)
CUTOFF = (BASE - timedelta(days=10)).isoformat()
ROLLUP_CUTOFF = (BASE - timedelta(days=12)).isoformat()

# sha256 of every SQL text the helpers build and send in _exercise(), joined
# with NUL, as released before the move.
RELEASED_SQL_SHA256 = "e04eb97dc4ffae3e5418f3103b68c31765c7749831bc503a4017bae342d9dcfb"
# The same texts once rollup buckets are named from julianday() (#1320).
CURRENT_SQL_SHA256 = "900efb193811ec34cd3bc706c65ffb8148b39afc38120252b4b33e40ce5f882a"
# (released, current) end of each rollup bucket expression.
BUCKET_READING = ("+00:00', observed_at)", "+00:00', julianday(observed_at))")


def _store_retention():
    return importlib.import_module("history_service.store_retention")


class _Recorder:
    """A connection that records each statement and parameter tuple it runs."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        self.statements.append((sql, tuple(parameters)))
        return self.connection.execute(sql, parameters)


def _sample(index: int) -> MetricSample:
    observed = BASE - timedelta(hours=index * 3, minutes=index % 7)
    offset = timezone(timedelta(hours=(index % 5) - 2))
    return MetricSample(
        observed_at=observed.astimezone(offset).isoformat(),
        system_id="system-a",
        system_label="System A",
        enclosure_key="enclosure-a",
        enclosure_id="enclosure-a",
        enclosure_label="Shelf A",
        slot=index % 4,
        slot_label=f"{index % 4}",
        metric_name="temperature_c",
        value_integer=None if index % 3 else index,
        value_real=float(index) if index % 3 else None,
        device_name="da0",
        serial="SERIAL-A",
        model="Model A",
        state="healthy",
    )


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = HistoryStore(str(Path(directory.name) / "history.sqlite3"))
        self.store.insert_metric_samples([_sample(index) for index in range(120)])

    def connection(self) -> sqlite3.Connection:
        connection = self.store._connect()
        self.addCleanup(connection.close)
        return connection


class StoreRetentionModuleTests(unittest.TestCase):
    def test_store_retention_imports_without_the_store_module(self) -> None:
        child = (
            "import json, sys\n"
            "import history_service.store_retention\n"
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('history_service'))))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "PYTHONPATH": str(REPOSITORY)},
                cwd=directory,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
        loaded = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertIn("history_service.store_retention", loaded)
        self.assertNotIn("history_service.store", loaded)

        tree = ast.parse((REPOSITORY / "history_service" / "store_retention.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse(
            {name for name in imported if name.split(".")[0] in {"app", "admin_service", "history_service"}}
        )

    def test_store_retention_opens_ends_and_locks_nothing(self) -> None:
        # HistoryStore keeps the connection, its locks and every commit or
        # rollback; the moved helpers only run statements on the connection
        # they are given.
        source = Path(_store_retention().__file__).read_text(encoding="utf-8")
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        self.assertFalse(
            called
            & {"commit", "rollback", "close", "connect", "closing", "_connect", "_read_connection",
               "history_write_lock", "history_migration_lock", "acquire", "release"}
        )

    def test_history_store_keeps_every_moved_name_with_its_kind_and_signature(self) -> None:
        store_retention = _store_retention()
        moved_functions = [
            node.name
            for node in ast.parse(Path(store_retention.__file__).read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef)
        ]
        self.assertEqual(moved_functions, list(MOVED))
        for name, (kind, released) in MOVED.items():
            with self.subTest(name=name):
                kept = inspect.getattr_static(HistoryStore, name)
                self.assertIsInstance(kept, staticmethod if kind == "static" else classmethod)
                self.assertEqual(str(inspect.signature(getattr(HistoryStore, name))), released)
                moved = list(inspect.signature(getattr(store_retention, name)).parameters)
                self.assertEqual(moved[: 2 if kind == "class" else 1],
                                 ["store_module", "cls"] if kind == "class" else ["store_module"])


class ReplacementReachesMovedCodeTests(_StoreCase):
    """Each way an existing test replaces a name still reaches the moved code."""

    def test_a_replaced_store_module_datetime_reaches_the_retention_bounds(self) -> None:
        seen: list[str] = []

        class RecordingDateTime(datetime):
            @classmethod
            def fromisoformat(cls, value: str):
                seen.append(value)
                return datetime.fromisoformat(value)

        with (
            mock.patch("history_service.store.datetime", RecordingDateTime),
            # _history_utc_instant reads the same name; keep it out of the count.
            mock.patch.object(store_module, "_history_utc_instant", lambda value: "instant"),
        ):
            bounds = HistoryStore._retention_bounds("2026-03-01T00:00:00-05:00")
        self.assertEqual(seen, ["2026-03-01T00:00:00-05:00"])
        self.assertEqual(bounds, ("2026-03-01T00:00:00-05:00", "2026-02-28", "instant"))

        with mock.patch("history_service.store.timedelta", lambda days: timedelta(days=days * 3)):
            self.assertEqual(HistoryStore._retention_bounds("2026-03-01T00:00:00+00:00")[1], "2026-02-26")

    def test_a_replaced_store_module_instant_reaches_bounds_and_the_registered_sql_function(self) -> None:
        connection = self.connection()
        HistoryStore._register_retention_functions(connection)
        released = connection.execute("SELECT _history_utc_instant(CAST(? AS BLOB))", ("2026-01-01T00:00:00+01:00",))
        self.assertEqual(released.fetchone()[0], "2025-12-31T23:00:00.000000")
        # The callback was installed before the replacement and still looks
        # the store-module name up on each call.
        with mock.patch.object(store_module, "_history_utc_instant", lambda value: f"replaced:{value}"):
            replaced = connection.execute("SELECT _history_utc_instant(CAST(? AS BLOB))", ("2026-01-01",))
            self.assertEqual(replaced.fetchone()[0], "replaced:2026-01-01")
            self.assertEqual(HistoryStore._retention_bounds(CUTOFF)[2], f"replaced:{CUTOFF}")

    def test_a_replaced_store_module_floor_limit_reaches_the_floor_text(self) -> None:
        self.assertIn("<= 256 THEN", HistoryStore._deleted_floor_text("observed_at"))
        with mock.patch.object(store_module, "_RETENTION_FLOOR_TEXT_MAX_BYTES", 9):
            self.assertIn("<= 9 THEN", HistoryStore._deleted_floor_text("observed_at"))

    def test_a_replaced_class_method_reaches_the_moved_callers(self) -> None:
        marker = "coalesce(?, ?, ?) IS NOT NULL /* replaced predicate */"
        connection = _Recorder(self.connection())
        HistoryStore._register_retention_functions(connection.connection)
        with mock.patch.object(HistoryStore, "_expired_timestamp_predicate", staticmethod(lambda *a, **k: marker)):
            self.assertIn(marker, HistoryStore._expired_rows_subquery("metric_samples", "observed_at", floored=False))
            HistoryStore._retention_rows_exist(connection, "metric_samples", "observed_at", CUTOFF)
            HistoryStore._rollups_exist(connection, 3600, CUTOFF)
        self.assertEqual(sum(marker in sql for sql, _ in connection.statements), 2)

        with mock.patch.object(HistoryStore, "_retention_bounds", staticmethod(lambda cutoff: ("a", "b", "c"))):
            self.assertEqual(HistoryStore._retention_parameters(CUTOFF, ("x", 1)), ("a", "b", "c", "x", 1))

        compared: list[tuple[str, str]] = []

        def newer(new: str, old: str) -> str:
            compared.append((new, old))
            return "0"

        with mock.patch.object(HistoryStore, "_newer_last_sample", staticmethod(newer)):
            HistoryStore._roll_up_metric_rows(connection, cutoff=CUTOFF, batch_size=5, bucket_seconds=3600, floor=None)
        self.assertEqual(compared, [("excluded.last_observed_at", "metric_rollups.last_observed_at")])

        calls: list[str] = []
        advance = HistoryStore._advance_scan_floor

        def watching(connection, floors, name, deleted):
            calls.append(name)
            return advance(connection, floors, name, deleted)

        with mock.patch.object(HistoryStore, "_advance_scan_floor", staticmethod(watching)):
            floors: dict[str, tuple[bytes | str, int]] = {}
            HistoryStore._delete_expired_rows(
                connection, table_name="metric_samples", timestamp_column="observed_at",
                cutoff=CUTOFF, batch_size=5, floors=floors,
            )
            HistoryStore._delete_rollup_batch(
                connection, bucket_seconds=3600, cutoff=ROLLUP_CUTOFF, batch_size=5, floors=floors,
            )
        self.assertEqual(calls, ["metric_samples", "rollups_3600"])


class RetentionSqlTextTests(_StoreCase):
    def _exercise(self) -> list[str]:
        texts = [
            HistoryStore._newer_last_sample("a.observed_at", "b.observed_at"),
            *(
                HistoryStore._expired_timestamp_predicate(column, key, floored=floored)
                for column in ("observed_at", "bucket_start")
                for key in ("id", "rowid")
                for floored in (False, True)
            ),
            HistoryStore._expired_timestamp_predicate("observed_at", floored=True),
            *(
                HistoryStore._expired_rows_subquery(table, "observed_at", floored=floored)
                for table in ("metric_samples", "slot_events")
                for floored in (False, True)
            ),
            HistoryStore._deleted_floor_text("observed_at"),
            HistoryStore._deleted_floor_text("bucket_start"),
        ]
        connection = _Recorder(self.connection())
        HistoryStore._register_retention_functions(connection.connection)
        floors: dict[str, tuple[bytes | str, int]] = {}
        HistoryStore._retention_rows_exist(connection, "metric_samples", "observed_at", CUTOFF)
        HistoryStore._rollups_exist(connection, 3600, CUTOFF)
        HistoryStore._roll_up_metric_rows(
            connection, cutoff=CUTOFF, batch_size=20, bucket_seconds=3600, floor=None, rollup_cutoff=ROLLUP_CUTOFF
        )
        HistoryStore._roll_up_metric_rows(connection, cutoff=CUTOFF, batch_size=20, bucket_seconds=86400, floor=None)
        HistoryStore._delete_expired_rows(
            connection, table_name="metric_samples", timestamp_column="observed_at",
            cutoff=CUTOFF, batch_size=20, floors=floors,
        )
        HistoryStore._roll_up_metric_rows(
            connection, cutoff=CUTOFF, batch_size=20, bucket_seconds=3600, floor=floors["metric_samples"]
        )
        HistoryStore._delete_rollup_batch(
            connection, bucket_seconds=3600, cutoff=ROLLUP_CUTOFF, batch_size=5, floors=floors
        )
        HistoryStore._delete_rollup_batch(
            connection, bucket_seconds=3600, cutoff=ROLLUP_CUTOFF, batch_size=5, floors=floors
        )
        HistoryStore._retention_rows_exist(
            connection, "metric_samples", "observed_at", CUTOFF, floor=floors["metric_samples"]
        )
        HistoryStore._rollups_exist(connection, 3600, ROLLUP_CUTOFF, floor=floors["rollups_3600"])
        return texts + [sql for sql, _ in connection.statements]

    def test_retention_sql_text_is_byte_for_byte_the_released_text(self) -> None:
        texts = self._exercise()
        self.assertEqual(len(texts), 29)
        digest = hashlib.sha256("\0".join(texts).encode("utf-8")).hexdigest()
        self.assertEqual(digest, CURRENT_SQL_SHA256)
        # The only change since the move: rollup buckets are named from
        # julianday(), three uses in each of the three roll-up statements
        # (#1320). Undoing it gives the released text back exactly.
        self.assertEqual(sum(text.count(BUCKET_READING[1]) for text in texts), 9)
        released = [text.replace(BUCKET_READING[1], BUCKET_READING[0]) for text in texts]
        released_digest = hashlib.sha256("\0".join(released).encode("utf-8")).hexdigest()
        self.assertEqual(released_digest, RELEASED_SQL_SHA256)

    def test_an_unsupported_rollup_interval_is_refused_with_the_released_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HistoryStore._roll_up_metric_rows(
                self.connection(), cutoff=CUTOFF, batch_size=20, bucket_seconds=7200, floor=None
            )
        self.assertEqual(
            (type(caught.exception), str(caught.exception)),
            (ValueError, "Unsupported history rollup interval."),
        )


if __name__ == "__main__":
    unittest.main()
