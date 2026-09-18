"""The on-disk history schema version is a compatibility contract (#416).

Startup used to run CREATE/ALTER/backfill against whatever SQLite file it was
pointed at: a database stamped `PRAGMA user_version = 999` by some future
release was accepted and gained this build's tables. These tests pin the gate
that refuses a newer-than-supported database before the first write, and that
still admits (and migrates) every version this build declares support for.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from history_service import startup as startup_module
from history_service.startup import (
    HistorySchemaVersionError,
    HistoryStartupError,
    open_history_store_with_retries,
)
from history_service.store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    HistoryStore,
)

# Opening a store for real takes the POSIX migration lock (fcntl); the refusal
# path deliberately runs before that lock, so only the admitted cases skip here.
requires_posix_migration_lock = unittest.skipUnless(
    importlib.util.find_spec("fcntl") is not None,
    "History migration locking requires POSIX flock support.",
)


def _stamp_database(path: Path, user_version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS legacy_marker (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO legacy_marker (id) VALUES (1)")
        connection.execute(f"PRAGMA user_version = {int(user_version)}")
        connection.commit()
    finally:
        connection.close()


class HistorySchemaVersionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.database_path = self.directory / "history.sqlite3"

    def _open(self) -> HistoryStore:
        return HistoryStore(str(self.database_path), recover_unreadable_database=False)

    def test_supported_range_is_declared_and_ordered(self) -> None:
        self.assertLessEqual(MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
        self.assertGreaterEqual(MIN_SUPPORTED_SCHEMA_VERSION, 0)

    def test_newer_than_supported_database_refuses_to_start(self) -> None:
        future_version = CURRENT_SCHEMA_VERSION + 998
        _stamp_database(self.database_path, future_version)

        with self.assertRaises(HistorySchemaVersionError) as raised:
            self._open()

        reason = str(raised.exception)
        self.assertIn(str(future_version), reason)
        self.assertIn(str(CURRENT_SCHEMA_VERSION), reason)
        # Plain words, not a traceback or a bare code.
        self.assertIn("newer", reason.lower())
        self.assertNotIn("Traceback", reason)

    def test_newer_than_supported_database_is_left_byte_identical(self) -> None:
        _stamp_database(self.database_path, CURRENT_SCHEMA_VERSION + 998)
        before = self.database_path.read_bytes()

        with self.assertRaises(HistorySchemaVersionError):
            self._open()

        self.assertEqual(self.database_path.read_bytes(), before)
        self.assertFalse(Path(f"{self.database_path}-wal").exists())
        # No application table was created against the foreign database.
        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(names, {"legacy_marker"})

    @requires_posix_migration_lock
    def test_equal_to_current_version_opens_without_remigrating(self) -> None:
        store = self._open()
        self.assertEqual(self._user_version(), CURRENT_SCHEMA_VERSION)
        del store

        reopened = HistoryStore(str(self.database_path), recover_unreadable_database=False)
        self.assertEqual(self._user_version(), CURRENT_SCHEMA_VERSION)
        self.assertIsNotNone(reopened)

    @requires_posix_migration_lock
    def test_older_supported_database_still_migrates_through_the_existing_path(self) -> None:
        # A database from before the disk-identity backfill: version 0, no tables.
        _stamp_database(self.database_path, MIN_SUPPORTED_SCHEMA_VERSION)

        self._open()

        self.assertEqual(self._user_version(), CURRENT_SCHEMA_VERSION)
        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            # The predecessor's own rows survive the migration.
            surviving = connection.execute("SELECT COUNT(*) FROM legacy_marker").fetchone()[0]
        finally:
            connection.close()
        self.assertIn("slot_state_current", names)
        self.assertIn("legacy_marker", names)
        self.assertEqual(surviving, 1)

    def test_startup_reports_a_schema_refusal_as_a_terminal_reason(self) -> None:
        _stamp_database(self.database_path, CURRENT_SCHEMA_VERSION + 998)

        with self.assertRaises(HistoryStartupError) as raised:
            open_history_store_with_retries(
                self._open,
                directory=self.directory,
                attempts=3,
                initial_backoff_seconds=0.0,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(startup_module.recorded_startup_failure(), raised.exception.reason)
        self.addCleanup(startup_module._record, None)

    def _user_version(self) -> int:
        connection = sqlite3.connect(self.database_path)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
