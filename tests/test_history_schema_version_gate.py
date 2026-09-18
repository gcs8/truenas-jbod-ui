"""The on-disk history schema version is a compatibility contract (#416).

Startup used to run CREATE/ALTER/backfill against whatever SQLite file it was
pointed at: a database stamped `PRAGMA user_version = 999` by some future
release was accepted and gained this build's tables. These tests pin the gate
that refuses a newer-than-supported database before the first write, and that
still admits (and migrates) every version this build declares support for.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
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


def _stamp_wal_database(path: Path, user_version: int) -> sqlite3.Connection:
    """Commit `user_version` into a WAL that is deliberately never checkpointed.

    `wal_autocheckpoint = 0` keeps the frames in the `-wal` sidecar, and the
    returned connection must stay open: closing the last connection checkpoints
    and deletes the WAL, which is exactly the state this helper must avoid.
    """
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL").fetchall()
    connection.execute("PRAGMA wal_autocheckpoint = 0").fetchall()
    connection.execute("CREATE TABLE IF NOT EXISTS legacy_marker (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO legacy_marker (id) VALUES (1)")
    connection.commit()
    connection.execute(f"PRAGMA user_version = {int(user_version)}")
    connection.commit()
    return connection


def _digest(path: Path) -> tuple[int, str] | None:
    if not path.exists():
        return None
    return (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


class HistorySchemaVersionWalGateTests(unittest.TestCase):
    """A future version committed to the WAL but not yet checkpointed (#546).

    `PRAGMA user_version` lives on page 1. In WAL mode a committed change to
    page 1 sits in the `-wal` sidecar until a checkpoint copies it back, so the
    main file's 100-byte header still carries the *old* value. A gate that reads
    only that header therefore admits a database a newer release has already
    stamped, and #416 requires the refusal to happen before any write.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name)
        self.database_path = self.directory / "history.sqlite3"
        self.future_version = CURRENT_SCHEMA_VERSION + 998

    def _crashed_wal_database(self, user_version: int) -> Path:
        """A database plus orphaned `-wal`, as a killed writer leaves them."""
        staging = Path(self._temporary.name) / "staging"
        staging.mkdir()
        source = staging / "history.sqlite3"
        connection = _stamp_wal_database(source, user_version)
        try:
            self.assertTrue(
                Path(f"{source}-wal").exists(),
                "the fixture needs an unreplayed -wal to be meaningful",
            )
            for suffix in ("", "-wal"):
                shutil.copyfile(f"{source}{suffix}", f"{self.database_path}{suffix}")
        finally:
            connection.close()
        return self.database_path

    def _open(self) -> HistoryStore:
        return HistoryStore(str(self.database_path), recover_unreadable_database=False)

    def test_the_main_header_still_shows_the_old_version(self) -> None:
        # Pins the premise: the header alone cannot answer this question.
        self._crashed_wal_database(self.future_version)
        header = self.database_path.read_bytes()[60:64]
        self.assertNotEqual(int.from_bytes(header, "big"), self.future_version)

    def test_future_version_committed_only_in_the_wal_is_reported(self) -> None:
        self._crashed_wal_database(self.future_version)

        self.assertEqual(
            HistoryStore._read_on_disk_schema_version(self.database_path),
            self.future_version,
        )

    def test_future_version_committed_only_in_the_wal_refuses_to_start(self) -> None:
        self._crashed_wal_database(self.future_version)

        with self.assertRaises(HistorySchemaVersionError) as raised:
            self._open()

        self.assertIn(str(self.future_version), str(raised.exception))

    def test_refusing_a_wal_database_leaves_the_database_and_wal_untouched(self) -> None:
        self._crashed_wal_database(self.future_version)
        wal_path = Path(f"{self.database_path}-wal")
        before_database = _digest(self.database_path)
        before_wal = _digest(wal_path)
        before_mtimes = (
            self.database_path.stat().st_mtime_ns,
            wal_path.stat().st_mtime_ns,
        )

        with self.assertRaises(HistorySchemaVersionError):
            self._open()

        self.assertEqual(_digest(self.database_path), before_database)
        self.assertEqual(_digest(wal_path), before_wal)
        self.assertEqual(
            (self.database_path.stat().st_mtime_ns, wal_path.stat().st_mtime_ns),
            before_mtimes,
        )
        # No application table was grafted onto the foreign database, and the
        # refusal did not replay the WAL into it either.
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

    def test_future_version_in_a_live_uncheckpointed_wal_refuses_to_start(self) -> None:
        # The same state with the writer still attached, which is what a
        # still-running newer release looks like from a second container.
        connection = _stamp_wal_database(self.database_path, self.future_version)
        self.addCleanup(connection.close)

        with self.assertRaises(HistorySchemaVersionError):
            self._open()

    def test_a_supported_version_in_the_wal_is_still_admitted(self) -> None:
        # The gate must read the WAL, not simply refuse whenever one exists.
        self._crashed_wal_database(CURRENT_SCHEMA_VERSION)

        self.assertEqual(
            HistoryStore._read_on_disk_schema_version(self.database_path),
            CURRENT_SCHEMA_VERSION,
        )

    def test_an_unreadable_wal_database_is_not_claimed_as_a_version_problem(self) -> None:
        # Foreign bytes with a foreign sidecar stay with the corrupt/recovery
        # path rather than being reported as an unsupported schema version.
        self.database_path.write_bytes(b"not a database at all")
        Path(f"{self.database_path}-wal").write_bytes(b"not a write-ahead log either")

        self.assertIsNone(HistoryStore._read_on_disk_schema_version(self.database_path))


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
        self.assertFalse(Path(f"{self.database_path}-shm").exists())
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

    def test_a_file_that_is_not_a_sqlite_database_reports_no_version(self) -> None:
        # Corrupt or foreign bytes are not a version problem; they belong to the
        # existing quarantine/recovery path, so the gate must not claim them.
        self.database_path.write_bytes(b"not a database at all")

        self.assertIsNone(HistoryStore._read_on_disk_schema_version(self.database_path))

    def test_a_missing_or_empty_file_reports_no_version(self) -> None:
        self.assertIsNone(HistoryStore._read_on_disk_schema_version(self.database_path))
        self.database_path.write_bytes(b"")
        self.assertIsNone(HistoryStore._read_on_disk_schema_version(self.database_path))

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
