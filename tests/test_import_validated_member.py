"""An import installs exactly the history member it validated (#1422).

Validation reads the member as one file and records its size and SHA-256.
Activation copies the staged member through one opened descriptor into a
private file, refuses it unless that copy has the validated size and digest,
checks the copy and publishes only that copy. SQLite reads that copy as one
file: a side file beside it at SQLite's first read refuses the import, one
appearing later is ignored. A segmented import holds its staged hot database
and segments to the validated digests the same way. SQLite side files next to
the staged member still refuse the import. Every fixture is synthetic and lives
in a temporary directory.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import history_service.store as store_module
import history_service.system_backup as system_backup_module
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import (
    HISTORY_DB_KEY,
    SEGMENTED_CATALOG_STAGING_KEY,
    SystemBackupService,
    _ImportActivationTransaction,
)
from tests.test_interrupted_restore_recovery import (
    PASSPHRASE,
    _create_history,
    _deployment,
    _hot_events,
    _tree_snapshot,
)
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
CONTENT_REASON = "is not the member that was validated"


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
        real_open = os.open
        armed = [True]

        def plant_once(target: Any) -> None:
            if armed and self.staged and str(self.staged[0]) in str(target):
                armed.clear()
                self._plant_write_ahead_log(self.staged[0])

        def plant_on_connect(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            plant_once(target)
            return real_connect(target, *args, **kwargs)

        def plant_on_open(target: Any, *args: Any, **kwargs: Any) -> int:
            plant_once(target)
            return real_open(target, *args, **kwargs)

        with patch("sqlite3.connect", plant_on_connect), patch("os.open", plant_on_open):
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

    # A staged member rewritten after validation differs from what was
    # validated, so the content check refuses it before any SQLite check;
    # MemberChecksOnThePrivateCopyTests shows those checks still run.
    def test_staged_member_missing_a_required_table_is_refused(self) -> None:
        def drop(staged: Path) -> None:
            _rewrite_in_place(staged, _drop_slot_events)

        with self.assertRaises(ValueError) as refused:
            self._import(drop)
        self._assert_refused_unchanged(refused, CONTENT_REASON)

    def test_staged_member_failing_the_integrity_check_is_refused(self) -> None:
        def corrupt(staged: Path) -> None:
            _rewrite_in_place(staged, _null_metric_name)
            with closing(sqlite3.connect(f"{staged.as_uri()}?mode=ro&immutable=1", uri=True)) as c:
                self.assertNotEqual(c.execute("PRAGMA quick_check").fetchall(), [("ok",)])

        with self.assertRaises(ValueError) as refused:
            self._import(corrupt)
        self._assert_refused_unchanged(refused, CONTENT_REASON)

    def _valid_replacement(self, like: Path | None = None) -> Path:
        """A well-formed history database whose rows differ from the import's.

        With ``like``, it is a copy of that file with other values, so it has
        the same size and only its digest tells it apart.
        """
        replacement = self.root / "replacement.sqlite3"
        replacement.unlink(missing_ok=True)
        if like is None:
            _history_database(replacement, [ORDINARY, ORDINARY, ORDINARY])
        else:
            shutil.copyfile(like, replacement)
        with closing(sqlite3.connect(replacement)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("UPDATE metric_samples SET value_integer = 777")
            connection.commit()
        if like is not None:
            self.assertEqual(replacement.stat().st_size, like.stat().st_size)
        SystemBackupService._validate_history_member(replacement)
        return replacement

    def _assert_replacement_not_installed(self, refused: Any) -> None:
        self._assert_refused_unchanged(refused, CONTENT_REASON)
        self.assertNotIn(777, [row[2] for row in _metric_rows(self.live)])

    def test_valid_database_moved_onto_the_staged_path_is_refused(self) -> None:
        def swap(staged: Path) -> None:
            os.replace(self._valid_replacement(like=staged), staged)

        with self.assertRaises(ValueError) as refused:
            self._import(swap)
        self._assert_replacement_not_installed(refused)

    def test_valid_database_rewritten_into_the_staged_file_is_refused(self) -> None:
        def rewrite(staged: Path) -> None:
            inode = staged.stat().st_ino
            content = self._valid_replacement(like=staged).read_bytes()
            with open(staged, "r+b") as handle:
                handle.write(content)
                handle.truncate()
            self.assertEqual(staged.stat().st_ino, inode)

        with self.assertRaises(ValueError) as refused:
            self._import(rewrite)
        self._assert_replacement_not_installed(refused)

    def test_member_changed_between_validation_and_staging_is_refused(self) -> None:
        real_init = _ImportActivationTransaction.__init__
        changed: list[Path] = []

        def change_then_stage(transaction, extracted_members, **kwargs):
            member = next(
                content
                for content in extracted_members.values()
                if isinstance(content, Path) and content.read_bytes()[:16] == b"SQLite format 3\0"
            )
            os.replace(self._valid_replacement(like=member), member)
            changed.append(member)
            real_init(transaction, extracted_members, **kwargs)

        with (
            patch.object(_ImportActivationTransaction, "__init__", change_then_stage),
            self.assertRaises(ValueError) as refused,
        ):
            self.target.import_bundle_from_file(self.artifact.path, passphrase=PASSPHRASE)
        self.assertEqual(len(changed), 1, "the extracted member was never changed")
        self.assertIn(CONTENT_REASON, str(refused.exception))
        self.assertEqual(_dump(self.live), self.original_dump)

    def test_content_is_judged_through_the_descriptor_that_is_copied(self) -> None:
        # The staged path names different files before and after the restore
        # opens it; the refusal must follow the opened file, whichever the
        # path names when the digest is taken.
        real_open = os.open
        for case in ("changed-just-before-open", "restored-just-after-open"):
            with self.subTest(case=case):
                self.staged.clear()
                state: dict[str, Any] = {"armed": True}

                def prepare(staged: Path) -> None:
                    state["validated"] = staged.read_bytes()
                    state["replacement"] = self._valid_replacement(like=staged).read_bytes()
                    if case == "restored-just-after-open":
                        staged.write_bytes(state["replacement"])

                def swap_around_open(target: Any, *args: Any, **kwargs: Any) -> int:
                    hit = state["armed"] and self.staged and str(target) == str(self.staged[0])
                    if hit and case == "changed-just-before-open":
                        state["armed"] = False
                        replacement = self.root / "before-open.sqlite3"
                        replacement.write_bytes(state["replacement"])
                        os.replace(replacement, self.staged[0])
                    descriptor = real_open(target, *args, **kwargs)
                    if hit and case == "restored-just-after-open":
                        state["armed"] = False
                        validated = self.root / "after-open.sqlite3"
                        validated.write_bytes(state["validated"])
                        os.replace(validated, self.staged[0])
                    return descriptor

                with patch("os.open", swap_around_open), self.assertRaises(ValueError) as refused:
                    self._import(prepare)
                self.assertFalse(state["armed"], "the staged member was never opened")
                self._assert_replacement_not_installed(refused)

    def test_timestamp_admission_runs_once_on_the_installed_copy(self) -> None:
        calls: list[str] = []
        activating = [False]
        real_bound = store_module.require_bounded_restored_timestamps
        real_activate = _ImportActivationTransaction.activate_history

        def count(where: str) -> Callable[[sqlite3.Connection], None]:
            def bound(connection: sqlite3.Connection) -> None:
                if activating[0]:
                    calls.append(where)
                real_bound(connection)

            return bound

        def activate(transaction, store, member_key):
            activating[0] = True
            try:
                real_activate(transaction, store, member_key)
            finally:
                activating[0] = False

        with (
            patch.object(store_module, "require_bounded_restored_timestamps", count("store")),
            patch.object(
                system_backup_module, "require_bounded_restored_timestamps", count("member check")
            ),
            patch.object(_ImportActivationTransaction, "activate_history", activate),
        ):
            self.target.import_bundle_from_file(self.artifact.path, passphrase=PASSPHRASE)
        self.assertEqual(calls, ["store"])
        self.assertEqual(_metric_rows(self.live), self.IMPORTED_ROWS)


class MemberChecksOnThePrivateCopyTests(_ImportRootCleanup):
    """The member checks still run on the private copy that is published,
    including for content that matches the recorded size and digest."""

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)
        live_dir = self.root / "live-history"
        live_dir.mkdir()
        self.live = _history_database(live_dir / "history.sqlite3", [ORDINARY])
        self.store = HistoryStore(str(self.live), recover_unreadable_database=False)
        self.original_dump = _dump(self.live)

    def _restore(self, member: Path) -> None:
        content = member.read_bytes()
        self.store.restore_validated_member(
            member,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            member_check=SystemBackupService._check_history_structure,
        )

    def test_member_missing_a_required_table_is_refused(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        with closing(sqlite3.connect(member)) as connection:
            _drop_slot_events(connection)
            connection.commit()
        with self.assertRaises(ValueError) as refused:
            self._restore(member)
        self.assertIn("missing table slot_events", str(refused.exception))
        self.assertEqual(_dump(self.live), self.original_dump)

    def test_member_failing_the_integrity_check_is_refused(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        with closing(sqlite3.connect(member)) as connection:
            _null_metric_name(connection)
        with self.assertRaises(ValueError) as refused:
            self._restore(member)
        self.assertIn("integrity check failed", str(refused.exception))
        self.assertEqual(_dump(self.live), self.original_dump)

    def test_matching_member_is_installed(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        self._restore(member)
        self.assertEqual(_metric_rows(self.live), [(ORDINARY, 0, 30), (ORDINARY, 1, 31)])

    def test_member_whose_header_records_write_ahead_logging_is_installed(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        with closing(sqlite3.connect(member)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        for suffix in ("-wal", "-shm"):
            Path(f"{member}{suffix}").unlink(missing_ok=True)
        self.assertEqual(member.read_bytes()[18:20], b"\x02\x02")
        self._restore(member)
        self.assertEqual(_metric_rows(self.live), [(ORDINARY, 0, 30), (ORDINARY, 1, 31)])


def _committed_write_ahead_log(database: Path, work: Path, value: int) -> bytes:
    """A committed write-ahead log for a copy of ``database`` that sets every
    sample to ``value``."""
    work.mkdir()
    copy = work / "copy.sqlite3"
    shutil.copyfile(database, copy)
    connection = sqlite3.connect(copy)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE metric_samples SET value_integer = ?", (value,))
        connection.commit()
        content = Path(f"{copy}-wal").read_bytes()
    finally:
        connection.close()
    shutil.rmtree(work)
    return content


def _hot_rollback_journal(database: Path, work: Path, value: int) -> bytes:
    """A hot rollback journal for a copy of ``database`` whose saved pages set
    every sample to ``value``: SQLite replays it at its first read."""
    work.mkdir()
    copy = work / "copy.sqlite3"
    shutil.copyfile(database, copy)
    connection = sqlite3.connect(copy, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("UPDATE metric_samples SET value_integer = ?", (value,))
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE metric_samples SET value_integer = 1")
        content = Path(f"{copy}-journal").read_bytes()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    shutil.rmtree(work)
    return content


class PrivateCopySideFilesTests(_ImportRootCleanup):
    """SQLite reads no side file beside the private copy it checks and publishes.

    A side file beside the private copy when SQLite first reads it refuses the
    import; one that appears after that first read is ignored, and the
    validated rows are installed.
    """

    _service = refusal.RollbackSnapshotTimestampBoundTests._service
    _artifact = refusal.RollbackSnapshotTimestampBoundTests._artifact
    ORDINARY = ORDINARY
    IMPORTED_ROWS = ImportInstallsTheValidatedMemberTests.IMPORTED_ROWS
    PLANTED = {"-wal": 999, "-journal": 777}

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)
        self.artifact = self._artifact()
        live_dir = self.root / "live-history"
        live_dir.mkdir()
        self.live = _history_database(live_dir / "history.sqlite3", [ORDINARY])
        self.target = self._service(self.root / "target", self.live)
        self.original_dump = _dump(self.live)
        self.private: list[Path] = []
        self.planted: list[str] = []

    def _plant(self, private: Path, suffix: str) -> None:
        work = self.root / f"plant-work-{len(self.planted)}"
        if suffix == "-wal":
            content = _committed_write_ahead_log(private, work, self.PLANTED[suffix])
        elif suffix == "-journal":
            content = _hot_rollback_journal(private, work, self.PLANTED[suffix])
        else:
            content = b"\0" * 32768
        Path(f"{private}{suffix}").write_bytes(content)
        self.planted.append(suffix)

    def _import(self, *, after_copy: str | None = None, after_open: str | None = None) -> None:
        """Run the real encrypted import. ``after_copy`` plants a side file
        beside the private copy once the validated bytes are in it, before
        SQLite opens it; ``after_open`` plants one after SQLite has read it,
        just before the schema version gate."""
        real_copy = HistoryStore._copy_validated_member
        real_gate = store_module.require_restorable_schema_version

        def copy_then_plant(source: Path, target_descriptor: int, *args: Any) -> None:
            real_copy(source, target_descriptor, *args)
            self.private.append(Path(os.readlink(f"/proc/self/fd/{target_descriptor}")))
            if after_copy is not None:
                self._plant(self.private[0], after_copy)

        def plant_then_gate(version: int) -> None:
            if after_open is not None and self.private and not self.planted:
                self._plant(self.private[0], after_open)
            real_gate(version)

        with (
            patch.object(HistoryStore, "_copy_validated_member", staticmethod(copy_then_plant)),
            patch.object(store_module, "require_restorable_schema_version", plant_then_gate),
        ):
            self.target.import_bundle_from_file(self.artifact.path, passphrase=PASSPHRASE)

    def _assert_refused_unchanged(self, refused: Any, suffix: str) -> None:
        self.assertEqual(self.planted, [suffix], "the side file was never planted")
        self.assertIn("private copy of the staged history member", str(refused.exception))
        self.assertIn("current history was not changed", str(refused.exception))
        self.assertEqual(_dump(self.live), self.original_dump)

    def test_clean_import_installs_the_validated_rows(self) -> None:
        self._import()
        self.assertEqual(len(self.private), 1, "the private copy was never made")
        self.assertEqual(_metric_rows(self.live), self.IMPORTED_ROWS)

    def test_side_file_beside_the_private_copy_before_it_is_opened_is_refused(self) -> None:
        for suffix in ("-wal", "-journal", "-shm"):
            with self.subTest(suffix=suffix):
                self.private.clear()
                self.planted.clear()
                with self.assertRaises(ValueError) as refused:
                    self._import(after_copy=suffix)
                self._assert_refused_unchanged(refused, suffix)

    def test_write_ahead_log_after_the_side_file_check_is_refused(self) -> None:
        # The log appears once the side-file check has passed, before SQLite's
        # first read: SQLite then reports write-ahead logging, which refuses.
        real_check = getattr(HistoryStore, "_refuse_private_copy_side_files", None)

        def check_then_plant(private: Path) -> None:
            if real_check is not None:
                real_check(private)
            self._plant(private, "-wal")

        with (
            patch.object(
                HistoryStore,
                "_refuse_private_copy_side_files",
                staticmethod(check_then_plant),
                create=True,
            ),
            self.assertRaises(ValueError) as refused,
        ):
            self._import()
        self._assert_refused_unchanged(refused, "-wal")

    def _assert_ignored(self, suffix: str) -> None:
        self._import(after_open=suffix)
        self.assertEqual(self.planted, [suffix], "the side file was never planted")
        self.assertEqual(_metric_rows(self.live), self.IMPORTED_ROWS)

    def test_write_ahead_log_appearing_after_the_first_read_is_ignored(self) -> None:
        self._assert_ignored("-wal")

    def test_rollback_journal_appearing_after_the_first_read_is_ignored(self) -> None:
        self._assert_ignored("-journal")


class SegmentedImportInstallsTheValidatedMembersTests(_ImportRootCleanup):
    """A segmented import installs the hot database and segments it validated."""

    artifact: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._class_directory = tempfile.TemporaryDirectory()
        class_root = Path(cls._class_directory.name)
        source = _deployment(class_root / "source", "candidate")
        cls.artifact = source.service.export_bundle_to_file(
            packaging="tar.zst",
            encrypt=True,
            passphrase=PASSPHRASE,
            included_paths=[HISTORY_DB_KEY],
        )
        # Well-formed history files with other rows, for the replacements.
        cls.other_hot, cls.other_segments = _create_history(class_root / "other", "other")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.artifact is not None:
            cls.artifact.cleanup()
        cls._class_directory.cleanup()

    def setUp(self) -> None:
        super().setUp()
        self.target = _deployment(Path(self._directory.name) / "target", "prior")
        self.prior_events = _hot_events(self.target.hot)
        self.prior_hot = self.target.hot.read_bytes()
        self.prior_segments = _tree_snapshot(self.target.segments)
        self.tampered: list[str] = []

    def _import(self, tamper: Callable[[_ImportActivationTransaction, str, list], None] | None):
        real_prepare = _ImportActivationTransaction.prepare_segmented_history

        def tamper_then_prepare(transaction, store, *, hot_member_key, segment_members, **kwargs):
            if tamper is not None:
                tamper(transaction, hot_member_key, segment_members)
            return real_prepare(
                transaction,
                store,
                hot_member_key=hot_member_key,
                segment_members=segment_members,
                **kwargs,
            )

        with patch.object(
            _ImportActivationTransaction, "prepare_segmented_history", tamper_then_prepare
        ):
            self.target.service.import_bundle_from_file(self.artifact.path, passphrase=PASSPHRASE)

    def _replace_hot(self, transaction, hot_member_key, _segment_members) -> None:
        staged = transaction._staged_member(hot_member_key)
        replacement = staged.with_name("replacement")
        shutil.copyfile(self.other_hot, replacement)
        os.replace(replacement, staged)
        self.tampered.append("hot")

    def _rewrite_segment(self, transaction, _hot_member_key, segment_members) -> None:
        member_key = next(
            key for key, _path in segment_members if key != SEGMENTED_CATALOG_STAGING_KEY
        )
        staged = transaction._staged_member(member_key)
        other_segment = next(
            path for path in sorted(self.other_segments.rglob("*.sqlite3"))
        )
        SystemBackupService._validate_history_member(other_segment)
        with open(staged, "r+b") as handle:
            handle.write(other_segment.read_bytes())
            handle.truncate()
        self.tampered.append("segment")

    def _assert_refused_unchanged(self, refused: Any, part: str) -> None:
        self.assertEqual(self.tampered, [part], "the staged member was never changed")
        self.assertIn(CONTENT_REASON, str(refused.exception))
        self.assertEqual(self.target.hot.read_bytes(), self.prior_hot)
        self.assertEqual(_tree_snapshot(self.target.segments), self.prior_segments)
        self.assertEqual(_hot_events(self.target.hot), self.prior_events)

    def test_untampered_segmented_import_installs_the_validated_history(self) -> None:
        self._import(None)
        self.assertNotEqual(_hot_events(self.target.hot), self.prior_events)
        self.assertNotEqual(_tree_snapshot(self.target.segments), self.prior_segments)

    def test_hot_member_replaced_after_validation_is_refused(self) -> None:
        with self.assertRaises(ValueError) as refused:
            self._import(self._replace_hot)
        self._assert_refused_unchanged(refused, "hot")

    def test_segment_member_rewritten_after_validation_is_refused(self) -> None:
        with self.assertRaises(ValueError) as refused:
            self._import(self._rewrite_segment)
        self._assert_refused_unchanged(refused, "segment")


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

    def test_validation_reports_the_size_and_digest_it_judged(self) -> None:
        member = _history_database(self.root / "member.sqlite3", [ORDINARY, ORDINARY])
        content = member.read_bytes()
        expected = (len(content), hashlib.sha256(content).hexdigest())
        self.assertEqual(tuple(SystemBackupService._validate_history_member(member)), expected)
        self.assertEqual(tuple(SystemBackupService._validate_history_member(content)), expected)


if __name__ == "__main__":
    unittest.main()
