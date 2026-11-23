"""An imported history publication stays bound to the checked private copy.

The private copy must have one name, and the descriptor published by rename
must still contain the bytes accepted after SQLite and member checks. Every
fixture is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from history_service import store as store_module
from history_service.store import HistoryStore, VALIDATED_MEMBER_CHANGED_MESSAGE
from history_service.system_backup import SystemBackupService
from tests import test_import_validated_member as validated
from tests import test_restore_refusal_cleanup as refusal


class ImportPublicationBindingTests(refusal._ImportRootCleanup):
    """The checked private copy is the exact single-link file published."""

    _service = refusal.RollbackSnapshotTimestampBoundTests._service
    _artifact = refusal.RollbackSnapshotTimestampBoundTests._artifact
    ORDINARY = validated.ORDINARY
    IMPORTED_ROWS = validated.ImportInstallsTheValidatedMemberTests.IMPORTED_ROWS

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)
        self.artifact = self._artifact()
        live_dir = self.root / "live-history"
        live_dir.mkdir()
        self.live = refusal._history_database(
            live_dir / "history.sqlite3",
            [validated.ORDINARY],
        )
        self.target = self._service(self.root / "target", self.live)
        self.original_dump = refusal._dump(self.live)
        self.original_rows = refusal._metric_rows(self.live)
        self.publication_seen = False

    def _import(
        self,
        change_private_copy: Callable[[Path, int], None] | None,
    ) -> None:
        real_publish = HistoryStore._publish_replacement

        def change_then_publish(
            store: HistoryStore,
            temp_path: Path,
            target_path: Path,
            **kwargs,
        ) -> None:
            if target_path == self.live and not self.publication_seen:
                descriptor = kwargs.get("temp_descriptor")
                self.assertIsInstance(descriptor, int)
                if not isinstance(descriptor, int):
                    self.fail("history publication did not retain its descriptor")
                before = os.fstat(descriptor)
                self.publication_seen = True
                if change_private_copy is not None:
                    change_private_copy(temp_path, descriptor)
                after = os.fstat(descriptor)
                self.assertEqual(
                    (after.st_dev, after.st_ino),
                    (before.st_dev, before.st_ino),
                    "the private publication descriptor changed identity",
                )
            real_publish(store, temp_path, target_path, **kwargs)

        with patch.object(HistoryStore, "_publish_replacement", change_then_publish):
            self.target.import_bundle_from_file(
                self.artifact.path,
                passphrase=refusal.PASSPHRASE,
            )

    def _rewrite_metric_in_place(self, path: Path, *, suffix: str) -> None:
        replacement = self.root / f"changed-private-copy-{suffix}.sqlite3"
        shutil.copyfile(path, replacement)
        with sqlite3.connect(replacement) as connection:
            connection.execute("UPDATE metric_samples SET value_integer = 777")
            connection.commit()
        replacement_bytes = replacement.read_bytes()
        self.assertEqual(len(replacement_bytes), path.stat().st_size)
        with path.open("r+b") as handle:
            handle.write(replacement_bytes)
            handle.truncate()

    def _assert_refused_unchanged(
        self,
        refused: Exception,
        *,
        publication_seen: bool | None = True,
    ) -> None:
        if publication_seen is not None:
            self.assertIs(self.publication_seen, publication_seen)
        self.assertEqual(str(refused), VALIDATED_MEMBER_CHANGED_MESSAGE)
        self.assertEqual(refusal._dump(self.live), self.original_dump)
        self.assertEqual(refusal._metric_rows(self.live), self.original_rows)

    def test_clean_private_copy_is_published(self) -> None:
        self._import(None)
        self.assertTrue(self.publication_seen)
        self.assertEqual(refusal._metric_rows(self.live), self.IMPORTED_ROWS)

    def test_multiply_linked_private_copy_is_refused(self) -> None:
        alias = self.root / "private-copy-alias.sqlite3"

        def add_link(temp_path: Path, descriptor: int) -> None:
            os.link(temp_path, alias)
            self.assertEqual(os.fstat(descriptor).st_nlink, 2)

        with self.assertRaises(ValueError) as refused:
            self._import(add_link)
        self._assert_refused_unchanged(refused.exception)

    def test_private_copy_changed_after_checks_is_refused(self) -> None:
        def rewrite_in_place(temp_path: Path, descriptor: int) -> None:
            self._rewrite_metric_in_place(temp_path, suffix="before-publish")
            self.assertEqual(temp_path.stat().st_size, os.fstat(descriptor).st_size)
            SystemBackupService._validate_history_member(temp_path)

        with self.assertRaises(ValueError) as refused:
            self._import(rewrite_in_place)
        self._assert_refused_unchanged(refused.exception)
        self.assertNotIn(777, [row[2] for row in refusal._metric_rows(self.live)])

    def test_private_copy_changed_during_member_checks_is_refused(self) -> None:
        real_check = SystemBackupService._check_history_structure

        def check_then_change(connection: sqlite3.Connection, **kwargs) -> None:
            real_check(connection, **kwargs)
            database_path = str(connection.execute("PRAGMA database_list").fetchone()[2])
            if database_path.endswith(".restore"):
                connection.execute("UPDATE metric_samples SET value_integer = 777")
                connection.commit()

        with (
            patch.object(SystemBackupService, "_check_history_structure", check_then_change),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self._assert_refused_unchanged(refused.exception, publication_seen=None)

    def test_live_file_rewritten_after_rename_is_rolled_back(self) -> None:
        real_rename = HistoryStore._rename_at2
        changed = False

        def rename_then_change(source_path: Path, target_path: Path, *, flags: int) -> None:
            nonlocal changed
            real_rename(source_path, target_path, flags=flags)
            if target_path == self.live and not changed:
                changed = True
                self._rewrite_metric_in_place(target_path, suffix="after-rename")

        with (
            patch.object(HistoryStore, "_rename_at2", side_effect=rename_then_change),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(changed, "the post-rename rewrite hook did not run")
        self._assert_refused_unchanged(refused.exception)

    def test_live_file_linked_after_rename_is_rolled_back(self) -> None:
        real_rename = HistoryStore._rename_at2
        alias = self.root / "published-copy-alias.sqlite3"
        linked = False

        def rename_then_link(source_path: Path, target_path: Path, *, flags: int) -> None:
            nonlocal linked
            real_rename(source_path, target_path, flags=flags)
            if target_path == self.live and not linked:
                linked = True
                os.link(target_path, alias)

        with (
            patch.object(HistoryStore, "_rename_at2", side_effect=rename_then_link),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(linked, "the post-rename link hook did not run")
        self._assert_refused_unchanged(refused.exception)

    def test_metadata_change_during_hash_is_refused(self) -> None:
        real_hash = store_module.hash_descriptor
        metadata_changed = False

        def hash_then_touch(descriptor, metadata, *, changed):
            nonlocal metadata_changed
            digest = real_hash(descriptor, metadata, changed=changed)
            private_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if not metadata_changed and private_path.name.endswith(".restore"):
                metadata_changed = True
                current = private_path.stat()
                os.utime(
                    private_path,
                    ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
                )
            return digest

        with (
            patch.object(store_module, "hash_descriptor", side_effect=hash_then_touch),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(metadata_changed, "the hash-time metadata hook did not run")
        self._assert_refused_unchanged(refused.exception, publication_seen=None)
