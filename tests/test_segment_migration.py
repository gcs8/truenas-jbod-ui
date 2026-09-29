from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from history_service import segment_migration, segment_sealer
from history_service.store import CURRENT_SCHEMA_VERSION, MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA, HistoryStore


class SimulatedMigrationCrash(BaseException):
    pass


class SegmentedHistoryMigrationCliTests(unittest.TestCase):
    def test_future_schema_refused_before_artifacts_for_api_and_cli(self) -> None:
        for cli in (False, True):
            for apply in (False, True):
                with self.subTest(cli=cli, apply=apply), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "history.db"
                    segments = root / "segments"
                    self._create_source_database(source)
                    connection = sqlite3.connect(source)
                    try:
                        connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
                        connection.execute("PRAGMA journal_mode=WAL")
                    finally:
                        connection.close()
                    before = source.read_bytes()
                    if cli:
                        result = subprocess.run(
                            [sys.executable, "scripts/migrate_segmented_history.py",
                             "--source", str(source), "--segments-dir", str(segments),
                             "--cutoff", "2025-01-02T00:00:00+00:00", "--key-id", "test-key-1",
                             *(["--apply"] if apply else [])],
                            capture_output=True, text=True, timeout=30,
                            cwd=Path(__file__).resolve().parents[1],
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("schema version", result.stderr)
                    else:
                        with self.assertRaisesRegex(ValueError, "schema version"):
                            segment_migration.migrate_segmented_history(
                                source=source, segments_directory=segments,
                                cutoff="2025-01-02T00:00:00+00:00", key_id="test-key-1", apply=apply,
                            )
                    self.assertEqual(source.read_bytes(), before)
                    self.assertEqual(sorted(p.name for p in root.iterdir()), ["history.db"])

    def test_supported_schema_versions_keep_dry_run_and_apply_behavior(self) -> None:
        for version in range(MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION + 1):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "history.db"
                segments = root / "segments"
                self._create_source_database(source)
                connection = sqlite3.connect(source)
                try:
                    connection.execute(f"PRAGMA user_version={version}")
                finally:
                    connection.close()
                before = source.read_bytes()
                self.assertFalse(segment_migration.migrate_segmented_history(
                    source=source, segments_directory=segments,
                    cutoff="2025-01-02T00:00:00+00:00", key_id="test-key-1",
                )["apply"])
                self.assertEqual(source.read_bytes(), before)
                self.assertFalse(segments.exists())
                self.assertTrue(segment_migration.migrate_segmented_history(
                    source=source, segments_directory=segments,
                    cutoff="2025-01-02T00:00:00+00:00", key_id="test-key-1", apply=True,
                )["apply"])
                segment_migration.rollback_segmented_history(
                    source=source, segments_directory=segments, apply=True,
                )
                self.assertEqual(source.read_bytes(), before)

    def _rollback_fixture(self, root: Path, *, recovery: bool = False) -> tuple[Path, Path, bytes]:
        source = root / "history.db"
        segments = root / "segments"
        self._create_source_database(source)
        original = source.read_bytes()
        segment_migration.migrate_segmented_history(
            source=source, segments_directory=segments,
            cutoff="2025-01-02T00:00:00+00:00", key_id="test-key-1", apply=True,
        )
        if recovery:
            # An old prepared rollback marker, with its catalog still intact.
            rollback = segments / ".v1-rollback.sqlite3"
            digest = segment_migration._sha256_file(rollback)
            segment_migration._write_pending_marker(
                segments / ".migration-pending.json", rollback_sha256=digest,
                operation="rollback", source_sha256=segment_migration._sha256_file(source),
            )
            segment_migration._restore_v1_source(source, rollback, 0o600, digest)
        return source, segments, original

    def _finish_rollback_repeatedly(self, source: Path, segments: Path, original: bytes) -> None:
        self.assertEqual(source.read_bytes(), original)
        if (segments / ".migration-pending.json").exists():
            for _ in range(2):
                self.assertFalse(segment_migration.recover_pending_migration(
                    source=source, segments_directory=segments,
                )["apply"])
            segment_migration.recover_pending_migration(
                source=source, segments_directory=segments, apply=True,
            )
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((segments / "catalog.json").exists())
        self.assertFalse((segments / "segment-0001.sqlite3").exists())
        self.assertFalse((segments / ".migration-pending.json").exists())
        # Existing API retires the redundant snapshot on the next explicit call.
        segment_migration.recover_pending_migration(
            source=source, segments_directory=segments, apply=True,
        )
        with self.assertRaisesRegex(ValueError, "not pending"):
            segment_migration.recover_pending_migration(
                source=source, segments_directory=segments, apply=True,
            )
        self.assertEqual(source.read_bytes(), original)

    def test_rollback_cleanup_recovers_after_actual_unlinks(self) -> None:
        for recovery in (False, True):
            for target in ("catalog.json", "segment-0001.sqlite3"):
                with self.subTest(recovery=recovery, target=target), tempfile.TemporaryDirectory() as temporary:
                    source, segments, original = self._rollback_fixture(Path(temporary), recovery=recovery)
                    real_unlink = os.unlink

                    def unlink_then_interrupt(path, *args, **kwargs):
                        real_unlink(path, *args, **kwargs)
                        if Path(path).name == target:
                            raise SimulatedMigrationCrash(target)

                    action = (segment_migration.recover_pending_migration if recovery
                              else segment_migration.rollback_segmented_history)
                    with patch.object(os, "unlink", side_effect=unlink_then_interrupt):
                        with self.assertRaises(SimulatedMigrationCrash):
                            action(source=source, segments_directory=segments, apply=True)
                    self.assertFalse((segments / target).exists())
                    self._finish_rollback_repeatedly(source, segments, original)

    def test_rollback_cleanup_recovers_after_bounded_process_exit(self) -> None:
        script = '''
import os, sys
from pathlib import Path
from history_service import segment_migration as migration
source, segments, recovery, target = sys.argv[1:]
real_unlink = os.unlink
def unlink(path, *args, **kwargs):
    real_unlink(path, *args, **kwargs)
    if Path(path).name == target:
        os._exit(73)
os.unlink = unlink
action = migration.recover_pending_migration if recovery == "1" else migration.rollback_segmented_history
action(source=Path(source), segments_directory=Path(segments), apply=True)
'''
        for recovery in (False, True):
            for target in ("catalog.json", "segment-0001.sqlite3"):
                with self.subTest(recovery=recovery, target=target), tempfile.TemporaryDirectory() as temporary:
                    source, segments, original = self._rollback_fixture(Path(temporary), recovery=recovery)
                    result = subprocess.run(
                        [sys.executable, "-c", script, str(source), str(segments), str(int(recovery)), target],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 73, result.stderr)
                    self.assertFalse((segments / target).exists())
                    self._finish_rollback_repeatedly(source, segments, original)

    def test_rollback_cleanup_unknown_segment_keeps_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, segments, original = self._rollback_fixture(Path(temporary))
            real_unlink = os.unlink

            def stop_after_catalog(path, *args, **kwargs):
                real_unlink(path, *args, **kwargs)
                if Path(path).name == "catalog.json":
                    raise SimulatedMigrationCrash()

            with patch.object(os, "unlink", side_effect=stop_after_catalog):
                with self.assertRaises(SimulatedMigrationCrash):
                    segment_migration.rollback_segmented_history(
                        source=source, segments_directory=segments, apply=True,
                    )
            unknown = segments / "segment-unknown.sqlite3"
            unknown.write_bytes(b"unowned")
            before = {p.name: p.read_bytes() for p in segments.iterdir()}
            for apply in (False, True, True):
                with self.assertRaisesRegex(ValueError, "unauthenticated"):
                    segment_migration.recover_pending_migration(
                        source=source, segments_directory=segments, apply=apply,
                    )
                self.assertEqual({p.name: p.read_bytes() for p in segments.iterdir()}, before)
                self.assertEqual(source.read_bytes(), original)
            unknown.unlink()
            self._finish_rollback_repeatedly(source, segments, original)

    def test_terminal_rollback_refuses_changed_or_unowned_evidence(self) -> None:
        cases = ("catalog", "segment", "source", "unknown", "symlink", "hardlink",
                 "path", "duplicate", "size-bool", "digest", "missing-inventory", "source-digest")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, segments, _ = self._rollback_fixture(root)
                real_unlink = os.unlink

                def stop_before_catalog(path, *args, **kwargs):
                    if Path(path).name == "catalog.json":
                        raise SimulatedMigrationCrash()
                    real_unlink(path, *args, **kwargs)

                with patch.object(os, "unlink", side_effect=stop_before_catalog):
                    with self.assertRaises(SimulatedMigrationCrash):
                        segment_migration.rollback_segmented_history(
                            source=source, segments_directory=segments, apply=True,
                        )
                pending = segments / ".migration-pending.json"
                segment = segments / "segment-0001.sqlite3"
                if case in {"catalog", "segment", "source"}:
                    target = {"catalog": segments / "catalog.json", "segment": segment, "source": source}[case]
                    with target.open("r+b") as stream:
                        stream.seek(128)
                        stream.write(b"altered")
                elif case == "unknown":
                    (segments / "unowned.txt").write_bytes(b"keep")
                elif case in {"symlink", "hardlink"}:
                    other = root / "other.sqlite3"
                    other.write_bytes(segment.read_bytes())
                    segment.unlink()
                    if case == "symlink":
                        segment.symlink_to(other)
                    else:
                        os.link(other, segment)
                else:
                    marker = json.loads(pending.read_bytes())
                    inventory = marker["rollback_cleanup"]
                    if case == "path":
                        inventory[1]["file_name"] = "../history.db"
                    elif case == "duplicate":
                        inventory.append(dict(inventory[1]))
                    elif case == "size-bool":
                        inventory[1]["size_bytes"] = True
                    elif case == "digest":
                        inventory[1]["sha256"] = "0" * 64
                    elif case == "missing-inventory":
                        del marker["rollback_cleanup"]
                    elif case == "source-digest":
                        marker["source_sha256"] = "0" * 64
                    pending.write_text(json.dumps(marker), encoding="utf-8")
                before_source = source.read_bytes()
                before = {p.name: (p.is_symlink(), p.read_bytes()) for p in segments.iterdir()}
                for apply in (False, True, True):
                    with self.assertRaises((ValueError, OSError)):
                        segment_migration.recover_pending_migration(
                            source=source, segments_directory=segments, apply=apply,
                        )
                    self.assertEqual(source.read_bytes(), before_source)
                    self.assertEqual({p.name: (p.is_symlink(), p.read_bytes()) for p in segments.iterdir()}, before)

    def test_repeated_rollback_recovery_interruptions_preserve_terminal_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, segments, original = self._rollback_fixture(Path(temporary))
            real_unlink = os.unlink
            marker_bytes = None
            for target in ("catalog.json", "segment-0001.sqlite3", ".migration-pending.json"):
                def stop_after_unlink(path, *args, **kwargs):
                    real_unlink(path, *args, **kwargs)
                    if Path(path).name == target:
                        raise SimulatedMigrationCrash()

                action = (segment_migration.rollback_segmented_history if marker_bytes is None
                          else segment_migration.recover_pending_migration)
                with patch.object(os, "unlink", side_effect=stop_after_unlink):
                    with self.assertRaises(SimulatedMigrationCrash):
                        action(source=source, segments_directory=segments, apply=True)
                self.assertEqual(source.read_bytes(), original)
                pending = segments / ".migration-pending.json"
                if pending.exists():
                    if marker_bytes is not None:
                        self.assertEqual(pending.read_bytes(), marker_bytes)
                    marker_bytes = pending.read_bytes()
            self._finish_rollback_repeatedly(source, segments, original)

    def test_rollback_persists_terminal_inventory_before_first_cleanup(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory() as temporary:
                source, segments, original = self._rollback_fixture(Path(temporary), recovery=recovery)
                pending = segments / ".migration-pending.json"
                synced: list[tuple[tuple[int, int], bytes | None]] = []
                real_fsync, real_unlink = os.fsync, os.unlink
                checked = False

                def fsync(descriptor):
                    real_fsync(descriptor)
                    metadata = os.fstat(descriptor)
                    synced.append(((metadata.st_dev, metadata.st_ino), pending.read_bytes() if pending.exists() else None))

                def unlink(path, *args, **kwargs):
                    nonlocal checked
                    if Path(path).name == "catalog.json":
                        marker = json.loads(pending.read_bytes())
                        self.assertEqual(marker["phase"], "rollback-cleanup")
                        self.assertEqual(marker["source_sha256"], segment_migration._sha256_file(source))
                        self.assertEqual(source.read_bytes(), original)
                        inventory = marker["rollback_cleanup"]
                        self.assertEqual({entry["file_name"] for entry in inventory},
                                         {"catalog.json", "segment-0001.sqlite3"})
                        for entry in inventory:
                            artifact = segments / entry["file_name"]
                            self.assertEqual(entry["size_bytes"], artifact.stat().st_size)
                            self.assertEqual(entry["sha256"], segment_migration._sha256_file(artifact))
                        directory_identity = (segments.stat().st_dev, segments.stat().st_ino)
                        marker_identity = (pending.stat().st_dev, pending.stat().st_ino)
                        self.assertIn((directory_identity, pending.read_bytes()), synced)
                        self.assertIn(marker_identity, [identity for identity, _ in synced])
                        checked = True
                    real_unlink(path, *args, **kwargs)

                action = (segment_migration.recover_pending_migration if recovery
                          else segment_migration.rollback_segmented_history)
                with patch.object(os, "fsync", side_effect=fsync), patch.object(os, "unlink", side_effect=unlink):
                    action(source=source, segments_directory=segments, apply=True)
                self.assertTrue(checked)
                self._finish_rollback_repeatedly(source, segments, original)

    def test_rollback_terminal_fsync_failures_are_recoverable(self) -> None:
        for recovery in (False, True):
            action = (segment_migration.recover_pending_migration if recovery
                      else segment_migration.rollback_segmented_history)
            # Discover the actual fsync ordinals after source restoration, then fail
            # each syscall in a fresh fixture. No fabricated high-level checkpoint.
            total = 0
            for fail_at in [None]:
                with tempfile.TemporaryDirectory() as temporary:
                    source, segments, _ = self._rollback_fixture(Path(temporary), recovery=recovery)
                    real_fsync = os.fsync
                    armed = recovery
                    real_restore = segment_migration._restore_v1_source

                    def restore(*args):
                        nonlocal armed
                        real_restore(*args)
                        armed = True

                    def count_fsync(fd):
                        nonlocal total
                        if armed:
                            total += 1
                        real_fsync(fd)

                    with (patch.object(os, "fsync", side_effect=count_fsync),
                          patch.object(segment_migration, "_restore_v1_source", side_effect=restore)):
                        action(source=source, segments_directory=segments, apply=True)
            self.assertGreater(total, 0)
            for fail_at in range(1, total + 1):
                with self.subTest(recovery=recovery, fsync=fail_at), tempfile.TemporaryDirectory() as temporary:
                    source, segments, original = self._rollback_fixture(Path(temporary), recovery=recovery)
                    real_fsync = os.fsync
                    armed = recovery
                    calls = 0
                    real_restore = segment_migration._restore_v1_source

                    def restore(*args):
                        nonlocal armed
                        real_restore(*args)
                        armed = True

                    def fail_fsync(fd):
                        nonlocal calls
                        if armed:
                            calls += 1
                            if calls == fail_at:
                                raise OSError("injected terminal fsync")
                        real_fsync(fd)

                    with (patch.object(os, "fsync", side_effect=fail_fsync),
                          patch.object(segment_migration, "_restore_v1_source", side_effect=restore)):
                        with self.assertRaisesRegex(OSError, "injected terminal fsync"):
                            action(source=source, segments_directory=segments, apply=True)
                    self._finish_rollback_repeatedly(source, segments, original)

    def test_dry_run_refuses_legacy_schema_without_creating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_legacy_source_database(source)
            original_bytes = source.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "initialize it once through the current history service",
            ):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertFalse(segments_directory.exists())

    def test_apply_refuses_legacy_schema_without_creating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_legacy_source_database(source)
            original_bytes = source.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "initialize it once through the current history service",
            ):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertFalse(segments_directory.exists())

    def test_dry_run_current_schema_creates_no_sqlite_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)

            receipt = segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
            )

            self.assertFalse(receipt["apply"])
            self.assertFalse(segments_directory.exists())
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(f"{source}{suffix}").exists())

    def test_migration_partitions_mixed_offset_rows_exactly_once_by_absolute_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            with sqlite3.connect(source) as connection:
                connection.executescript(SCHEMA)
                for event_type, observed_at in (
                    ("chronologically-old", "2025-01-02T01:00:00+14:00"),
                    ("chronologically-new", "2025-01-01T20:00:00-12:00"),
                ):
                    connection.execute(
                        """
                        INSERT INTO slot_events (
                            observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (observed_at, "system-1", "enclosure-1", 1, "slot-1", event_type, "{}"),
                    )

            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )

            with sqlite3.connect(segments_directory / "segment-0001.sqlite3") as connection:
                segment_events = connection.execute("SELECT event_type FROM slot_events").fetchall()
            with sqlite3.connect(source) as connection:
                hot_events = connection.execute("SELECT event_type FROM slot_events").fetchall()
            self.assertEqual(segment_events, [("chronologically-old",)])
            self.assertEqual(hot_events, [("chronologically-new",)])

    def test_apply_publishes_group_readable_segment_catalog_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source.chmod(0o660)
            source_owner = (source.stat().st_uid, source.stat().st_gid)

            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )

            self.assertEqual(stat.S_IMODE(segments_directory.stat().st_mode), 0o750)
            self.assertEqual(
                (segments_directory.stat().st_uid, segments_directory.stat().st_gid),
                source_owner,
            )
            for name in ("segment-0001.sqlite3", "catalog.json"):
                with self.subTest(name=name):
                    metadata = (segments_directory / name).stat()
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o640)
                    self.assertEqual((metadata.st_uid, metadata.st_gid), source_owner)

    def test_apply_authenticates_segment_in_marker_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            pending_path = segments_directory / ".migration-pending.json"
            self._create_source_database(source)
            original_link = segment_sealer.os.link
            publication_checked = False

            def inspect_marker_before_link(source_path: Path, destination_path: Path) -> None:
                nonlocal publication_checked
                if destination_path.name == "segment-0001.sqlite3":
                    marker = json.loads(pending_path.read_text(encoding="utf-8"))
                    publication = marker["segment_publication"]
                    self.assertEqual(publication["file_name"], destination_path.name)
                    self.assertEqual(publication["size_bytes"], source_path.stat().st_size)
                    self.assertEqual(
                        publication["sha256"],
                        segment_migration._sha256_file(source_path),
                    )
                    publication_checked = True
                original_link(source_path, destination_path)

            with patch.object(segment_sealer.os, "link", side_effect=inspect_marker_before_link):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            self.assertTrue(publication_checked)

    def test_recovery_removes_only_the_authenticated_published_orphan_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            original_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            catalog_path = segments_directory / "catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            segment_entry = catalog["segments"][0]
            segment_path = segments_directory / segment_entry["file_name"]
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            rollback_sha256 = segment_migration._sha256_file(rollback_path)
            pending_path = segments_directory / ".migration-pending.json"
            segment_migration._write_pending_marker(
                pending_path,
                rollback_sha256=rollback_sha256,
                source_sha256=rollback_sha256,
                phase="hot-ready",
                replacement_sha256=segment_migration._sha256_file(source),
            )
            marker = json.loads(pending_path.read_text(encoding="utf-8"))
            marker["segment_publication"] = {
                "file_name": segment_entry["file_name"],
                "sha256": segment_entry["sha256"],
                "size_bytes": segment_entry["size_bytes"],
            }
            pending_path.write_text(json.dumps(marker), encoding="utf-8")
            catalog_path.unlink()
            temporary_segment_path = segments_directory / ".segment-crash.sqlite3"
            os.link(segment_path, temporary_segment_path)

            receipt = segment_migration.recover_pending_migration(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertFalse(segment_path.exists())
            self.assertFalse(temporary_segment_path.exists())
            self.assertFalse(pending_path.exists())
            self.assertEqual(receipt["removed_orphaned_segment_paths"], [str(segment_path)])

    def test_rollback_catalog_metadata_comes_from_the_catalog_that_was_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            catalog_path = segments_directory / "catalog.json"
            replacement_path = segments_directory / "replacement-catalog.json"
            replacement_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            replacement_catalog["generation_id"] = "generation-swapped"
            replacement_path.write_text(json.dumps(replacement_catalog), encoding="utf-8")
            original_verify = segment_migration.SegmentedHistoryReader.verify_catalog_segments

            def verify_then_swap(reader: segment_migration.SegmentedHistoryReader) -> tuple[Path, ...]:
                paths = original_verify(reader)
                replacement_path.replace(catalog_path)
                return paths

            with patch.object(
                segment_migration.SegmentedHistoryReader,
                "verify_catalog_segments",
                autospec=True,
                side_effect=verify_then_swap,
            ):
                catalog, _ = segment_migration._load_rollback_catalog(catalog_path, source)

            self.assertEqual(catalog["generation_id"], "generation-0001")

    def test_apply_refuses_rollback_snapshot_symlink_race_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            victim = root / "victim"
            self._create_source_database(source)
            victim.write_bytes(b"must-not-change")
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            original_path_entry_exists = segment_migration.path_entry_exists
            injected = False

            def inject_symlink_after_preflight(path: Path) -> bool:
                nonlocal injected
                if path == rollback_path and not injected:
                    path.symlink_to(victim)
                    injected = True
                    return False
                return original_path_entry_exists(path)

            with patch.object(segment_migration, "path_entry_exists", side_effect=inject_symlink_after_preflight):
                with self.assertRaisesRegex(ValueError, "rollback snapshot"):
                    segment_migration.migrate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-02T00:00:00+00:00",
                        key_id="test-key-1",
                        apply=True,
                    )

            self.assertEqual(victim.read_bytes(), b"must-not-change")

    def test_recover_pending_forward_migration_refuses_divergent_live_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            expected_live_sha256 = segment_migration._sha256_file(source)
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            pending_path = segments_directory / ".migration-pending.json"
            catalog_path = segments_directory / "catalog.json"
            segment_entry = json.loads(catalog_path.read_text(encoding="utf-8"))["segments"][0]
            segment_publication = {
                key: segment_entry[key]
                for key in ("file_name", "sha256", "size_bytes")
            }
            catalog_path.unlink()
            segment_migration._write_pending_marker(
                pending_path,
                rollback_sha256=segment_migration._sha256_file(rollback_path),
                operation="forward",
                source_sha256=expected_live_sha256,
                segment_publication=segment_publication,
            )
            with sqlite3.connect(source) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        observed_at, system_id, system_label, enclosure_key,
                        enclosure_id, enclosure_label, slot, slot_label,
                        event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2025-01-04T00:00:00+00:00",
                        "archive-core",
                        "Archive CORE",
                        "enc-a",
                        "enc-a",
                        "Front Shelf",
                        5,
                        "5",
                        "post-crash-write",
                        "{}",
                    ),
                )
                connection.commit()
            divergent_bytes = source.read_bytes()

            with self.assertRaisesRegex(ValueError, "divergent"):
                segment_migration.recover_pending_migration(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(source.read_bytes(), divergent_bytes)
            with sqlite3.connect(source) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM slot_events WHERE event_type = 'post-crash-write'"
                    ).fetchone()[0],
                    1,
                )

    def test_recover_pending_rollback_finishes_after_source_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            original_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            hot_sha256 = segment_migration._sha256_file(source)
            rollback_sha256 = segment_migration._sha256_file(rollback_path)
            segment_migration._write_pending_marker(
                segments_directory / ".migration-pending.json",
                rollback_sha256=rollback_sha256,
                operation="rollback",
                source_sha256=hot_sha256,
            )
            segment_migration._restore_v1_source(source, rollback_path, 0o600, rollback_sha256)

            receipt = segment_migration.recover_pending_migration(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(receipt["recovery_state"], "rollback-finalized")
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())
            self.assertFalse((segments_directory / ".migration-pending.json").exists())

    def test_recover_pending_migration_finalizes_a_valid_published_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            pending_path = segments_directory / ".migration-pending.json"
            rollback_sha256 = segment_migration._sha256_file(rollback_path)
            segment_migration._write_pending_marker(
                pending_path,
                rollback_sha256=rollback_sha256,
                source_sha256=rollback_sha256,
                phase="hot-ready",
                replacement_sha256=segment_migration._sha256_file(source),
            )

            receipt = segment_migration.recover_pending_migration(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(receipt["recovery_state"], "published-catalog-finalized")
            self.assertFalse(pending_path.exists())
            self.assertEqual(
                len(
                    segment_migration.SegmentedHistoryReader.from_catalog(
                        hot_path=source,
                        catalog_path=segments_directory / "catalog.json",
                    ).segment_paths
                ),
                1,
            )

    def test_rollback_refuses_symlinked_segment_directory_before_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            backing_directory = root / "backing"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=backing_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            hot_bytes = source.read_bytes()
            segments_directory.symlink_to(backing_directory, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "output directory"):
                segment_migration.rollback_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(source.read_bytes(), hot_bytes)
            self.assertTrue((backing_directory / "catalog.json").is_file())
            self.assertFalse((backing_directory / ".migration-pending.json").exists())

    def test_rollback_refuses_snapshot_swapped_after_initial_digest_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            hot_bytes = source.read_bytes()
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            original_write_marker = segment_migration._write_pending_marker

            def write_marker_then_swap(
                path: Path,
                *,
                rollback_sha256: str,
                operation: str,
                source_sha256: str | None = None,
            ) -> None:
                original_write_marker(
                    path,
                    rollback_sha256=rollback_sha256,
                    operation=operation,
                    source_sha256=source_sha256,
                )
                rollback_path.write_bytes(b"tampered")

            with patch.object(segment_migration, "_write_pending_marker", side_effect=write_marker_then_swap):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    segment_migration.rollback_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        apply=True,
                    )

            self.assertEqual(source.read_bytes(), hot_bytes)

    def test_apply_refuses_symlinked_segment_directory_before_writing_recovery_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            target_directory = root / "target"
            segments_directory = root / "segments"
            self._create_source_database(source)
            target_directory.mkdir()
            segments_directory.symlink_to(target_directory, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "output directory"):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            self.assertFalse((target_directory / ".v1-rollback.sqlite3").exists())
            self.assertFalse((target_directory / ".migration-pending.json").exists())

    def test_rollback_rejects_history_store_writes_before_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            writer = HistoryStore(str(source), recover_unreadable_database=False)
            original_restore = segment_migration._restore_v1_source
            write_committed = False

            def restore_after_concurrent_write(
                source_path: Path,
                rollback_path: Path,
                source_mode: int,
                expected_sha256: str,
            ) -> None:
                nonlocal write_committed
                try:
                    writer._execute_write(
                        lambda connection: connection.execute(
                            """
                            INSERT INTO slot_events (
                                id, observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (99, "2025-01-03T12:00:00+00:00", "system-1", "enclosure-1", 1, "slot-1", "race", "{}"),
                        )
                    )
                    write_committed = True
                except sqlite3.OperationalError as exc:
                    self.assertIn("migration", str(exc).lower())
                original_restore(source_path, rollback_path, source_mode, expected_sha256)

            with patch.object(segment_migration, "_restore_v1_source", side_effect=restore_after_concurrent_write):
                segment_migration.rollback_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertFalse(write_committed)

    def test_migration_does_not_lose_a_pre_cutoff_write_between_segment_and_hot_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            original_stage_hot = segment_migration._stage_hot_replacement
            writer = HistoryStore(str(source), recover_unreadable_database=False)
            concurrent_write_committed = False

            def stage_after_concurrent_write(source_path: Path, cutoff: str) -> Path:
                nonlocal concurrent_write_committed
                try:
                    writer._execute_write(
                        lambda connection: connection.execute(
                        """
                        INSERT INTO slot_events (
                            id, observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (99, "2025-01-01T12:00:00+00:00", "system-1", "enclosure-1", 1, "slot-1", "race", "{}"),
                        )
                    )
                    concurrent_write_committed = True
                except sqlite3.OperationalError as exc:
                    self.assertIn("migration", str(exc).lower())
                return original_stage_hot(source_path, cutoff)

            with patch.object(segment_migration, "_stage_hot_replacement", side_effect=stage_after_concurrent_write):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            copies = 0
            for database_path in (source, segments_directory / "segment-0001.sqlite3"):
                with self.subTest(database=database_path.name):
                    with sqlite3.connect(database_path) as connection:
                        copies += int(connection.execute("SELECT COUNT(*) FROM slot_events WHERE id = 99").fetchone()[0])
            self.assertFalse(concurrent_write_committed and copies == 0)

    def test_recover_orphan_snapshot_removes_it_only_when_source_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            self._create_source_database(source)
            segments_directory.mkdir()
            rollback_path.write_bytes(source.read_bytes())
            rollback_path.chmod(0o600)

            receipt = segment_migration.recover_pending_migration(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(receipt["recovery_state"], "orphan-snapshot-removed")
            self.assertFalse(rollback_path.exists())
            self.assertFalse((segments_directory / ".migration-pending.json").exists())

    def test_recovery_reports_segment_temp_left_by_process_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            original_copy_and_prune = segment_sealer._copy_and_prune
            original_unlink = Path.unlink
            orphan_path: Path | None = None

            def copy_then_kill(copy_source: Path, destination: Path, cutoff: str) -> dict[str, int]:
                nonlocal orphan_path
                original_copy_and_prune(copy_source, destination, cutoff)
                orphan_path = destination
                raise SimulatedMigrationCrash("segment-copy")

            def preserve_process_kill_temp(path: Path, *args, **kwargs) -> None:
                if path == orphan_path:
                    return
                original_unlink(path, *args, **kwargs)

            with (
                patch.object(segment_sealer, "_copy_and_prune", side_effect=copy_then_kill),
                patch.object(Path, "unlink", preserve_process_kill_temp),
            ):
                with self.assertRaises(SimulatedMigrationCrash):
                    segment_migration.migrate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-02T00:00:00+00:00",
                        key_id="test-key-1",
                        apply=True,
                    )

            self.assertIsNotNone(orphan_path)
            assert orphan_path is not None
            self.assertTrue(orphan_path.is_file())
            orphan_bytes = orphan_path.read_bytes()
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            self.assertTrue(rollback_path.is_file())

            with self.assertRaises(ValueError) as raised:
                segment_migration.recover_pending_migration(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(
                str(raised.exception),
                "Segmented history recovery found unreferenced segment temporary artifact: "
                + json.dumps(str(orphan_path))
                + ".",
            )
            self.assertEqual(orphan_path.read_bytes(), orphan_bytes)
            self.assertTrue(rollback_path.is_file())

    def test_recovery_json_escapes_hostile_segment_temp_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            orphan_path = segments_directory / ".segment-hostile\nname\x1b[31m.sqlite3"
            orphan_bytes = b"unauthenticated-segment"
            self._create_source_database(source)
            segments_directory.mkdir()
            rollback_path.write_bytes(source.read_bytes())
            rollback_path.chmod(0o600)
            orphan_path.write_bytes(orphan_bytes)

            with self.assertRaises(ValueError) as raised:
                segment_migration.recover_pending_migration(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            message = str(raised.exception)
            self.assertEqual(
                message,
                "Segmented history recovery found unreferenced segment temporary artifact: "
                + json.dumps(str(orphan_path))
                + ".",
            )
            self.assertNotIn("\n", message)
            self.assertNotIn("\x1b", message)
            self.assertEqual(orphan_path.read_bytes(), orphan_bytes)
            self.assertTrue(rollback_path.is_file())

    def test_cli_recover_rollback_restores_a_pre_catalog_pending_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            rollback_sha256 = segment_migration._sha256_file(rollback_path)
            catalog_path = segments_directory / "catalog.json"
            segment_entry = json.loads(catalog_path.read_text(encoding="utf-8"))["segments"][0]
            segment_migration._write_pending_marker(
                segments_directory / ".migration-pending.json",
                rollback_sha256=rollback_sha256,
                source_sha256=rollback_sha256,
                phase="hot-ready",
                replacement_sha256=segment_migration._sha256_file(source),
                segment_publication={
                    key: segment_entry[key]
                    for key in ("file_name", "sha256", "size_bytes")
                },
            )
            catalog_path.unlink()

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate_segmented_history.py",
                    "--source",
                    str(source),
                    "--segments-dir",
                    str(segments_directory),
                    "--recover-rollback",
                    "--apply",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["apply"])
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse((segments_directory / ".migration-pending.json").exists())

    def test_recover_pending_migration_restores_v1_and_reports_unreferenced_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            rollback_path = segments_directory / ".v1-rollback.sqlite3"
            rollback_sha256 = segment_migration._sha256_file(rollback_path)
            catalog_path = segments_directory / "catalog.json"
            segment_entry = json.loads(catalog_path.read_text(encoding="utf-8"))["segments"][0]
            segment_migration._write_pending_marker(
                segments_directory / ".migration-pending.json",
                rollback_sha256=rollback_sha256,
                source_sha256=rollback_sha256,
                phase="hot-ready",
                replacement_sha256=segment_migration._sha256_file(source),
                segment_publication={
                    key: segment_entry[key]
                    for key in ("file_name", "sha256", "size_bytes")
                },
            )
            catalog_path.unlink()

            receipt = segment_migration.recover_pending_migration(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse((segments_directory / ".migration-pending.json").exists())
            self.assertEqual(receipt["orphaned_segment_paths"], [str(segments_directory / "segment-0001.sqlite3")])
            self.assertEqual(
                receipt["removed_orphaned_segment_paths"],
                [str(segments_directory / "segment-0001.sqlite3")],
            )
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())

    def test_restore_preserves_the_root_copy_failure_and_source_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            rollback = root / "rollback.sqlite3"
            source.write_bytes(b"current-history")
            rollback.write_bytes(b"rollback-history")

            with patch.object(segment_migration.shutil, "copyfileobj", side_effect=OSError("copy injected")):
                with self.assertRaisesRegex(OSError, "copy injected"):
                    segment_migration._restore_v1_source(
                        source,
                        rollback,
                        0o600,
                        segment_migration._sha256_file(rollback),
                    )

            self.assertEqual(source.read_bytes(), b"current-history")
            self.assertEqual(list(root.glob(".history.db.rollback-*.sqlite3")), [])

    def test_rollback_refuses_a_cataloged_segment_with_a_tampered_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )
            hot_bytes = source.read_bytes()
            segment_path = segments_directory / "segment-0001.sqlite3"
            with segment_path.open("r+b") as stream:
                stream.seek(128)
                original = stream.read(1)
                stream.seek(128)
                stream.write(bytes((original[0] ^ 0x01,)))

            with self.assertRaisesRegex(ValueError, "integrity"):
                segment_migration.rollback_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(source.read_bytes(), hot_bytes)
            self.assertTrue((segments_directory / "catalog.json").is_file())
            self.assertTrue(segment_path.is_file())
            self.assertFalse((segments_directory / ".migration-pending.json").exists())

    def test_apply_refuses_a_dangling_pending_recovery_marker_before_creating_rollback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segments_directory.mkdir()
            (segments_directory / ".migration-pending.json").symlink_to(root / "missing-recovery-receipt")

            with self.assertRaisesRegex(ValueError, "recovery"):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            self.assertFalse((segments_directory / ".v1-rollback.sqlite3").exists())
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())

    def test_cli_rollback_restores_v1_source_after_a_successful_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate_segmented_history.py",
                    "--source",
                    str(source),
                    "--segments-dir",
                    str(segments_directory),
                    "--rollback",
                    "--apply",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["apply"])
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse((segments_directory / "catalog.json").exists())

    def test_rollback_restores_byte_identical_v1_source_and_removes_cataloged_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source_bytes = source.read_bytes()
            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T00:00:00+00:00",
                key_id="test-key-1",
                apply=True,
            )

            receipt = segment_migration.rollback_segmented_history(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertTrue(receipt["apply"])
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())
            self.assertFalse((segments_directory / ".migration-pending.json").exists())
            self.assertTrue((segments_directory / ".v1-rollback.sqlite3").is_file())

    def test_apply_refuses_existing_pending_recovery_before_creating_rollback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            segments_directory.mkdir()
            (segments_directory / ".migration-pending.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "recovery"):
                segment_migration.migrate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-02T00:00:00+00:00",
                    key_id="test-key-1",
                    apply=True,
                )

            self.assertFalse((segments_directory / ".v1-rollback.sqlite3").exists())
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())

    def test_apply_marks_recovery_pending_until_catalog_publication_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            pending_path = segments_directory / ".migration-pending.json"
            self._create_source_database(source)
            source_bytes = source.read_bytes()

            def fail_catalog_write(
                path: Path,
                payload: dict[str, object],
                *,
                owner: tuple[int, int],
            ) -> None:
                self.assertEqual(path, segments_directory / "catalog.json")
                self.assertEqual(owner, (source.stat().st_uid, source.stat().st_gid))
                self.assertTrue(pending_path.is_file())
                raise OSError("injected catalog failure")

            with patch.object(segment_migration, "_write_catalog", side_effect=fail_catalog_write):
                with self.assertRaisesRegex(OSError, "injected catalog failure"):
                    segment_migration.migrate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-02T00:00:00+00:00",
                        key_id="test-key-1",
                        apply=True,
                    )

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse(pending_path.exists())
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())

    def test_apply_rejects_sqlite_sidecar_before_creating_rollback_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            (root / "history.db-wal").write_bytes(b"unexpected-sidecar")

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate_segmented_history.py",
                    "--source",
                    str(source),
                    "--segments-dir",
                    str(segments_directory),
                    "--cutoff",
                    "2025-01-02T00:00:00+00:00",
                    "--key-id",
                    "test-key-1",
                    "--apply",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sidecar", result.stderr)
            self.assertFalse((segments_directory / ".v1-rollback.sqlite3").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())
            self.assertFalse((segments_directory / "catalog.json").exists())

    def test_apply_restores_v1_source_when_catalog_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)
            source_bytes = source.read_bytes()

            with patch.object(segment_migration, "_write_catalog", side_effect=OSError("injected failure")):
                with self.assertRaisesRegex(OSError, "injected failure"):
                    segment_migration.migrate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-02T00:00:00+00:00",
                        key_id="test-key-1",
                        apply=True,
                    )

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse((segments_directory / "catalog.json").exists())
            self.assertFalse((segments_directory / "segment-0001.sqlite3").exists())
            self.assertTrue((segments_directory / ".v1-rollback.sqlite3").is_file())

    def test_apply_snaps_cutoff_for_both_segment_and_hot_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)

            segment_migration.migrate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-02T12:34:56+00:00",
                key_id="test-key-1",
                apply=True,
            )

            segment_path = segments_directory / "segment-0001.sqlite3"
            catalog = json.loads((segments_directory / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["segments"][0]["sealed_at"], "2025-01-02T00:00:00+00:00")
            with sqlite3.connect(source) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT observed_at FROM slot_events").fetchone()[0],
                    "2025-01-02T00:00:00+00:00",
                )
            with sqlite3.connect(segment_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT observed_at FROM slot_events").fetchone()[0],
                    "2025-01-01T00:00:00+00:00",
                )

    def test_stage_hot_replacement_of_a_service_wal_hot_creates_no_source_sidecars(self) -> None:
        # The service leaves the hot database with a WAL header. A plain mode=ro
        # open of that quiesced file creates -wal/-shm that survive close
        # (issue #278). Needs POSIX (fchown); CI is authoritative.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            self._create_source_database(source)
            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
            finally:
                connection.close()
            self.assertEqual(source.read_bytes()[18:20], b"\x02\x02")
            for suffix in ("-wal", "-shm"):
                self.assertFalse(Path(f"{source}{suffix}").exists(), suffix)

            staged = segment_migration._stage_hot_replacement(source, "2025-01-02T00:00:00+00:00")

            try:
                self.assertTrue(staged.is_file())
                for suffix in ("-wal", "-shm", "-journal"):
                    self.assertFalse(Path(f"{source}{suffix}").exists(), suffix)
                segment_sealer._require_regular_source(source)
            finally:
                staged.unlink()

    def test_apply_migrates_pre_cutoff_history_into_cataloged_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_source_database(source)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/migrate_segmented_history.py",
                    "--source",
                    str(source),
                    "--segments-dir",
                    str(segments_directory),
                    "--cutoff",
                    "2025-01-02T00:00:00+00:00",
                    "--key-id",
                    "test-key-1",
                    "--apply",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            catalog_path = segments_directory / "catalog.json"
            segment_path = segments_directory / "segment-0001.sqlite3"
            self.assertEqual(receipt["catalog_path"], str(catalog_path))
            self.assertTrue(catalog_path.is_file())
            self.assertTrue(segment_path.is_file())
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertTrue(catalog["complete"])
            self.assertEqual(catalog["segments"][0]["segment_id"], "segment-0001")

            with sqlite3.connect(source) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT MIN(observed_at) FROM slot_events").fetchone()[0],
                    "2025-01-02T00:00:00+00:00",
                )

            with sqlite3.connect(segment_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0], 1)

    @staticmethod
    def _create_source_database(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT INTO slot_state_current (
                    system_id, enclosure_key, slot, slot_label, present, identify_active, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("system-1", "enclosure-1", 1, "slot-1", 1, 0, "2025-01-02T00:00:00+00:00"),
            )
            for observed_at in ("2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (observed_at, "system-1", "enclosure-1", 1, "slot-1", "state_change", "{}"),
                )
                connection.execute(
                    """
                    INSERT INTO metric_samples (
                        observed_at, system_id, enclosure_key, slot, slot_label, metric_name, value_integer
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (observed_at, "system-1", "enclosure-1", 1, "slot-1", "temperature", 30),
                )
                connection.execute(
                    """
                    INSERT INTO metric_rollups (
                        bucket_start, bucket_seconds, system_id, enclosure_key, slot, slot_label,
                        metric_name, sample_count, value_sum, value_min, value_max, last_value, last_observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observed_at,
                        3600,
                        "system-1",
                        "enclosure-1",
                        1,
                        "slot-1",
                        "temperature",
                        1,
                        30.0,
                        30.0,
                        30.0,
                        30.0,
                        observed_at,
                    ),
                )

    @staticmethod
    def _create_legacy_source_database(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            connection.execute("DROP TABLE metric_rollups")
            connection.execute("DROP TABLE history_table_counts")
            connection.execute("DROP TABLE history_maintenance_state")
