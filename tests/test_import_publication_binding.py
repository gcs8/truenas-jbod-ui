"""An imported history publication stays bound to the checked private copy.

The private copy must have one name, and the descriptor published by rename
must still contain the bytes bound before SQLite and member checks. Every
fixture is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import hashlib
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

    def _assert_refused_absent(self, refused: Exception) -> None:
        self.assertTrue(self.publication_seen, "history publication was never reached")
        self.assertEqual(str(refused), VALIDATED_MEMBER_CHANGED_MESSAGE)
        self.assertFalse(os.path.lexists(self.live), "a refused first publication left live history")

    def test_clean_private_copy_is_published(self) -> None:
        self._import(None)
        self.assertTrue(self.publication_seen)
        self.assertEqual(refusal._metric_rows(self.live), self.IMPORTED_ROWS)

    def test_validated_copy_is_hashed_before_checks_and_live_recount_follows_publish(
        self,
    ) -> None:
        """Published bytes stay hash-bound; counters derive from the live file."""
        member = refusal._history_database(
            self.root / "member.sqlite3",
            [validated.ORDINARY, validated.ORDINARY],
        )
        with sqlite3.connect(member) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO history_table_counts (table_name, row_count) "
                "VALUES ('metric_samples', 0)"
            )
            connection.commit()
        content = member.read_bytes()
        store = HistoryStore(str(self.live), recover_unreadable_database=False)
        events: list[str] = []
        real_copy = HistoryStore._copy_validated_member
        real_hash = HistoryStore._hash_private_publication_descriptor
        real_read = HistoryStore._read_private_copy_alone
        real_recount = HistoryStore._recount_restored_table_counts
        real_publish = HistoryStore._publish_replacement

        def record_copy(source: Path, descriptor: int, *expected) -> None:
            real_copy(source, descriptor, *expected)
            events.append("copy")

        def record_hash(descriptor: int) -> tuple[int, str]:
            events.append("hash")
            return real_hash(descriptor)

        def record_read(
            cls,
            connection: sqlite3.Connection,
            private_path: Path,
        ) -> None:
            events.append("check")
            real_read(connection, private_path)

        def record_recount(connection: sqlite3.Connection) -> None:
            database_path = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
            events.append("recount:live" if database_path == self.live else "recount:private")
            real_recount(connection)

        def record_publish(
            instance: HistoryStore,
            temp_path: Path,
            target_path: Path,
            **kwargs,
        ) -> None:
            if target_path == self.live:
                events.append("publish")
            real_publish(instance, temp_path, target_path, **kwargs)

        with (
            patch.object(HistoryStore, "_copy_validated_member", staticmethod(record_copy)),
            patch.object(
                HistoryStore,
                "_hash_private_publication_descriptor",
                staticmethod(record_hash),
            ),
            patch.object(HistoryStore, "_read_private_copy_alone", classmethod(record_read)),
            patch.object(
                HistoryStore,
                "_recount_restored_table_counts",
                staticmethod(record_recount),
            ),
            patch.object(HistoryStore, "_publish_replacement", record_publish),
        ):
            store.restore_validated_member(
                member,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                member_check=SystemBackupService._check_history_structure,
            )

        self.assertEqual(
            events,
            ["copy", "hash", "check", "publish", "hash", "hash", "recount:live"],
        )
        with sqlite3.connect(self.live) as connection:
            stored = connection.execute(
                "SELECT row_count FROM history_table_counts WHERE table_name = 'metric_samples'"
            ).fetchone()[0]
            actual = connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        self.assertEqual(stored, actual)

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

    def test_private_copy_changed_before_publish_hash_is_refused(self) -> None:
        real_fsync = store_module.os.fsync
        real_rename = HistoryStore._rename_at2
        changed = False
        changed_path: Path | None = None
        candidate_renames = 0

        def rewrite_after_fsync(descriptor: int) -> None:
            nonlocal changed, changed_path
            real_fsync(descriptor)
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_path.name.endswith(".restore") and not changed:
                changed = True
                changed_path = descriptor_path
                self._rewrite_metric_in_place(descriptor_path, suffix="before-publish-hash")

        def observe_rename(source_path: Path, target_path: Path, *, flags: int) -> None:
            nonlocal candidate_renames
            if source_path == changed_path:
                candidate_renames += 1
            real_rename(source_path, target_path, flags=flags)

        with (
            patch.object(store_module.os, "fsync", side_effect=rewrite_after_fsync),
            patch.object(HistoryStore, "_rename_at2", side_effect=observe_rename),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(changed, "the pre-publish rewrite hook did not run")
        self.assertEqual(candidate_renames, 0, "publication renamed bytes before checking them")
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

    def test_live_path_swapped_during_post_rename_hash_is_refused(self) -> None:
        real_hash = HistoryStore._hash_private_publication_descriptor
        original_copy = self.root / "original-live-copy.sqlite3"
        parked = self.root / "published-copy-parked.sqlite3"
        shutil.copyfile(self.live, original_copy)
        swapped = False

        def hash_then_swap_path(descriptor: int) -> tuple[int, str]:
            nonlocal swapped
            digest = real_hash(descriptor)
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if descriptor_path == self.live and not swapped:
                swapped = True
                os.replace(self.live, parked)
                os.replace(original_copy, self.live)
            return digest

        with (
            patch.object(
                HistoryStore,
                "_hash_private_publication_descriptor",
                staticmethod(hash_then_swap_path),
            ),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(swapped, "the live-path swap hook did not run")
        self.assertEqual(str(refused.exception), VALIDATED_MEMBER_CHANGED_MESSAGE)
        self.assertEqual(refusal._dump(self.live), self.original_dump)
        self.assertEqual(refusal._metric_rows(self.live), self.original_rows)

    def test_absent_live_file_rewritten_after_rename_is_removed(self) -> None:
        self.live.unlink()
        real_rename = HistoryStore._rename_at2
        changed = False

        def rename_then_change(source_path: Path, target_path: Path, *, flags: int) -> None:
            nonlocal changed
            real_rename(source_path, target_path, flags=flags)
            if target_path == self.live and not changed:
                changed = True
                self._rewrite_metric_in_place(target_path, suffix="absent-after-rename")

        with (
            patch.object(HistoryStore, "_rename_at2", side_effect=rename_then_change),
            self.assertRaises(ValueError) as refused,
        ):
            self._import(None)
        self.assertTrue(changed, "the absent-target rewrite hook did not run")
        self._assert_refused_absent(refused.exception)

    def test_absent_live_file_linked_after_rename_is_removed(self) -> None:
        self.live.unlink()
        real_rename = HistoryStore._rename_at2
        alias = self.root / "absent-published-copy-alias.sqlite3"
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
        self.assertTrue(linked, "the absent-target link hook did not run")
        self._assert_refused_absent(refused.exception)

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
