"""Restore refuses a history database outside the supported schema range (#1189).

Startup refuses a database whose `PRAGMA user_version` is outside
`MIN_SUPPORTED_SCHEMA_VERSION..CURRENT_SCHEMA_VERSION` (#416). Restore used to
check only integrity and a few columns, so both bundle formats (single database
and segmented) and direct `HistoryStore.restore_backup()` callers installed a
newer-schema database, discarded the rollback copy and reported success; the
next start then refused the database. These tests pin the same gate on every
restore path, before anything is activated, and keep the supported range
(including the oldest supported version, which restore still migrates)
restorable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from app import __version__
from history_service.config import HistorySettings
from history_service.startup import HistorySchemaVersionError
from history_service.store import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    SCHEMA,
    HistoryStore,
)
from history_service.system_backup import (
    BACKUP_GROUP_METADATA,
    BUNDLE_FORMAT,
    HISTORY_DB_KEY,
    SystemBackupService,
)

requires_posix_migration_lock = unittest.skipUnless(
    importlib.util.find_spec("fcntl") is not None,
    "History migration locking requires POSIX flock support.",
)

FUTURE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION + 1


def _write_history_database(path: Path, marker: str, user_version: int) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA)
        connection.execute("CREATE TABLE restore_marker (value TEXT)")
        connection.execute("INSERT INTO restore_marker VALUES (?)", (marker,))
        connection.execute(f"PRAGMA user_version = {int(user_version)}")
        connection.commit()


def _read_marker_and_version(path: Path) -> tuple[str, int]:
    with closing(sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)) as connection:
        marker = connection.execute("SELECT value FROM restore_marker").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    return str(marker), int(version)


def _tree_digest(root: Path) -> dict[str, str]:
    """Every file under `root` by relative path and content hash."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_bundle(path: Path, history_bytes: bytes, *, segmented: bool) -> None:
    metadata = BACKUP_GROUP_METADATA[HISTORY_DB_KEY]
    archive_path = str(metadata["archive_root"])
    manifest: dict[str, object] = {
        "schema_version": 2 if segmented else 1,
        "format": BUNDLE_FORMAT,
        "app_version": __version__,
        "packaging": "zip",
        "groups": [
            {
                "key": HISTORY_DB_KEY,
                "archive_root": archive_path,
                "selected": True,
                "present": True,
                "restore_mode": metadata["restore_mode"],
            }
        ],
        "files": [
            {
                "key": HISTORY_DB_KEY,
                "group_key": HISTORY_DB_KEY,
                "archive_path": archive_path,
                "size_bytes": len(history_bytes),
                "sha256": hashlib.sha256(history_bytes).hexdigest(),
            }
        ],
    }
    if segmented:
        manifest["generation"] = {
            "generation_id": "candidate-generation",
            "complete": True,
            "baseline": True,
            "parent_generation_id": None,
            "min_reader_version": 2,
        }
        manifest["history_catalog"] = {
            "catalog_version": 1,
            "hot_member_key": HISTORY_DB_KEY,
            "segments": [],
            "tombstones": [],
        }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(archive_path, history_bytes)


@requires_posix_migration_lock
class BundleRestoreSchemaGateTests(unittest.TestCase):
    """Both bundle formats refuse an out-of-range history database in preflight."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="restore-schema-gate-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.state_root = self.temp_dir / "state"
        self.state_root.mkdir()
        self.history_path = self.state_root / "history.sqlite3"

    def _service(self, *, segmented: bool) -> SystemBackupService:
        _write_history_database(self.history_path, "prior", CURRENT_SCHEMA_VERSION)
        catalog_path: Path | None = None
        if segmented:
            catalog_path = self.state_root / "segments" / "catalog.json"
            catalog_path.parent.mkdir()
            catalog_path.write_text(
                json.dumps(
                    {
                        "catalog_version": 1,
                        "generation_id": "prior-generation",
                        "complete": True,
                        "segments": [],
                        "tombstones": [],
                    }
                ),
                encoding="utf-8",
            )
        store = HistoryStore(str(self.history_path), segment_catalog_path=catalog_path)
        settings = HistorySettings(
            sqlite_path=str(self.history_path),
            segment_catalog_path=str(catalog_path) if catalog_path else None,
            backup_dir=str(self.state_root / "history-backups"),
            startup_grace_seconds=0,
        )
        return SystemBackupService(settings, store)

    def _bundle(self, user_version: int, *, segmented: bool) -> Path:
        work = Path(tempfile.mkdtemp(prefix="bundle-", dir=self.temp_dir))
        candidate = work / "candidate.sqlite3"
        _write_history_database(candidate, "candidate", user_version)
        bundle = work / "bundle.zip"
        _write_bundle(bundle, candidate.read_bytes(), segmented=segmented)
        return bundle

    def _fresh_store(self, service: SystemBackupService) -> HistoryStore:
        return HistoryStore(
            str(self.history_path),
            segment_catalog_path=service.store.segment_catalog_path,
            recover_unreadable_database=False,
        )

    def _assert_refused_before_activation(self, *, segmented: bool) -> None:
        service = self._service(segmented=segmented)
        bundle = self._bundle(FUTURE_SCHEMA_VERSION, segmented=segmented)
        before = _tree_digest(self.state_root)

        with self.assertRaises(ValueError) as preflight_refusal:
            service.preflight_import_bundle_file(bundle)
        with self.assertRaises(ValueError) as import_refusal:
            service.import_bundle_from_file(bundle)

        for refusal in (preflight_refusal.exception, import_refusal.exception):
            message = str(refusal)
            self.assertIn(f"schema version {FUTURE_SCHEMA_VERSION}", message)
            self.assertIn(
                f"schema versions {MIN_SUPPORTED_SCHEMA_VERSION} to {CURRENT_SCHEMA_VERSION}",
                message,
            )
            self.assertIsInstance(refusal.__cause__, HistorySchemaVersionError)
        # Live history, catalog and any rollback material are byte-identical,
        # and nothing was left behind (no activation marker, no staged copy).
        self.assertEqual(_tree_digest(self.state_root), before)
        self.assertEqual(
            _read_marker_and_version(self._fresh_store(service).file_path),
            ("prior", CURRENT_SCHEMA_VERSION),
        )

    def _assert_restores(self, user_version: int, *, segmented: bool) -> None:
        service = self._service(segmented=segmented)
        bundle = self._bundle(user_version, segmented=segmented)
        digest = service.preflight_import_bundle_file(bundle)
        result = service.import_bundle_from_file(bundle, preflighted_archive_sha256=digest)
        self.assertTrue(result["ok"])
        # The next start admits the restored database and leaves it at the
        # version this build writes: an older supported schema is migrated.
        fresh = self._fresh_store(service)
        self.assertEqual(
            _read_marker_and_version(fresh.file_path),
            ("candidate", CURRENT_SCHEMA_VERSION),
        )

    def test_single_database_restore_refuses_a_newer_schema_before_activation(self) -> None:
        self._assert_refused_before_activation(segmented=False)

    def test_segmented_restore_refuses_a_newer_schema_before_activation(self) -> None:
        self._assert_refused_before_activation(segmented=True)

    def test_supported_schema_versions_still_restore_in_both_formats(self) -> None:
        for segmented in (False, True):
            for user_version in sorted({MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}):
                with self.subTest(segmented=segmented, user_version=user_version):
                    shutil.rmtree(self.state_root)
                    self.state_root.mkdir()
                    self._assert_restores(user_version, segmented=segmented)


@requires_posix_migration_lock
class DirectRestoreBackupSchemaGateTests(unittest.TestCase):
    """`HistoryStore.restore_backup()` applies the same gate to direct callers."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="restore-backup-schema-gate-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.live_root = self.temp_dir / "live"
        self.live_root.mkdir()
        self.live_path = self.live_root / "history.sqlite3"
        _write_history_database(self.live_path, "prior", CURRENT_SCHEMA_VERSION)
        self.store = HistoryStore(str(self.live_path))

    def _source(self, user_version: int) -> Path:
        source = Path(tempfile.mkdtemp(prefix="source-", dir=self.temp_dir)) / "source.sqlite3"
        _write_history_database(source, "candidate", user_version)
        return source

    def test_restore_backup_refuses_a_database_outside_the_supported_range(self) -> None:
        for user_version in (FUTURE_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION - 1):
            with self.subTest(user_version=user_version):
                source = self._source(user_version)
                before = _tree_digest(self.live_root)
                with self.assertRaises(HistorySchemaVersionError) as refusal:
                    self.store.restore_backup(source)
                self.assertIn(f"schema version {user_version}", refusal.exception.reason)
                self.assertIn(
                    f"schema versions {MIN_SUPPORTED_SCHEMA_VERSION} to {CURRENT_SCHEMA_VERSION}",
                    refusal.exception.reason,
                )
                self.assertEqual(_tree_digest(self.live_root), before)
                self.assertEqual(
                    _read_marker_and_version(self.live_path),
                    ("prior", CURRENT_SCHEMA_VERSION),
                )

    def test_restore_backup_still_restores_and_migrates_supported_versions(self) -> None:
        for user_version in sorted({MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}):
            with self.subTest(user_version=user_version):
                self.store.restore_backup(self._source(user_version))
                self.assertEqual(
                    _read_marker_and_version(self.live_path),
                    ("candidate", CURRENT_SCHEMA_VERSION),
                )


if __name__ == "__main__":
    unittest.main()
