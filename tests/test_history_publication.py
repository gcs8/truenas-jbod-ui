from __future__ import annotations

import asyncio
import ast
import errno
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from history_service import store as history_store
from history_service.collector import HistoryCollector
from history_service.config import HistorySettings, get_history_settings
from history_service.store import HistoryStore
from history_service.startup import HistoryStartupError, open_history_store_with_retries


class HistoryPublicationDurabilityTests(unittest.TestCase):
    """Real SQLite/rename/fsync probes; not a power-loss simulation."""

    @contextmanager
    def _observe_publication(self, store, *, fail_syncs=()):
        events = []
        real_sync = os.fsync
        real_rename = store._rename_at2
        real_unlink = os.unlink
        directory_syncs = 0

        def sync(fd):
            nonlocal directory_syncs
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append((kind, path))
            if kind == "directory":
                directory_syncs += 1
                if directory_syncs in fail_syncs:
                    raise OSError(errno.EIO, f"injected directory sync {directory_syncs}")
            real_sync(fd)

        def rename(source, target, *, flags):
            real_rename(source, target, flags=flags)
            events.append(("rename", source, target, flags))

        def unlink(path, *, dir_fd=None):
            resolved = Path(path)
            if dir_fd is not None:
                resolved = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
            events.append(("unlink", resolved))
            if dir_fd is None:
                real_unlink(path)
            else:
                real_unlink(path, dir_fd=dir_fd)

        with (
            patch("history_service.store.os.fsync", side_effect=sync),
            patch.object(store, "_rename_at2", side_effect=rename),
            patch("history_service.store.os.unlink", side_effect=unlink),
        ):
            yield events

    def _seed(self, root):
        store = HistoryStore(str(root / "history.db"))
        with closing(sqlite3.connect(store.file_path)) as connection:
            connection.execute("CREATE TABLE synthetic_publication (value TEXT)")
            connection.execute("INSERT INTO synthetic_publication VALUES ('prior')")
            connection.commit()
        return store

    def _change(self, store):
        with closing(sqlite3.connect(store.file_path)) as connection:
            connection.execute("UPDATE synthetic_publication SET value = 'successor'")
            connection.commit()

    def _value(self, path):
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            return connection.execute("SELECT value FROM synthetic_publication").fetchone()[0]

    @contextmanager
    def _observe_directory_entries(self, store):
        real_mkdir = os.mkdir
        real_rename = store._rename_at2
        real_local_prune = store._prune_backup_snapshots
        real_archive_prune = store._prune_named_backups
        with self._observe_publication(store) as events:
            observed_rename = store._rename_at2

            def mkdir(path, mode=0o777, *, dir_fd=None):
                real_mkdir(path, mode, dir_fd=dir_fd)
                resolved = Path(path) if dir_fd is None else Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
                events.append(("created-directory", resolved))

            def rename(source, target, *, flags):
                if source.is_dir():
                    real_rename(source, target, flags=flags)
                    events.append(("created-directory", target))
                else:
                    observed_rename(source, target, flags=flags)

            def local_prune(*args):
                path, count = args[-2:]
                events.append(("prune", path))
                return real_local_prune(*args)

            def archive_prune(path, pattern, count):
                events.append(("prune", path))
                return real_archive_prune(path, pattern, count)

            with (
                patch("history_service.store.os.mkdir", side_effect=mkdir),
                patch.object(store, "_rename_at2", side_effect=rename),
                patch.object(store, "_prune_backup_snapshots", local_prune),
                patch.object(store, "_prune_named_backups", side_effect=archive_prune),
            ):
                yield events

    @contextmanager
    def _fail_entry_parent(self, parent):
        real_sync = os.fsync
        attempts = []

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and Path(os.readlink(f"/proc/self/fd/{fd}")) == parent:
                attempts.append(parent)
                raise OSError(errno.EIO, "injected new-directory parent sync")
            real_sync(fd)

        with patch("history_service.store.os.fsync", side_effect=sync):
            yield attempts

    def _production_runtime(self):
        # Execute unchanged construction bodies without importing app startup.
        from history_service.startup import DEFAULT_ATTEMPTS, DEFAULT_INITIAL_BACKOFF_SECONDS
        from history_service.sqlite_temp import configure_sqlite_temporary_directory
        from history_service.startup_migration import open_history_store_after_recovery

        source = Path(history_store.__file__).with_name("main.py")
        names = {"build_history_store", "_configured_history_directory", "open_history_runtime"}
        functions = [node for node in ast.parse(source.read_bytes()).body
                     if isinstance(node, ast.FunctionDef) and node.name in names]
        self.assertEqual({node.name for node in functions}, names)
        namespace: dict[str, Any] = dict(
            HistorySettings=HistorySettings, HistoryStore=HistoryStore,
            get_history_settings=get_history_settings, Path=Path, os=os, time=time,
            Callable=Any, DEFAULT_ATTEMPTS=DEFAULT_ATTEMPTS,
            DEFAULT_INITIAL_BACKOFF_SECONDS=DEFAULT_INITIAL_BACKOFF_SECONDS,
            open_history_store_with_retries=open_history_store_with_retries,
            open_history_store_after_recovery=open_history_store_after_recovery,
            configure_sqlite_temporary_directory=configure_sqlite_temporary_directory,
        )
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        get_history_settings.cache_clear()
        try:
            return namespace["open_history_runtime"](attempts=1, initial_backoff_seconds=0)
        finally:
            get_history_settings.cache_clear()

    def _loaded_backup(self, settings, store):
        return store.create_backup(
            settings.backup_dir, snapshot_label="2030-01-01T00:00:00Z", retention_count=1,
            long_term_backup_dir=settings.long_term_backup_dir,
            weekly_retention_count=1, monthly_retention_count=1,
        )

    def test_production_loader_preserves_directory_entry_obligations(self):
        for layout in ("separate", "nested", "default-existing-db", "default-fresh-db",
                       "nested-database", "database-inside-backup", "existing"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db = root / "history.db"
                if layout not in ("default-fresh-db", "nested-database", "database-inside-backup"):
                    self._seed(root)
                default = layout.startswith("default")
                local = root / "backups" if default else root / "local"
                archive = local / "long-term" if default else root / "archive"
                if layout == "nested":
                    local, archive = root / "one" / "local", root / "two" / "archive"
                elif layout == "nested-database":
                    db = root / "new" / "database" / "history.db"
                    local, archive = db.parent / "backups", db.parent / "backups" / "long-term"
                elif layout == "database-inside-backup":
                    db = local / "database" / "history.db"
                destinations = [local, archive / "weekly", archive / "monthly"]
                if layout == "existing":
                    for directory in destinations:
                        directory.mkdir(parents=True, exist_ok=True)
                required = set()
                for directory in destinations:
                    while not directory.exists():
                        required.add(directory)
                        directory = directory.parent
                env = {"HISTORY_SQLITE_PATH": str(db), "RELEASE_CHECK_ENABLED": "false"}
                if not default:
                    env.update(HISTORY_BACKUP_DIR=str(local), HISTORY_LONG_TERM_BACKUP_DIR=str(archive))
                # Observe before the real settings loader, not only create_backup.
                with patch.dict(os.environ, env, clear=True), self._observe_directory_entries(HistoryStore) as events:
                    settings, store = self._production_runtime()
                    result = self._loaded_backup(settings, store)
                    events.append(("return", result))
                for directory in required:
                    created = next(i for i, event in enumerate(events) if event == ("created-directory", directory))
                    barrier = next((i for i, event in enumerate(events)
                                    if i > created and event == ("directory", directory.parent)), None)
                    self.assertIsNotNone(barrier, f"loader-created entry lacks parent barrier: {directory}")
                    assert barrier is not None
                    prunes = [i for i, event in enumerate(events)
                              if event[0] == "prune" and event[1].is_relative_to(directory)]
                    self.assertTrue(prunes)
                    self.assertLess(barrier, min(prunes))
                allowed = {directory.parent for directory in required} | set(destinations)
                for event in events:
                    if event[0] == "directory":
                        self.assertTrue(event[1] in allowed or event[1].name.startswith(".history-"), event)
                for directory in destinations:
                    snapshot, = directory.glob("*.sqlite3")
                    with closing(sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)) as connection:
                        self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                        self.assertGreater(connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_production_loader_local_barrier_failure_retries_without_ack_or_prune(self):
        for defaults in (False, True):
            for position in range(2 if not defaults else 1):
                with self.subTest(defaults=defaults, position=position), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._seed(root)
                    local = root / "backups" if defaults else root / "one" / "local"
                    parent = local.parent if position == 0 else root
                    env = {"HISTORY_SQLITE_PATH": str(root / "history.db"), "RELEASE_CHECK_ENABLED": "false"}
                    if not defaults:
                        env.update(HISTORY_BACKUP_DIR=str(local), HISTORY_LONG_TERM_BACKUP_DIR=str(root / "archive"))
                    with patch.dict(os.environ, env, clear=True):
                        for attempt in range(2):
                            # Loader preparation leaves the barrier to the operation.
                            with self._fail_entry_parent(parent) as failures:
                                settings, store = self._production_runtime()
                                with patch.object(store, "_prune_backup_snapshots", wraps=store._prune_backup_snapshots) as prune:
                                    with self.assertRaisesRegex(OSError, "injected new-directory parent sync"):
                                        self._loaded_backup(settings, store)
                            self.assertEqual(failures, [parent], f"fresh runtime retry {attempt}")
                            prune.assert_not_called()
                            self.assertEqual(list(local.glob("*.sqlite3")), [])
                        settings, store = self._production_runtime()
                        with self._observe_directory_entries(store) as events:
                            result = self._loaded_backup(settings, store)
                        self.assertIn(("directory", parent), events)
                        self.assertEqual(self._value(result), "prior")

    def test_production_loader_archive_barrier_remains_best_effort_across_retries(self):
        for period in ("weekly", "monthly"):
            for position in range(3):
                with self.subTest(period=period, position=position), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._seed(root)
                    local = root / "local"
                    local.mkdir()
                    archive = root / "one" / "archive"
                    chain = [root / "one", archive, archive / period]
                    parent = chain[position].parent
                    env = {"HISTORY_SQLITE_PATH": str(root / "history.db"),
                           "HISTORY_BACKUP_DIR": str(local), "HISTORY_LONG_TERM_BACKUP_DIR": str(archive),
                           "RELEASE_CHECK_ENABLED": "false"}
                    with patch.dict(os.environ, env, clear=True):
                        for attempt in range(2):
                            with self._fail_entry_parent(parent) as failures:
                                settings, store = self._production_runtime()
                                with patch.object(store, "_prune_named_backups", wraps=store._prune_named_backups) as prune:
                                    with self.assertLogs("history_service.store", level="WARNING"):
                                        result = store.create_backup(
                                            settings.backup_dir, snapshot_label="2030-01-01T00:00:00Z",
                                            long_term_backup_dir=settings.long_term_backup_dir,
                                            weekly_retention_count=int(period == "weekly"),
                                            monthly_retention_count=int(period == "monthly"),
                                        )
                            self.assertEqual(failures, [parent], f"fresh runtime archive retry {attempt}")
                            prune.assert_not_called()
                            self.assertEqual(self._value(result), "prior")
                        settings, store = self._production_runtime()
                        with self._observe_directory_entries(store) as events:
                            result = self._loaded_backup(settings, store)
                        self.assertIn(("directory", parent), events)
                        self.assertEqual(self._value(result), "prior")
                        self.assertEqual(self._value(next(chain[-1].glob("*.sqlite3"))), "prior")

    def test_loader_preparation_failure_never_adopts_unmarked_created_roots(self):
        for boundary in ("marker-open", "marker-sync", "staging-sync", "install"):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._seed(root)
                    local = root / "one" / "local"
                    env = {"HISTORY_SQLITE_PATH": str(root / "history.db"),
                           "HISTORY_BACKUP_DIR": str(local), "HISTORY_LONG_TERM_BACKUP_DIR": str(root / "archive"),
                           "RELEASE_CHECK_ENABLED": "false"}
                    real_open, real_sync, real_rename = os.open, os.fsync, HistoryStore._rename_at2
                    hits = []

                    def fail():
                        hits.append(boundary)
                        raise OSError(errno.EIO, "injected loader preparation")

                    def open_file(path, flags, mode=0o777, *, dir_fd=None):
                        match = boundary == "marker-open" and Path(path).name == ".history-backup-directory-pending" and not hits
                        if match and not after:
                            fail()
                        fd = real_open(path, flags, mode, dir_fd=dir_fd)
                        if match and after:
                            os.close(fd)
                            fail()
                        return fd

                    def sync(fd):
                        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                        match = ((boundary == "marker-sync" and path.name == ".history-backup-directory-pending")
                                 or (boundary == "staging-sync" and path.name.startswith(".history-directory-"))) and not hits
                        if match and not after:
                            fail()
                        real_sync(fd)
                        if match and after:
                            fail()

                    def rename(source, target, *, flags):
                        match = boundary == "install" and target == root / "one" and not hits
                        if match and not after:
                            fail()
                        real_rename(source, target, flags=flags)
                        if match and after:
                            fail()

                    with patch.dict(os.environ, env, clear=True):
                        with (patch("os.open", side_effect=open_file), patch("os.fsync", side_effect=sync),
                              patch.object(HistoryStore, "_rename_at2", side_effect=rename)):
                            with self.assertRaises((OSError, HistoryStartupError)):
                                self._production_runtime()
                        self.assertEqual(hits, [boundary])
                        self.assertEqual(list(local.glob("*.sqlite3")), [])
                        if (root / "one").exists():
                            self.assertTrue((root / "one" / ".history-backup-directory-pending").is_file())
                        # A fresh loader and store must still pay the root barrier.
                        with self._fail_entry_parent(root) as failures:
                            settings, store = self._production_runtime()
                            with self.assertRaisesRegex(OSError, "injected new-directory parent sync"):
                                self._loaded_backup(settings, store)
                        self.assertEqual(failures, [root])
                        settings, store = self._production_runtime()
                        self.assertEqual(self._value(self._loaded_backup(settings, store)), "prior")

    def test_production_construction_retries_every_sync_and_destructive_boundary(self):
        def run(failed=None, after=False):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._seed(root)
                local, archive = root / "one" / "local", root / "two" / "archive"
                env = {"HISTORY_SQLITE_PATH": str(root / "history.db"),
                       "HISTORY_BACKUP_DIR": str(local), "HISTORY_LONG_TERM_BACKUP_DIR": str(archive),
                       "RELEASE_CHECK_ENABLED": "false"}
                events, hits, prunes = [], [], []
                phase = "loader"
                real_sync, real_rename, real_unlink = os.fsync, HistoryStore._rename_at2, os.unlink
                real_local_prune, real_archive_prune = HistoryStore._prune_backup_snapshots, HistoryStore._prune_named_backups

                def boundary(kind, path, operation):
                    events.append((phase, kind, path))
                    match = len(events) == failed
                    if match and not after:
                        hits.append(events[-1])
                        raise OSError(errno.EIO, "injected construction boundary")
                    result = operation()
                    if match and after:
                        hits.append(events[-1])
                        raise OSError(errno.EIO, "injected construction boundary")
                    return result

                def sync(fd):
                    return boundary("sync", Path(os.readlink(f"/proc/self/fd/{fd}")), lambda: real_sync(fd))

                def rename(source, target, *, flags):
                    return boundary("rename", target, lambda: real_rename(source, target, flags=flags))

                def unlink(path, *, dir_fd=None):
                    resolved = Path(path) if dir_fd is None else Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
                    return boundary("unlink", resolved, lambda: real_unlink(path, dir_fd=dir_fd))

                def local_prune(store, path, count):
                    prunes.append(path)
                    return real_local_prune(store, path, count)

                def archive_prune(path, pattern, count):
                    prunes.append(path)
                    return real_archive_prune(path, pattern, count)

                result = None
                with patch.dict(os.environ, env, clear=True):
                    with (patch("os.fsync", side_effect=sync), patch("os.unlink", side_effect=unlink),
                          patch.object(HistoryStore, "_rename_at2", side_effect=rename),
                          patch.object(HistoryStore, "_prune_backup_snapshots", local_prune),
                          patch.object(HistoryStore, "_prune_named_backups", side_effect=archive_prune),
                          patch.object(history_store.logger, "warning") as warnings):
                        try:
                            settings, store = self._production_runtime()
                            phase = "backup"
                            result = self._loaded_backup(settings, store)
                        except (OSError, HistoryStartupError):
                            self.assertIsNotNone(failed)
                    if failed is None:
                        self.assertEqual(self._value(result), "prior")
                        warnings.assert_not_called()
                        return len(events)
                    self.assertEqual(len(hits), 1)
                    if hits[0][0] == "loader":
                        self.assertIsNone(result)
                        self.assertEqual(prunes, [])
                    elif hits[0][2].is_relative_to(root / "one") or (hits[0][2] == root and local not in prunes):
                        # A failed local publication cannot earn prune credit.
                        self.assertIsNone(result)
                        self.assertEqual(prunes, [])
                    else:
                        self.assertEqual(self._value(result), "prior")
                    pending = [marker.parent for marker in root.rglob(".history-backup-directory-pending")
                               if not marker.parent.name.startswith(".history-")]
                    # Cache eviction plus a new store must not forget any entry
                    # installed before the interrupted loader/operation returned.
                    with self._observe_directory_entries(HistoryStore) as retry:
                        settings, store = self._production_runtime()
                        result = self._loaded_backup(settings, store)
                    for directory in pending:
                        self.assertIn(("directory", directory.parent), retry)
                        self.assertFalse((directory / ".history-backup-directory-pending").exists())
                    for directory in (local, archive / "weekly", archive / "monthly"):
                        snapshot, = directory.glob("*.sqlite3")
                        self.assertEqual(self._value(snapshot), "prior")
                return len(events)

        count = run()
        self.assertGreater(count, 20)
        for failed in range(1, count + 1):
            for after in (False, True):
                with self.subTest(failed=failed, after=after):
                    run(failed, after)

    def test_loader_pending_markers_refuse_invalid_entries_and_preserve_concurrent_creation(self):
        marker_name = ".history-backup-directory-pending"
        for kind in ("contents", "directory", "symlink", "hardlink", "concurrent"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._seed(root)
                local = root / "local"
                env = {"HISTORY_SQLITE_PATH": str(root / "history.db"),
                       "HISTORY_BACKUP_DIR": str(local), "HISTORY_LONG_TERM_BACKUP_DIR": str(root / "archive"),
                       "RELEASE_CHECK_ENABLED": "false"}
                if kind != "concurrent":
                    local.mkdir()
                    marker = local / marker_name
                    if kind == "contents":
                        marker.write_bytes(b"invalid")
                    elif kind == "directory":
                        marker.mkdir()
                    else:
                        source = root / "synthetic-marker"
                        source.touch()
                        if kind == "symlink":
                            marker.symlink_to(source)
                        else:
                            os.link(source, marker)
                    before = marker.lstat()
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaisesRegex(ValueError, "invalid directory marker"):
                            self._production_runtime()
                    self.assertEqual(marker.lstat(), before)
                    self.assertEqual(list(local.glob("*.sqlite3")), [])
                else:
                    real_rename = HistoryStore._rename_at2
                    winners = []

                    def rename(source, target, *, flags):
                        if target == local and not winners:
                            # A second real loader prepares the winning root.
                            winners.append(target)
                            get_history_settings.cache_clear()
                            get_history_settings()
                        return real_rename(source, target, flags=flags)

                    with patch.dict(os.environ, env, clear=True):
                        with patch.object(HistoryStore, "_rename_at2", side_effect=rename):
                            settings, store = self._production_runtime()
                        self.assertEqual(winners, [local])
                        self.assertTrue((local / marker_name).is_file())
                        with self._fail_entry_parent(root) as failures:
                            with self.assertRaisesRegex(OSError, "injected new-directory parent sync"):
                                self._loaded_backup(settings, store)
                        self.assertEqual(failures, [root])
                        settings, store = self._production_runtime()
                        self.assertEqual(self._value(self._loaded_backup(settings, store)), "prior")
                        self.assertEqual(list(root.glob(".history-directory-*")), [])

    def test_long_term_invalid_marker_is_contained_by_best_effort_promotion(self):
        marker_name = ".history-backup-directory-pending"
        for kind in ("contents", "directory", "symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._seed(root)
                local = root / "local"
                archive = root / "archive"
                archive.mkdir()
                marker = archive / marker_name
                source = root / "synthetic-marker"
                if kind == "contents":
                    marker.write_bytes(b"invalid")
                elif kind == "directory":
                    marker.mkdir()
                else:
                    source.write_bytes(b"synthetic")
                    if kind == "symlink":
                        marker.symlink_to(source)
                    else:
                        os.link(source, marker)
                before = marker.lstat()
                env = {
                    "HISTORY_SQLITE_PATH": str(root / "history.db"),
                    "HISTORY_BACKUP_DIR": str(local),
                    "HISTORY_LONG_TERM_BACKUP_DIR": str(archive),
                    "RELEASE_CHECK_ENABLED": "false",
                }

                with patch.dict(os.environ, env, clear=True):
                    settings, store = self._production_runtime()
                    with patch.object(store, "_prune_named_backups", wraps=store._prune_named_backups) as prune:
                        with self.assertLogs("history_service.store", level="WARNING") as logs:
                            result = self._loaded_backup(settings, store)

                self.assertEqual(self._value(result), "prior")
                self.assertTrue(any("long-term backup promotion failed" in line for line in logs.output))
                self.assertEqual(marker.lstat(), before)
                if kind == "contents":
                    self.assertEqual(marker.read_bytes(), b"invalid")
                elif kind in {"symlink", "hardlink"}:
                    self.assertEqual(source.read_bytes(), b"synthetic")
                self.assertEqual(list(archive.rglob("*.sqlite3")), [])
                prune.assert_not_called()

    def test_public_backup_persists_only_new_directory_entry_chains(self):
        cases = (
            (0, None, (), False),
            (1, None, (), False),
            (2, None, (), False),
            (0, 0, ("weekly",), False),
            (0, 0, ("monthly",), False),
            (0, 1, ("weekly", "monthly"), False),
            (0, 2, ("weekly", "monthly"), False),
            (0, 0, ("weekly", "monthly"), True),
        )
        for local_depth, archive_depth, periods, existing_periods in cases:
            with self.subTest(case=(local_depth, archive_depth, periods, existing_periods)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                local = root / "local-parent" / "local" if local_depth == 2 else root / "local"
                if local_depth == 0:
                    local.mkdir()
                archive = root / "archive-parent" / "archive" if archive_depth == 2 else root / "archive"
                if archive_depth == 0:
                    archive.mkdir()
                if existing_periods:
                    for period in periods:
                        (archive / period).mkdir()
                destinations = [local, *(archive / period for period in periods)]
                required = set()
                for destination in destinations:
                    while not destination.exists():
                        required.add(destination)
                        destination = destination.parent
                with self._observe_directory_entries(store) as events:
                    result = store.create_backup(
                        local, snapshot_label="2030-01-01T00:00:00Z", retention_count=1,
                        long_term_backup_dir=archive if periods else None,
                        weekly_retention_count=int("weekly" in periods),
                        monthly_retention_count=int("monthly" in periods),
                    )
                    events.append(("return", result))
                for directory in required:
                    created = next(i for i, event in enumerate(events) if event == ("created-directory", directory))
                    barrier = next((i for i, event in enumerate(events) if i > created and event == ("directory", directory.parent)), None)
                    self.assertIsNotNone(barrier, f"new directory entry lacks parent barrier: {directory}")
                    assert barrier is not None
                    relevant_prunes = [i for i, event in enumerate(events) if event[0] == "prune" and event[1].is_relative_to(directory)]
                    self.assertTrue(relevant_prunes)
                    self.assertLess(barrier, min(relevant_prunes))
                # No attempt to repair arbitrary pre-existing ancestors.
                allowed = {directory.parent for directory in required} | set(destinations)
                for event in events:
                    if event[0] == "directory":
                        self.assertTrue(event[1] in allowed or event[1].name.startswith(".history-"), event)
                for destination in destinations:
                    snapshots = list(destination.glob("*.sqlite3"))
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(self._value(snapshots[0]), "prior")

    def test_new_local_entry_failure_refuses_pruning_and_retries_in_fresh_store(self):
        for position in range(3):
            with self.subTest(position=position), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                chain = [root / "one", root / "one" / "two", root / "one" / "two" / "local"]
                failed_parent = chain[position].parent
                for attempt in range(2):
                    store = HistoryStore(str(store.file_path))
                    with (
                        self._fail_entry_parent(failed_parent) as failures,
                        patch.object(store, "_prune_backup_snapshots", wraps=store._prune_backup_snapshots) as prune,
                        self.assertRaisesRegex(OSError, "injected new-directory parent sync"),
                    ):
                        store.create_backup(chain[-1], snapshot_label="2030-01-01T00:00:00Z")
                    self.assertEqual(failures, [failed_parent], f"fresh retry {attempt} skipped its failed entry")
                    prune.assert_not_called()
                    self.assertEqual(list(chain[-1].glob("*.sqlite3")), [])
                store = HistoryStore(str(store.file_path))
                with self._observe_directory_entries(store) as events:
                    result = store.create_backup(chain[-1], snapshot_label="2030-01-01T00:00:00Z")
                self.assertIn(("directory", failed_parent), events)
                self.assertEqual(self._value(result), "prior")
                # Once acknowledged, an ordinary repeat must not sync ancestors.
                store = HistoryStore(str(store.file_path))
                with self._observe_directory_entries(store) as events:
                    store.create_backup(chain[-1], snapshot_label="2030-01-02T00:00:00Z", retention_count=1)
                self.assertNotIn(("directory", failed_parent), events)

    def test_new_archive_entry_failure_keeps_local_backup_and_retries_in_fresh_store(self):
        for period in ("weekly", "monthly"):
            for position in range(3):
                with self.subTest(period=period, position=position), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    store = self._seed(root)
                    local = root / "local"
                    local.mkdir()
                    archive = root / "one" / "archive"
                    chain = [root / "one", archive, archive / period]
                    failed_parent = chain[position].parent
                    options: dict[str, Any] = dict(long_term_backup_dir=archive, weekly_retention_count=0, monthly_retention_count=0)
                    options[f"{period}_retention_count"] = 1
                    for attempt in range(2):
                        store = HistoryStore(str(store.file_path))
                        with (
                            self._fail_entry_parent(failed_parent) as failures,
                            patch.object(store, "_prune_named_backups", wraps=store._prune_named_backups) as prune,
                            self.assertLogs("history_service.store", level="WARNING"),
                        ):
                            result = store.create_backup(local, snapshot_label="2030-01-01T00:00:00Z", **options)
                        self.assertEqual(failures, [failed_parent], f"fresh archive retry {attempt} skipped its failed entry")
                        prune.assert_not_called()
                        self.assertEqual(self._value(result), "prior")
                        self.assertEqual(list(chain[-1].glob("*.sqlite3")), [])
                    store = HistoryStore(str(store.file_path))
                    with self._observe_directory_entries(store) as events:
                        result = store.create_backup(local, snapshot_label="2030-01-01T00:00:00Z", **options)
                    self.assertIn(("directory", failed_parent), events)
                    self.assertEqual(self._value(result), "prior")
                    self.assertEqual(self._value(next(chain[-1].glob("*.sqlite3"))), "prior")

    def test_new_directory_preparation_and_marker_retirement_fail_closed(self):
        for boundary in ("marker-sync", "staging-sync", "install", "marker-unlink"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                local = root / "local"
                real_sync, real_rename, real_unlink = os.fsync, store._rename_at2, os.unlink
                failures = []

                def fail():
                    failures.append(boundary)
                    raise OSError(errno.EIO, "injected directory preparation failure")

                def sync(fd):
                    path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                    is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if boundary == "marker-sync" and path.name == ".history-backup-directory-pending":
                        fail()
                    if boundary == "staging-sync" and is_directory and path.name.startswith(".history-directory-"):
                        fail()
                    return real_sync(fd)

                def rename(source, target, *, flags):
                    if boundary == "install" and target == local:
                        fail()
                    return real_rename(source, target, flags=flags)

                def unlink(path, *, dir_fd=None):
                    if boundary == "marker-unlink" and Path(path) == local / ".history-backup-directory-pending":
                        fail()
                    return real_unlink(path, dir_fd=dir_fd)

                with (
                    patch("history_service.store.os.fsync", side_effect=sync),
                    patch.object(store, "_rename_at2", side_effect=rename),
                    patch("history_service.store.os.unlink", side_effect=unlink),
                    patch.object(store, "_prune_backup_snapshots", wraps=store._prune_backup_snapshots) as prune,
                    self.assertRaisesRegex(OSError, "injected directory preparation failure"),
                ):
                    store.create_backup(local, snapshot_label="2030-01-01T00:00:00Z")
                self.assertEqual(failures, [boundary])
                prune.assert_not_called()
                self.assertEqual(list(local.glob("*.sqlite3")), [])
                self.assertEqual(local.exists(), boundary == "marker-unlink")
                self.assertEqual(list(root.glob(".history-directory-*")), [])
                store = HistoryStore(str(store.file_path))
                with self._observe_directory_entries(store) as events:
                    result = store.create_backup(local, snapshot_label="2030-01-01T00:00:00Z")
                self.assertIn(("directory", root), events)
                self.assertEqual(self._value(result), "prior")

    def test_local_publication_syncs_file_and_both_parents_before_retirement(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                prior = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                label = "2030-01-01T00:00:00Z" if existing else "2030-01-02T00:00:00Z"
                self._change(store)
                with self._observe_publication(store) as events:
                    result = store.create_backup(root / "backups", snapshot_label=label, retention_count=1)
                rename_index = next(i for i, event in enumerate(events) if event[0] == "rename")
                rename = events[rename_index]
                self.assertEqual(rename[3], 2 if existing else 1)
                self.assertTrue(any(event[0] == "file" for event in events[:rename_index]))
                barriers = events[rename_index + 1:rename_index + 3]
                self.assertEqual(barriers, [("directory", rename[1].parent), ("directory", rename[2].parent)])
                unlinks = [i for i, event in enumerate(events) if event[0] == "unlink"]
                self.assertTrue(unlinks)
                self.assertGreater(min(unlinks), rename_index + 2)
                self.assertEqual(self._value(result), "successor")
                if not existing:
                    self.assertFalse(prior.exists())

    def test_local_directory_sync_failures_preserve_prior_and_gate_pruning(self):
        for existing in (False, True):
            for failed in (1, 2):
                with self.subTest(existing=existing, failed=failed), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    store = self._seed(root)
                    prior = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                    before = prior.read_bytes()
                    self._change(store)
                    label = "2030-01-01T00:00:00Z" if existing else "2030-01-02T00:00:00Z"
                    with (
                        self._observe_publication(store, fail_syncs=(failed,)),
                        patch.object(store, "_prune_backup_snapshots", wraps=store._prune_backup_snapshots) as prune,
                        self.assertRaisesRegex(OSError, "injected directory sync"),
                    ):
                        store.create_backup(root / "backups", snapshot_label=label, retention_count=1)
                    prune.assert_not_called()
                    self.assertEqual(prior.read_bytes(), before)
                    self.assertEqual(self._value(prior), "prior")
                    self.assertEqual(list((root / "backups").glob("*.sqlite3")), [prior])

    def test_promotion_durability_and_failures_gate_weekly_and_monthly_pruning(self):
        for period in ("weekly", "monthly"):
            for existing in (False, True):
                for failed in (None, 1, 2):
                    with self.subTest(period=period, existing=existing, failed=failed), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        store = self._seed(root)
                        source = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                        options = dict(long_term_backup_dir=root / "archive", weekly_retention_count=0, monthly_retention_count=0)
                        options[f"{period}_retention_count"] = 1
                        store._promote_long_term_backups(source, snapshot_label="2030-01-01T00:00:00Z", **options)
                        prior = next((root / "archive" / period).glob("*.sqlite3"))
                        before = prior.read_bytes()
                        self._change(store)
                        source = store.create_backup(root / "backups", snapshot_label="2030-03-01T00:00:00Z")
                        label = "2030-01-01T00:00:00Z" if existing else "2030-03-01T00:00:00Z"
                        with (
                            self._observe_publication(store, fail_syncs=(() if failed is None else (failed,))) as events,
                            patch.object(store, "_prune_named_backups", wraps=store._prune_named_backups) as prune,
                        ):
                            if failed is None:
                                store._promote_long_term_backups(source, snapshot_label=label, **options)
                            else:
                                with self.assertRaisesRegex(OSError, "injected directory sync"):
                                    store._promote_long_term_backups(source, snapshot_label=label, **options)
                        if failed is not None:
                            prune.assert_not_called()
                            self.assertEqual(prior.read_bytes(), before)
                            self.assertEqual(self._value(prior), "prior")
                        else:
                            prune.assert_called_once()
                            index = next(i for i, event in enumerate(events) if event[0] == "rename")
                            rename = events[index]
                            self.assertEqual(rename[3], 2 if existing else 1)
                            self.assertTrue(any(event[0] == "file" for event in events[:index]))
                            self.assertEqual(events[index + 1:index + 3], [
                                ("directory", rename[1].parent), ("directory", rename[2].parent),
                            ])
                            self.assertTrue(all(i > index + 2 for i, event in enumerate(events) if event[0] == "unlink"))
                            self.assertEqual(self._value(next(prior.parent.glob("*.sqlite3"))), "successor")

    def test_restore_directory_sync_failure_rolls_back_before_sidecar_cleanup(self):
        for failed in (1, 2):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                backup = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                self._change(store)
                before = store.file_path.read_bytes()
                family = [Path(f"{store.file_path}{suffix}") for suffix in ("-wal", "-shm")]
                for path in family:
                    path.write_bytes(b"synthetic-sidecar")
                with self._observe_publication(store, fail_syncs=(failed,)), self.assertRaises(OSError):
                    store.restore_backup(backup)
                self.assertEqual(store.file_path.read_bytes(), before)
                for path in family:
                    self.assertEqual(path.read_bytes(), b"synthetic-sidecar")

    def test_rollback_sync_failure_keeps_prior_generation(self):
        for rollback_failure in (2, 3):
            with self.subTest(rollback_failure=rollback_failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                prior = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                before = prior.read_bytes()
                self._change(store)
                with (
                    self._observe_publication(store, fail_syncs=(1, rollback_failure)) as events,
                    self.assertRaises(OSError),
                ):
                    store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                self.assertEqual(prior.read_bytes(), before)
                self.assertEqual(self._value(prior), "prior")
                self.assertEqual(len([e for e in events if e[0] == "rename"]), 2)
                self.assertEqual(len([e for e in events if e[0] == "directory"]), rollback_failure)

    def test_file_sync_failure_prevents_publication_and_preserves_prior(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                prior = store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z")
                assert prior is not None
                before = prior.read_bytes()
                self._change(store)
                label = "2030-01-01T00:00:00Z" if existing else "2030-01-02T00:00:00Z"
                with (
                    patch("history_service.store.os.fsync", side_effect=OSError(errno.EIO, "injected file sync")),
                    patch.object(store, "_rename_at2", wraps=store._rename_at2) as rename,
                    self.assertRaisesRegex(OSError, "injected file sync"),
                ):
                    store.create_backup(root / "backups", snapshot_label=label, retention_count=1)
                rename.assert_not_called()
                self.assertEqual(prior.read_bytes(), before)
                self.assertEqual(self._value(prior), "prior")

    def test_archive_failure_keeps_existing_best_effort_local_backup_contract(self):
        for period in ("weekly", "monthly"):
            with self.subTest(period=period), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                options: dict[str, Any] = dict(
                    long_term_backup_dir=root / "archive",
                    weekly_retention_count=0, monthly_retention_count=0,
                )
                options[f"{period}_retention_count"] = 1
                store.create_backup(root / "backups", snapshot_label="2030-01-01T00:00:00Z", **options)
                prior = next((root / "archive" / period).glob("*.sqlite3"))
                before = prior.read_bytes()
                self._change(store)
                # Local publication has two directory barriers; fail the first
                # archive barrier, not the already-durable local snapshot.
                with (
                    self._observe_publication(store, fail_syncs=(3,)),
                    patch.object(store, "_prune_named_backups", wraps=store._prune_named_backups) as prune,
                    self.assertLogs("history_service.store", level="WARNING") as warnings,
                ):
                    result = store.create_backup(
                        root / "backups", snapshot_label="2030-03-01T00:00:00Z", **options,
                    )
                prune.assert_not_called()
                self.assertIn("long-term backup promotion failed", warnings.output[0])
                self.assertEqual(self._value(result), "successor")
                self.assertEqual(prior.read_bytes(), before)
                self.assertEqual(list(prior.parent.glob("*.sqlite3")), [prior])

    def test_collector_does_not_credit_failed_real_publication_for_retention(self):
        for failed in (None, 1, 2):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = self._seed(root)
                collector = HistoryCollector(HistorySettings(
                    sqlite_path=str(store.file_path), backup_dir=str(root / "backups"),
                    startup_grace_seconds=0,
                ), store)
                collector.last_fast_metrics_at = collector.started_at
                collector._enumerate_scopes = AsyncMock(return_value=[])
                with (
                    self._observe_publication(store, fail_syncs=(failed,)),
                    patch.object(store, "maintain_retention", wraps=store.maintain_retention) as retention,
                    patch.object(collector, "_run_retention_if_due", wraps=collector._run_retention_if_due) as gate,
                ):
                    asyncio.run(collector.run_once(force_slow=True))
                if failed is None:
                    self.assertTrue(gate.call_args.kwargs["backup_succeeded"])
                    self.assertIsNotNone(gate.call_args.kwargs["backup_at"])
                    retention.assert_called_once()
                    self.assertIsNotNone(collector.last_backup_at)
                    self.assertIsNone(collector.last_backup_error)
                    self.assertEqual(len(list((root / "backups").glob("*.sqlite3"))), 1)
                else:
                    self.assertFalse(gate.call_args.kwargs["backup_succeeded"])
                    self.assertIsNone(gate.call_args.kwargs["backup_at"])
                    retention.assert_not_called()
                    self.assertIsNone(collector.last_backup_at)
                    self.assertIsNotNone(collector.last_backup_error)
                    self.assertEqual(list((root / "backups").glob("*.sqlite3")), [])
