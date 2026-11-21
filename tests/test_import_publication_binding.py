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

from history_service.store import HistoryStore
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

    def _assert_refused_unchanged(self, refused: Exception) -> None:
        self.assertTrue(self.publication_seen, "history publication was never reached")
        self.assertIn(validated.CONTENT_REASON, str(refused))
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
            replacement = self.root / "changed-private-copy.sqlite3"
            shutil.copyfile(temp_path, replacement)
            with sqlite3.connect(replacement) as connection:
                connection.execute("UPDATE metric_samples SET value_integer = 777")
                connection.commit()
            replacement_bytes = replacement.read_bytes()
            self.assertEqual(len(replacement_bytes), os.fstat(descriptor).st_size)
            with open(temp_path, "r+b") as handle:
                handle.write(replacement_bytes)
                handle.truncate()
            SystemBackupService._validate_history_member(temp_path)

        with self.assertRaises(ValueError) as refused:
            self._import(rewrite_in_place)
        self._assert_refused_unchanged(refused.exception)
        self.assertNotIn(777, [row[2] for row in refusal._metric_rows(self.live)])
