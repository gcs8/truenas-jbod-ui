"""An import installs exactly the history member it validated (#1422).

Validation reads the member as one file, and activation installs that same
file: SQLite side files next to the staged member refuse the import, and the
member checks run again on the private copy that is published. Every fixture
is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import shutil
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from history_service.store import SCHEMA
from history_service.system_backup import SystemBackupService, _ImportActivationTransaction
from tests.test_interrupted_restore_recovery import PASSPHRASE
# Module import, not class import: importing the test classes would run
# them a second time under this module.
from tests import test_restore_refusal_cleanup as refusal
from tests.test_restore_refusal_cleanup import (
    _dump,
    _history_database,
    _ImportRootCleanup,
    _metric_rows,
)

ORDINARY = refusal.RestoreTimestampSizeTests.ORDINARY
SIDE_FILE_REASON = "next to the staged history member"


def _rewrite_in_place(target: Path, change: Callable[[sqlite3.Connection], None]) -> None:
    """Apply ``change`` to a copy of ``target`` and write the result back over
    ``target`` as a single file (no side files are left beside it)."""
    work = target.parent.parent / "rewrite-work"
    work.mkdir()
    copy = work / "copy.sqlite3"
    shutil.copyfile(target, copy)
    with closing(sqlite3.connect(copy)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        change(connection)
        connection.commit()
    content = copy.read_bytes()
    with open(target, "r+b") as handle:
        handle.write(content)
        handle.truncate()
    shutil.rmtree(work)


def _drop_slot_events(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE slot_events")


def _null_metric_name(connection: sqlite3.Connection) -> None:
    """Leave a NULL in a NOT NULL column, which only an integrity check finds."""
    relax = "UPDATE sqlite_master SET sql = replace(sql, ?, ?) WHERE name = 'metric_samples'"
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(relax, ("metric_name TEXT NOT NULL", "metric_name TEXT"))
    connection.commit()
    connection.execute("PRAGMA writable_schema=RESET")
    connection.execute("UPDATE metric_samples SET metric_name = NULL WHERE slot = 1")
    connection.commit()
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(relax, ("metric_name TEXT", "metric_name TEXT NOT NULL"))
    connection.commit()
    connection.execute("PRAGMA writable_schema=RESET")


class ImportInstallsTheValidatedMemberTests(_ImportRootCleanup):
    """Content beside or in the staged member after validation is never installed."""

    _service = refusal.RollbackSnapshotTimestampBoundTests._service
    _artifact = refusal.RollbackSnapshotTimestampBoundTests._artifact
    ORDINARY = ORDINARY

    IMPORTED_ROWS = [(ORDINARY, 0, 30), (ORDINARY, 1, 31)]

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)
        self.artifact = self._artifact()
        live_dir = self.root / "live-history"
        live_dir.mkdir()
        self.live = _history_database(live_dir / "history.sqlite3", [ORDINARY])
        self.target = self._service(self.root / "target", self.live)
        self.original_rows = _metric_rows(self.live)
        self.assertEqual(self.original_rows, [(ORDINARY, 0, 30)])
        self.original_dump = _dump(self.live)
        self.staged: list[Path] = []

    def _import(self, tamper: Callable[[Path], None] | None) -> None:
        """Run the real encrypted import; ``tamper`` sees the staged member
        after validation and just before activation installs it."""
        real_activate = _ImportActivationTransaction.activate_history

        def tamper_then_activate(transaction, store, member_key):
            staged = transaction._staged_member(member_key)
            self.assertTrue(staged.is_file())
            self.staged.append(staged)
            if tamper is not None:
                tamper(staged)
            real_activate(transaction, store, member_key)

        with patch.object(
            _ImportActivationTransaction, "activate_history", tamper_then_activate
        ):
            self.target.import_bundle_from_file(self.artifact.path, passphrase=PASSPHRASE)

    def _assert_refused_unchanged(self, refused: Any, reason: str) -> None:
        self.assertEqual(len(self.staged), 1, "activation was never reached")
        self.assertIn(reason, str(refused.exception))
        self.assertEqual(_dump(self.live), self.original_dump)
        self.assertEqual(_metric_rows(self.live), self.original_rows)

    def _plant_write_ahead_log(self, staged: Path) -> None:
        """Leave a committed write-ahead log beside the staged member."""
        work = self.root / "wal-work"
        work.mkdir()
        copy = work / "copy.sqlite3"
        shutil.copyfile(staged, copy)
        connection = sqlite3.connect(copy)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE metric_samples SET value_integer = 999")
            connection.commit()
            shutil.copyfile(f"{copy}-wal", f"{staged}-wal")
        finally:
            connection.close()

    def test_untampered_import_installs_the_validated_rows(self) -> None:
        self._import(None)
        self.assertEqual(len(self.staged), 1)
        self.assertEqual(_metric_rows(self.live), self.IMPORTED_ROWS)

    def test_write_ahead_log_beside_the_staged_member_is_refused(self) -> None:
        with self.assertRaises(ValueError) as refused:
            self._import(self._plant_write_ahead_log)
        self._assert_refused_unchanged(refused, SIDE_FILE_REASON)
        self.assertNotIn(999, [row[2] for row in _metric_rows(self.live)])

    def test_write_ahead_log_appearing_after_the_check_is_not_read(self) -> None:
        # The log lands just before the restore opens the staged member, after
        # any check for side files: the validated rows are installed.
        real_connect = sqlite3.connect
        armed = [True]

        def plant_on_open(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            if armed and self.staged and str(self.staged[0]) in str(target):
                armed.clear()
                self._plant_write_ahead_log(self.staged[0])
            return real_connect(target, *args, **kwargs)

        with patch("sqlite3.connect", plant_on_open):
            self._import(None)
        self.assertEqual(armed, [], "the staged member was never opened")
        self.assertEqual(_metric_rows(self.live), self.IMPORTED_ROWS)

    def test_any_side_file_beside_the_staged_member_is_refused(self) -> None:
        for suffix in ("-journal", "-shm"):
            with self.subTest(suffix=suffix):
                self.staged.clear()

                def plant(staged: Path, suffix: str = suffix) -> None:
                    Path(f"{staged}{suffix}").write_bytes(b"\0" * 512)

                with self.assertRaises(ValueError) as refused:
                    self._import(plant)
                self._assert_refused_unchanged(refused, SIDE_FILE_REASON)

    def test_staged_member_missing_a_required_table_is_refused(self) -> None:
        def drop(staged: Path) -> None:
            _rewrite_in_place(staged, _drop_slot_events)

        with self.assertRaises(ValueError) as refused:
            self._import(drop)
        self._assert_refused_unchanged(refused, "missing table slot_events")

    def test_staged_member_failing_the_integrity_check_is_refused(self) -> None:
        def corrupt(staged: Path) -> None:
            _rewrite_in_place(staged, _null_metric_name)
            with closing(sqlite3.connect(f"{staged.as_uri()}?mode=ro&immutable=1", uri=True)) as c:
                self.assertNotEqual(c.execute("PRAGMA quick_check").fetchall(), [("ok",)])

        with self.assertRaises(ValueError) as refused:
            self._import(corrupt)
        self._assert_refused_unchanged(refused, "integrity check failed")


class MemberValidationReadsOneFileTests(_ImportRootCleanup):
    """Validation judges the member file that activation copies, not the
    member combined with side files that happen to sit next to it."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)

    def test_write_ahead_log_beside_the_member_is_not_part_of_it(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY])
        with closing(sqlite3.connect(member)) as connection:
            connection.execute("DROP TABLE slot_events")
            connection.commit()
        with self.assertRaises(ValueError):
            SystemBackupService._validate_history_member(member)
        # A write-ahead log beside it that would add the missing table back.
        copy = self.root / "copy.sqlite3"
        shutil.copyfile(member, copy)
        connection = sqlite3.connect(copy)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.executescript(SCHEMA)
            connection.commit()
            shutil.copyfile(f"{copy}-wal", f"{member}-wal")
        finally:
            connection.close()
        with self.assertRaises(ValueError) as refused:
            SystemBackupService._validate_history_member(member)
        self.assertIn("missing table slot_events", str(refused.exception))

    def test_complete_member_still_validates(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        SystemBackupService._validate_history_member(member)


if __name__ == "__main__":
    unittest.main()
