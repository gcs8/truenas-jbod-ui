from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from history_service import segment_migration, segment_sealer, system_backup
from history_service.config import HistorySettings
from history_service.segment_catalog import activation_pending_path
from history_service.segment_reader import SegmentedHistoryReader
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import HISTORY_DB_KEY, SystemBackupService, _ImportActivationTransaction


class SimulatedRotationCrash(BaseException):
    pass


# `_checkpoint_hot_database` opens the hot file with the production busy timeout
# (`SQLITE_CONNECT_TIMEOUT_SECONDS`, 5 s), so a checkpoint refused because a reader
# still holds WAL frames only reports that refusal after the full wait. The tests
# below prove the refusal, not the wait, so they shorten the wait while the reader
# is deliberately held open. The product default is untouched.
def _short_checkpoint_busy_wait():
    return patch.object(system_backup, "SQLITE_CONNECT_TIMEOUT_SECONDS", 0.05)


class LaterGenerationRotationRedTests(unittest.TestCase):
    EXPECTED_PHASES = (
        "prepared",
        "segment-published",
        "hot-staged",
        "hot-replaced",
        "catalog-replaced",
        "cleanup",
    )

    def test_rotation_api_exposes_the_recovery_state_machine(self) -> None:
        rotation = self._rotation_module()

        self.assertTrue(callable(rotation.rotate_segmented_history))
        self.assertTrue(callable(rotation.recover_pending_rotation))
        self.assertEqual(rotation.ROTATION_JOURNAL_PHASES, (*self.EXPECTED_PHASES, "prior-restored"))

    def test_rotation_cli_supports_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)

            dry_run = self._run_rotation_cli(
                source,
                segments_directory,
                backup_directory,
                backup_status_path,
            )

            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertFalse(json.loads(dry_run.stdout)["apply"])
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

            applied = self._run_rotation_cli(
                source,
                segments_directory,
                backup_directory,
                backup_status_path,
                apply=True,
            )

            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(applied.stdout)["generation_id"], "generation-0002")
            self.assertTrue((segments_directory / "segment-0002.sqlite3").is_file())

    def test_rotation_cli_dry_run_does_not_change_existing_directory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            segments_directory.chmod(0o750)

            dry_run = self._run_rotation_cli(
                source,
                segments_directory,
                backup_directory,
                backup_status_path,
            )

            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertFalse(json.loads(dry_run.stdout)["apply"])
            self.assertEqual(stat.S_IMODE(segments_directory.stat().st_mode), 0o750)
            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_appends_generation_0002_without_rewriting_prior_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            prior_segment = segments_directory / "segment-0001.sqlite3"
            prior_segment_sha256 = self._sha256_file(prior_segment)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            receipt = rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T00:00:00+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=backup_status_path,
                apply=True,
            )

            catalog = json.loads((segments_directory / "catalog.json").read_text(encoding="utf-8"))
            self.assertTrue(receipt["apply"])
            self.assertEqual(receipt["generation_id"], "generation-0002")
            self.assertEqual(catalog["generation_id"], "generation-0002")
            self.assertTrue(catalog["complete"])
            self.assertEqual(
                [segment["segment_id"] for segment in catalog["segments"]],
                ["segment-0001", "segment-0002"],
            )
            self.assertEqual(catalog["segments"][1]["sequence"], 2)
            self.assertEqual(catalog.get("tombstones", []), [])
            self.assertTrue(all(segment.get("supersedes", []) == [] for segment in catalog["segments"]))
            self.assertEqual(self._sha256_file(prior_segment), prior_segment_sha256)
            self.assertEqual(self._event_types(prior_segment), ["generation-1-sealed"])
            self.assertEqual(
                self._event_types(segments_directory / "segment-0002.sqlite3"),
                ["generation-1-hot", "generation-2-sealed"],
            )
            self.assertEqual(self._event_types(source), ["generation-2-hot"])
            self.assertEqual(stat.S_IMODE(segments_directory.stat().st_mode), 0o750)
            for name in ("segment-0002.sqlite3", "catalog.json"):
                with self.subTest(name=name):
                    metadata = (segments_directory / name).stat()
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o640)
                    self.assertEqual(
                        (metadata.st_uid, metadata.st_gid),
                        (source.stat().st_uid, source.stat().st_gid),
                    )

    def test_rotation_authenticates_the_new_segment_before_publication_and_blocks_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_link = segment_sealer.os.link
            publication_checked = False

            def inspect_journal_before_link(staged_path: Path, destination_path: Path) -> None:
                nonlocal publication_checked
                if destination_path.name == "segment-0002.sqlite3":
                    marker_path = activation_pending_path(source)
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    publication = marker["new_segment"]
                    self.assertEqual(marker["operation"], "rotate")
                    self.assertEqual(marker["prior_generation_id"], "generation-0001")
                    self.assertEqual(marker["candidate_generation_id"], "generation-0002")
                    self.assertEqual(publication["file_name"], destination_path.name)
                    self.assertEqual(publication["size_bytes"], staged_path.stat().st_size)
                    self.assertEqual(publication["sha256"], self._sha256_file(staged_path))
                    with self.assertRaisesRegex(ValueError, "activation is pending"):
                        SegmentedHistoryReader.from_catalog(
                            hot_path=source,
                            catalog_path=segments_directory / "catalog.json",
                        )
                    publication_checked = True
                original_link(staged_path, destination_path)

            with patch.object(segment_sealer.os, "link", side_effect=inspect_journal_before_link):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertTrue(publication_checked)
            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_refuses_mismatched_backup_artifact_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            backup_artifact = next(backup_directory.iterdir())
            backup_artifact.write_bytes(b"tampered-backup")
            original_hot_sha256 = self._sha256_file(source)
            original_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()

            with self.assertRaisesRegex(ValueError, "backup"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertEqual(self._sha256_file(source), original_hot_sha256)
            self.assertEqual(
                self._sha256_file(segments_directory / "catalog.json"),
                original_catalog_sha256,
            )
            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_refuses_sqlite_sidecar_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            Path(f"{source}-wal").write_bytes(b"unexpected-sidecar")
            rotation = self._rotation_module()

            with self.assertRaisesRegex(ValueError, "sidecar"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_row_count_read_of_a_service_wal_hot_creates_no_sqlite_sidecars(self) -> None:
        # The history service initialises the hot database in WAL mode. A plain
        # mode=ro open of that quiesced file creates -wal/-shm that outlive the
        # connection, and the sealer's sidecar preflight then refused the
        # rotation after its journal was already written (issue #278).
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "history.db"
            self._create_service_wal_hot(source)
            rotation = self._rotation_module()

            counts = rotation._history_row_counts(source)

            self.assertEqual(counts["slot_events"], 1)
            self._assert_no_sqlite_sidecars(source)
            segment_sealer._require_regular_source(source)

    def test_rotation_apply_completes_against_a_service_wal_hot(self) -> None:
        # Every other apply fixture forces journal_mode=DELETE, which hid #278.
        # Needs POSIX (segment ownership checks); CI is authoritative.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00", journal_mode="WAL")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00", journal_mode="WAL")
            self.assertEqual(source.read_bytes()[18:20], b"\x02\x02")
            self._assert_no_sqlite_sidecars(source)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            receipt = rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T00:00:00+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=backup_status_path,
                apply=True,
            )

            self.assertTrue(receipt["apply"])
            self.assertEqual(receipt["generation_id"], "generation-0002")
            self.assertFalse(activation_pending_path(source).exists())
            self._assert_no_sqlite_sidecars(source)
            self.assertEqual(
                self._event_types(segments_directory / "segment-0002.sqlite3"),
                ["generation-1-hot", "generation-2-sealed"],
            )
            self.assertEqual(self._event_types(source), ["generation-2-hot"])

    def test_rotation_rechecks_quiescence_after_preflight_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_headroom = rotation._require_headroom

            def create_sidecar_after_preflight(*args, **kwargs) -> None:
                original_headroom(*args, **kwargs)
                Path(f"{source}-wal").write_bytes(b"late-writer-sidecar")

            with patch.object(rotation, "_require_headroom", side_effect=create_sidecar_after_preflight):
                with self.assertRaisesRegex(ValueError, "sidecar|quiesc"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((root / ".history.db.generation-0002.rollback.sqlite3").exists())
            self.assertFalse((segments_directory / ".generation-0001.catalog.rollback.json").exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_recovery_removes_both_links_after_segment_publish_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            prior_hot_sha256 = self._sha256_file(source)
            prior_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()
            original_unlink = Path.unlink

            def crash_before_temporary_link_unlink(path: Path, *args, **kwargs) -> None:
                if (
                    path.parent == segments_directory
                    and path.name.startswith(".segment-")
                    and (segments_directory / "segment-0002.sqlite3").exists()
                ):
                    raise SimulatedRotationCrash("segment-link-visible")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", crash_before_temporary_link_unlink):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            segment_path = segments_directory / "segment-0002.sqlite3"
            temporary_links = list(segments_directory.glob(".segment-*.sqlite3"))
            self.assertEqual(len(temporary_links), 1)
            self.assertEqual(segment_path.stat().st_nlink, 2)
            self.assertEqual(temporary_links[0].stat().st_ino, segment_path.stat().st_ino)

            result = rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(result["recovery_state"], "prior-generation-restored")
            self.assertEqual(self._sha256_file(source), prior_hot_sha256)
            self.assertEqual(self._sha256_file(segments_directory / "catalog.json"), prior_catalog_sha256)
            self.assertFalse(segment_path.exists())
            self.assertFalse(temporary_links[0].exists())
            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_refuses_a_thirty_third_active_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            catalog_path = segments_directory / "catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            base_segment_path = segments_directory / "segment-0001.sqlite3"
            base_segment_bytes = base_segment_path.read_bytes()
            base_entry = dict(catalog["segments"][0])
            entries = []
            for sequence in range(1, 33):
                segment_id = f"segment-{sequence:04d}"
                segment_path = segments_directory / f"{segment_id}.sqlite3"
                if sequence > 1:
                    segment_path.write_bytes(base_segment_bytes)
                entry = dict(base_entry)
                entry.update(
                    {
                        "segment_id": segment_id,
                        "file_name": segment_path.name,
                        "sequence": sequence,
                    }
                )
                entries.append(entry)
            catalog["generation_id"] = "generation-0032"
            catalog["segments"] = entries
            catalog_path.write_text(json.dumps(catalog, sort_keys=True), encoding="utf-8")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            with self.assertRaisesRegex(ValueError, "segment limit"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-33",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0033.sqlite3").exists())

    def test_recovery_converges_every_durable_rotation_phase(self) -> None:
        for phase in self.EXPECTED_PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                source = root / "history.db"
                segments_directory = root / "segments"
                self._create_generation_0001(source, segments_directory)
                self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
                self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
                backup_directory, backup_status_path = self._create_full_backup_evidence(root)
                prior_hot_sha256 = self._sha256_file(source)
                prior_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
                prior_segment_sha256 = self._sha256_file(segments_directory / "segment-0001.sqlite3")
                rotation = self._rotation_module()
                self.assertTrue(
                    hasattr(rotation, "_write_rotation_journal"),
                    "rotation journal writer is not implemented",
                )
                original_write_journal = rotation._write_rotation_journal

                def write_then_crash(path: Path, payload: dict[str, object]) -> None:
                    original_write_journal(path, payload)
                    if payload.get("phase") == phase:
                        raise SimulatedRotationCrash(phase)

                with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                    with self.assertRaises(SimulatedRotationCrash):
                        rotation.rotate_segmented_history(
                            source=source,
                            segments_directory=segments_directory,
                            cutoff="2025-01-03T00:00:00+00:00",
                            key_id="generation-key-2",
                            scheduled_backup_directory=backup_directory,
                            scheduled_backup_status_path=backup_status_path,
                            apply=True,
                        )

                marker_path = activation_pending_path(source)
                self.assertTrue(marker_path.is_file())
                with self.assertRaisesRegex(ValueError, "activation is pending"):
                    SegmentedHistoryReader.from_catalog(
                        hot_path=source,
                        catalog_path=segments_directory / "catalog.json",
                    )

                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

                self.assertFalse(marker_path.exists())
                self.assertEqual(
                    self._sha256_file(segments_directory / "segment-0001.sqlite3"),
                    prior_segment_sha256,
                )
                if phase in {"catalog-replaced", "cleanup"}:
                    catalog = json.loads(
                        (segments_directory / "catalog.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(catalog["generation_id"], "generation-0002")
                    self.assertTrue((segments_directory / "segment-0002.sqlite3").is_file())
                    self.assertEqual(self._event_types(source), ["generation-2-hot"])
                else:
                    self.assertEqual(self._sha256_file(source), prior_hot_sha256)
                    self.assertEqual(
                        self._sha256_file(segments_directory / "catalog.json"),
                        prior_catalog_sha256,
                    )
                    self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def _pending_cleanup_fixture(self, root: Path, phase: str):
        source = root / "history.db"
        segments = root / "segments"
        self._create_generation_0001(source, segments)
        self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
        self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
        backup_directory, backup_status = self._create_full_backup_evidence(root)
        rotation = self._rotation_module()
        writer = rotation._write_rotation_journal

        def stop_publisher(path, payload):
            record = writer(path, payload)
            if payload["phase"] == phase:
                raise SimulatedRotationCrash(phase)
            return record

        with patch.object(rotation, "_write_rotation_journal", side_effect=stop_publisher):
            with self.assertRaises(SimulatedRotationCrash):
                rotation.rotate_segmented_history(
                    source=source, segments_directory=segments,
                    cutoff="2025-01-03T00:00:00+00:00", key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status, apply=True,
                )
        journal = json.loads(activation_pending_path(source).read_text())
        selected = {segments / "catalog.json": (segments / "catalog.json").read_bytes()}
        selected[segments / "segment-0001.sqlite3"] = (segments / "segment-0001.sqlite3").read_bytes()
        if phase == "catalog-replaced":
            selected[source] = source.read_bytes()
            selected[segments / "segment-0002.sqlite3"] = (segments / "segment-0002.sqlite3").read_bytes()
        else:
            selected[source] = (root / journal["prior_hot_rollback"]["file_name"]).read_bytes()
        return source, segments, journal, selected

    def _restart_recovery(self, source: Path, segments: Path, *, pending: bool) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/rotate_segmented_history.py", "--source", str(source),
             "--segments-dir", str(segments), "--recover", "--apply"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
        )
        if pending:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            # Preserve the public no-pending contract, not a fabricated success.
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("recovery is not pending", result.stderr)

    def _assert_selected_generation(self, source, segments, selected, *, candidate):
        for path, content in selected.items():
            self.assertEqual(self._sha256_file(path), hashlib.sha256(content).hexdigest(), path.name)
        catalog = json.loads((segments / "catalog.json").read_text())
        self.assertEqual(catalog["generation_id"], "generation-0002" if candidate else "generation-0001")
        self.assertEqual(self._event_types(segments / "segment-0001.sqlite3"), ["generation-1-sealed"])
        if candidate:
            self.assertEqual(self._event_types(source), ["generation-2-hot"])
            self.assertEqual(self._event_types(segments / "segment-0002.sqlite3"),
                             ["generation-1-hot", "generation-2-sealed"])
        else:
            self.assertEqual(self._event_types(source),
                             ["generation-1-hot", "generation-2-sealed", "generation-2-hot"])
            self.assertFalse((segments / "segment-0002.sqlite3").exists())
        self.assertFalse(activation_pending_path(source).exists())
        reader = SegmentedHistoryReader.from_catalog(hot_path=source, catalog_path=segments / "catalog.json")
        reader.verify_catalog_segments()
        self.assertEqual(
            [row["event_type"] for row in reader.list_slot_events("synthetic-system", "synthetic-enclosure", 1)],
            ["generation-2-hot", "generation-2-sealed", "generation-1-hot", "generation-1-sealed"],
        )
        self.assertEqual(list(source.parent.glob(".*.rollback.sqlite3")), [])
        self.assertEqual(list(segments.glob(".*.rollback.json")), [])
        self.assertEqual(list(source.parent.glob(".history.db.segmented-*.sqlite3")), [])
        self.assertEqual(list(segments.glob(".rotation-catalog-*.json")), [])

    def test_recovery_cleanup_restarts_after_each_real_unlink(self) -> None:
        for phase in ("hot-staged", "hot-replaced", "catalog-replaced"):
            keys = ["prior_hot_rollback", "prior_catalog_rollback", "journal"]
            if phase != "catalog-replaced":
                keys += ["new_segment", "candidate_catalog"]
            if phase == "hot-staged":
                keys += ["staged_hot"]
            for key in keys:
                with self.subTest(phase=phase, removed=key), tempfile.TemporaryDirectory() as temporary:
                    source, segments, journal, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                    marker = activation_pending_path(source)
                    target = marker.name if key == "journal" else journal[key]["file_name"]
                    unlink = os.unlink
                    removed = []

                    def unlink_then_crash(path, *args, **kwargs):
                        unlink(path, *args, **kwargs)
                        if Path(path).name == target:
                            removed.append(target)
                            raise SimulatedRotationCrash(target)

                    with patch.object(os, "unlink", side_effect=unlink_then_crash):
                        with self.assertRaises(SimulatedRotationCrash):
                            self._rotation_module().recover_pending_rotation(
                                source=source, segments_directory=segments, apply=True,
                            )
                    self.assertEqual(removed, [target])
                    if marker.exists():
                        with self.assertRaisesRegex(ValueError, "activation is pending"):
                            SegmentedHistoryReader.from_catalog(hot_path=source, catalog_path=segments / "catalog.json")
                    self._restart_recovery(source, segments, pending=key != "journal")
                    self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")
                    self._restart_recovery(source, segments, pending=False)
                    self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")

    def test_recovery_cleanup_survives_process_exit_at_each_mutating_syscall(self) -> None:
        # Trace real cleanup calls after the terminal decision, then kill a fresh
        # child at each observed boundary. os._exit deliberately skips finally.
        script = """
import json
import os
import sys
from pathlib import Path
from history_service import segment_rotation as rotation

source, segments, stop = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
armed = False
ordinal = 0

def checkpoint(operation):
    global ordinal
    if armed:
        print(json.dumps({"ordinal": ordinal, "operation": operation}), flush=True)
        if ordinal == stop:
            os._exit(73)
        ordinal += 1

writer = rotation._write_rotation_journal
def write_terminal(path, payload):
    global armed
    record = writer(path, payload)
    if payload["phase"] in {"cleanup", "prior-restored"}:
        armed = True
        checkpoint("terminal-journal")
    return record
rotation._write_rotation_journal = write_terminal

def wrap(name):
    original = getattr(os, name)
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        checkpoint(name)
        return result
    setattr(os, name, observed)
for name in ("replace", "unlink", "rmdir", "fsync"):
    wrap(name)
rotation.recover_pending_rotation(source=source, segments_directory=segments, apply=True)
"""
        for phase in ("hot-replaced", "catalog-replaced"):
            with self.subTest(phase=phase, boundary="trace"), tempfile.TemporaryDirectory() as temporary:
                source, segments, _, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                traced = subprocess.run(
                    [sys.executable, "-c", script, str(source), str(segments), "-1"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(traced.returncode, 0, traced.stderr)
                events = [json.loads(line) for line in traced.stdout.splitlines()]
                self.assertEqual({event["operation"] for event in events},
                                 {"terminal-journal", "replace", "unlink", "rmdir", "fsync"})
                self.assertLess(len(events), 40, "keep the synthetic crash matrix bounded")
                self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")
            for event in events:
                with self.subTest(phase=phase, boundary=event), tempfile.TemporaryDirectory() as temporary:
                    source, segments, _, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                    crashed = subprocess.run(
                        [sys.executable, "-c", script, str(source), str(segments), str(event["ordinal"])],
                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
                    )
                    self.assertEqual(crashed.returncode, 73, crashed.stderr)
                    self.assertEqual(json.loads(crashed.stdout.splitlines()[-1]), event)
                    self._restart_recovery(source, segments, pending=activation_pending_path(source).exists())
                    self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")
                    self._restart_recovery(source, segments, pending=False)
                    self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")

    def test_recovery_persists_selected_terminal_phase_before_cleanup(self) -> None:
        for phase, terminal in (("hot-replaced", "prior-restored"), ("catalog-replaced", "cleanup")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                source, segments, journal, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                rotation = self._rotation_module()
                writer = rotation._write_rotation_journal

                def write_then_crash(path, payload):
                    result = writer(path, payload)
                    if payload["phase"] == terminal:
                        raise SimulatedRotationCrash(terminal)
                    return result

                with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                    with self.assertRaises(SimulatedRotationCrash):
                        rotation.recover_pending_rotation(source=source, segments_directory=segments, apply=True)
                self.assertEqual(json.loads(activation_pending_path(source).read_text())["phase"], terminal)
                self.assertTrue((source.parent / journal["prior_hot_rollback"]["file_name"]).is_file())
                self.assertTrue((segments / journal["prior_catalog_rollback"]["file_name"]).is_file())
                self._restart_recovery(source, segments, pending=True)
                self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")

    def test_recovery_terminal_journal_fsync_failure_does_not_retire_evidence(self) -> None:
        for phase in ("hot-staged", "catalog-replaced"):
            for ordinal in (1, 2):
                with self.subTest(phase=phase, fsync=ordinal), tempfile.TemporaryDirectory() as temporary:
                    source, segments, journal, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                    sync = os.fsync
                    calls = []
                    before = {p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}

                    def fail_sync(fd):
                        calls.append(fd)
                        if len(calls) == ordinal:
                            raise OSError("terminal fsync failed")
                        return sync(fd)

                    with patch.object(os, "fsync", side_effect=fail_sync):
                        with self.assertRaisesRegex(OSError, "terminal fsync"):
                            self._rotation_module().recover_pending_rotation(
                                source=source, segments_directory=segments, apply=True,
                            )
                    self.assertEqual(len(calls), ordinal)
                    for path, content in before.items():
                        if path != activation_pending_path(source):
                            self.assertTrue(path.is_file(), path.name)
                            self.assertEqual(path.read_bytes(), content)
                    self._restart_recovery(source, segments, pending=True)
                    self._assert_selected_generation(source, segments, selected, candidate=phase == "catalog-replaced")

    def test_recovery_terminal_replay_rejects_changed_selected_generation(self) -> None:
        for phase in ("hot-replaced", "catalog-replaced"):
            targets = ["hot", "catalog", "prior-segment", "remaining-rollback", "wrong-generation"]
            if phase == "catalog-replaced":
                targets += ["new-segment"]
            for target in targets:
                with self.subTest(phase=phase, target=target), tempfile.TemporaryDirectory() as temporary:
                    source, segments, journal, selected = self._pending_cleanup_fixture(Path(temporary), phase)
                    rotation = self._rotation_module()
                    remove = rotation._remove_recorded_file
                    other_hot = source.read_bytes() if phase == "hot-replaced" else (
                        source.parent / journal["prior_hot_rollback"]["file_name"]
                    ).read_bytes()
                    other_catalog = (segments / journal["candidate_catalog"]["file_name"]).read_bytes() if phase == "hot-replaced" else (
                        segments / journal["prior_catalog_rollback"]["file_name"]
                    ).read_bytes()

                    def remove_then_crash(root, record, *, label, allow_missing=False):
                        remove(root, record, label=label, allow_missing=allow_missing)
                        if label == "prior hot rollback":
                            raise SimulatedRotationCrash(label)

                    with patch.object(rotation, "_remove_recorded_file", side_effect=remove_then_crash):
                        with self.assertRaises(SimulatedRotationCrash):
                            rotation.recover_pending_rotation(source=source, segments_directory=segments, apply=True)
                    if target == "wrong-generation":
                        source.write_bytes(other_hot)
                        (segments / "catalog.json").write_bytes(other_catalog)
                    else:
                        path = {
                            "hot": source, "catalog": segments / "catalog.json",
                            "prior-segment": segments / "segment-0001.sqlite3",
                            "new-segment": segments / "segment-0002.sqlite3",
                            "remaining-rollback": segments / journal["prior_catalog_rollback"]["file_name"],
                        }[target]
                        path.write_bytes(b"synthetic-divergent-evidence")
                    before = {p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}
                    for apply in (False, True):
                        with self.assertRaisesRegex(ValueError, "integrity|divergent|generation"):
                            rotation.recover_pending_rotation(source=source, segments_directory=segments, apply=apply)
                        self.assertEqual({p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}, before)

    def test_prior_recovery_revalidates_retained_segments_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, segments, _, _ = self._pending_cleanup_fixture(Path(temporary), "hot-replaced")
            (segments / "segment-0001.sqlite3").write_bytes(b"synthetic-corrupt-prior-segment")
            before = {p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}
            with self.assertRaisesRegex(ValueError, "integrity"):
                self._rotation_module().recover_pending_rotation(
                    source=source, segments_directory=segments, apply=True,
                )
            self.assertEqual({p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}, before)

    def test_recovery_nonterminal_missing_rollbacks_remain_fail_closed(self) -> None:
        for phase in ("hot-replaced", "catalog-replaced"):
            for key in ("prior_hot_rollback", "prior_catalog_rollback"):
                with self.subTest(phase=phase, key=key), tempfile.TemporaryDirectory() as temporary:
                    source, segments, journal, _ = self._pending_cleanup_fixture(Path(temporary), phase)
                    root = source.parent if key == "prior_hot_rollback" else segments
                    (root / journal[key]["file_name"]).unlink()
                    before = {p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}
                    for apply in (False, True):
                        with self.assertRaisesRegex(ValueError, "rollback integrity"):
                            self._rotation_module().recover_pending_rotation(
                                source=source, segments_directory=segments, apply=apply,
                            )
                        self.assertEqual({p: p.read_bytes() for p in Path(temporary).rglob("*") if p.is_file()}, before)

    def test_recovery_refuses_divergent_hot_bytes_and_preserves_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            self.assertTrue(
                hasattr(rotation, "_write_rotation_journal"),
                "rotation journal writer is not implemented",
            )
            original_write_journal = rotation._write_rotation_journal

            def crash_after_hot_replace(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                if payload.get("phase") == "hot-replaced":
                    raise SimulatedRotationCrash("hot-replaced")

            with patch.object(rotation, "_write_rotation_journal", side_effect=crash_after_hot_replace):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            with sqlite3.connect(source) as connection:
                self._insert_event(connection, "divergent-write", "2025-01-04T00:00:00+00:00")
            divergent_hot_sha256 = self._sha256_file(source)
            marker_path = activation_pending_path(source)

            with self.assertRaisesRegex(ValueError, "divergent|integrity"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(self._sha256_file(source), divergent_hot_sha256)
            self.assertTrue(marker_path.is_file())
            self.assertTrue((segments_directory / "segment-0002.sqlite3").is_file())

    def test_recovery_refuses_replaced_published_segment_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            self.assertTrue(
                hasattr(rotation, "_write_rotation_journal"),
                "rotation journal writer is not implemented",
            )
            original_write_journal = rotation._write_rotation_journal

            def crash_after_segment_publish(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                if payload.get("phase") == "segment-published":
                    raise SimulatedRotationCrash("segment-published")

            with patch.object(rotation, "_write_rotation_journal", side_effect=crash_after_segment_publish):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            segment_path = segments_directory / "segment-0002.sqlite3"
            segment_path.write_bytes(b"replacement-segment")
            replacement_sha256 = self._sha256_file(segment_path)
            marker_path = activation_pending_path(source)

            with self.assertRaisesRegex(ValueError, "integrity|authenticated"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(self._sha256_file(segment_path), replacement_sha256)
            self.assertTrue(marker_path.is_file())

    def test_rotation_refuses_insufficient_headroom_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            no_space = type("DiskUsage", (), {"free": 0})()

            with patch.object(rotation.shutil, "disk_usage", return_value=no_space):
                with self.assertRaisesRegex(ValueError, "headroom"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_short_copy_removes_partial_rollback_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write = rotation.os.write
            injected = False

            def short_write(descriptor: int, data: bytes | memoryview) -> int:
                nonlocal injected
                if not injected:
                    injected = True
                    return 0
                return original_write(descriptor, data)

            with patch.object(rotation.os, "write", side_effect=short_write):
                with self.assertRaisesRegex(OSError, "incomplete"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((root / ".history.db.generation-0002.rollback.sqlite3").exists())

    def test_short_copy_cleanup_does_not_unlink_replacement_at_reserved_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            rollback_path = root / ".history.db.generation-0002.rollback.sqlite3"
            saved_partial_path = root / "saved-partial-rollback.sqlite3"
            replacement_bytes = b"unrelated-short-copy-replacement"
            original_write = rotation.os.write
            original_replace = rotation.os.replace
            write_failed = False
            swapped = False

            def short_write(descriptor: int, data: bytes | memoryview) -> int:
                nonlocal write_failed
                if not write_failed:
                    write_failed = True
                    return 0
                return original_write(descriptor, data)

            def swap_before_retirement(
                source_path: str | os.PathLike[str],
                destination_path: str | os.PathLike[str],
            ) -> None:
                nonlocal swapped
                if Path(source_path) == rollback_path and not swapped:
                    original_replace(rollback_path, saved_partial_path)
                    rollback_path.write_bytes(replacement_bytes)
                    swapped = True
                original_replace(source_path, destination_path)

            with (
                patch.object(rotation.os, "write", side_effect=short_write),
                patch.object(rotation.os, "replace", side_effect=swap_before_retirement),
            ):
                with self.assertRaisesRegex((OSError, ValueError), "incomplete|integrity|changed"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertTrue(write_failed)
            self.assertTrue(swapped)
            self.assertEqual(rollback_path.read_bytes(), replacement_bytes)
            self.assertTrue(saved_partial_path.is_file())
            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_directory_fsync_failure_removes_unjournaled_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            with patch.object(
                rotation,
                "_fsync_directory",
                side_effect=OSError("injected directory fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "fsync"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((root / ".history.db.generation-0002.rollback.sqlite3").exists())

    def test_rotation_malformed_timestamp_preserves_recoverable_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "malformed", "not-a-timestamp")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            original_hot_sha256 = self._sha256_file(source)
            original_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()

            with self.assertRaisesRegex(ValueError, "timestamp"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertTrue(activation_pending_path(source).is_file())
            rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )
            self.assertEqual(self._sha256_file(source), original_hot_sha256)
            self.assertEqual(
                self._sha256_file(segments_directory / "catalog.json"),
                original_catalog_sha256,
            )
            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_catalog_stage_failure_leaves_a_recoverable_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            original_hot_sha256 = self._sha256_file(source)
            original_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()

            with patch.object(
                rotation,
                "_stage_catalog",
                side_effect=OSError("injected catalog staging failure"),
            ):
                with self.assertRaisesRegex(OSError, "catalog staging"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertTrue(activation_pending_path(source).is_file())
            rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )
            self.assertEqual(self._sha256_file(source), original_hot_sha256)
            self.assertEqual(
                self._sha256_file(segments_directory / "catalog.json"),
                original_catalog_sha256,
            )
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())
            self.assertEqual(list(root.glob(".history.db.segmented-*.sqlite3")), [])

    def test_rotation_hot_replace_failure_recovers_the_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            original_hot_sha256 = self._sha256_file(source)
            original_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()
            self.assertTrue(hasattr(rotation, "_replace_hot"), "hot replacement helper is not implemented")

            with patch.object(
                rotation,
                "_replace_hot",
                side_effect=OSError("injected hot replacement failure"),
            ):
                with self.assertRaisesRegex(OSError, "hot replacement"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )
            self.assertEqual(self._sha256_file(source), original_hot_sha256)
            self.assertEqual(
                self._sha256_file(segments_directory / "catalog.json"),
                original_catalog_sha256,
            )
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_catalog_replace_failure_recovers_the_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            original_hot_sha256 = self._sha256_file(source)
            original_catalog_sha256 = self._sha256_file(segments_directory / "catalog.json")
            rotation = self._rotation_module()
            self.assertTrue(
                hasattr(rotation, "_replace_catalog"),
                "catalog replacement helper is not implemented",
            )

            with patch.object(
                rotation,
                "_replace_catalog",
                side_effect=OSError("injected catalog replacement failure"),
            ):
                with self.assertRaisesRegex(OSError, "catalog replacement"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )
            self.assertEqual(self._sha256_file(source), original_hot_sha256)
            self.assertEqual(
                self._sha256_file(segments_directory / "catalog.json"),
                original_catalog_sha256,
            )
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_cleanup_failure_is_finished_by_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_remove = rotation._remove_recorded_file

            def fail_catalog_rollback_cleanup(
                cleanup_root: Path,
                record: dict[str, object],
                *,
                label: str,
                allow_missing: bool = False,
            ) -> None:
                if label == "prior catalog rollback":
                    raise OSError("injected cleanup failure")
                original_remove(
                    cleanup_root,
                    record,
                    label=label,
                    allow_missing=allow_missing,
                )

            with patch.object(
                rotation,
                "_remove_recorded_file",
                side_effect=fail_catalog_rollback_cleanup,
            ):
                with self.assertRaisesRegex(OSError, "cleanup"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertTrue(activation_pending_path(source).is_file())
            receipt = rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )
            self.assertEqual(receipt["recovery_state"], "candidate-finalized")
            self.assertFalse(activation_pending_path(source).exists())
            catalog = json.loads((segments_directory / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["generation_id"], "generation-0002")
            self.assertTrue((segments_directory / "segment-0002.sqlite3").is_file())

    def test_rotation_refuses_hard_linked_source_alias_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            os.link(source, root / "history-alias.db")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            with self.assertRaisesRegex(ValueError, "hard-link"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-03T00:00:00+00:00",
                    key_id="generation-key-2",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=backup_status_path,
                    apply=True,
                )

            self.assertFalse(activation_pending_path(source).exists())

    def test_rotation_refuses_source_replacement_after_preflight_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_validate_backup = rotation._validate_backup_evidence

            def validate_backup_then_replace_source(
                candidate_backup_directory: Path,
                candidate_status_path: Path,
            ) -> dict[str, object]:
                evidence = original_validate_backup(
                    candidate_backup_directory,
                    candidate_status_path,
                )
                replacement = root / "replacement-history.db"
                replacement.write_bytes(source.read_bytes())
                with sqlite3.connect(replacement) as connection:
                    self._insert_event(
                        connection,
                        "replacement-row",
                        "2025-01-03T00:00:00+00:00",
                    )
                replacement.replace(source)
                return evidence

            with patch.object(
                rotation,
                "_validate_backup_evidence",
                side_effect=validate_backup_then_replace_source,
            ):
                with self.assertRaisesRegex(ValueError, "source changed"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertEqual(self._event_types(source)[-1], "replacement-row")
            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_rotation_refuses_catalog_replacement_after_verification_before_journal_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_validate_backup = rotation._validate_backup_evidence
            catalog_path = segments_directory / "catalog.json"

            def validate_backup_then_replace_catalog(
                candidate_backup_directory: Path,
                candidate_status_path: Path,
            ) -> dict[str, object]:
                evidence = original_validate_backup(
                    candidate_backup_directory,
                    candidate_status_path,
                )
                replacement_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                replacement_catalog["generation_id"] = "generation-0099"
                catalog_path.write_text(
                    json.dumps(replacement_catalog, sort_keys=True),
                    encoding="utf-8",
                )
                return evidence

            with patch.object(
                rotation,
                "_validate_backup_evidence",
                side_effect=validate_backup_then_replace_catalog,
            ):
                with self.assertRaisesRegex(ValueError, "catalog changed"):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertFalse(activation_pending_path(source).exists())
            self.assertFalse((segments_directory / "segment-0002.sqlite3").exists())

    def test_each_rotation_requires_a_newer_verified_full_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            first_backup_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            backup_directory, first_status_path = self._create_full_backup_evidence(
                root,
                nonce="11111111",
                observed_at=first_backup_at,
            )
            rotation = self._rotation_module()
            rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T00:00:00+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=first_status_path,
                apply=True,
            )
            self._append_event(source, "generation-3-sealed", "2025-01-04T12:00:00+00:00")
            self._append_event(source, "generation-3-hot", "2025-01-05T12:00:00+00:00")

            with self.assertRaisesRegex(ValueError, "newer.*backup"):
                rotation.rotate_segmented_history(
                    source=source,
                    segments_directory=segments_directory,
                    cutoff="2025-01-05T00:00:00+00:00",
                    key_id="generation-key-3",
                    scheduled_backup_directory=backup_directory,
                    scheduled_backup_status_path=first_status_path,
                    apply=True,
                )

            backup_directory, second_status_path = self._create_full_backup_evidence(
                root,
                nonce="22222222",
                observed_at=datetime.now(timezone.utc),
            )
            receipt = rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-05T00:00:00+00:00",
                key_id="generation-key-3",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=second_status_path,
                apply=True,
            )

            self.assertEqual(receipt["generation_id"], "generation-0003")
            catalog = json.loads((segments_directory / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [segment["segment_id"] for segment in catalog["segments"]],
                ["segment-0001", "segment-0002", "segment-0003"],
            )

    def test_recovery_removes_authenticated_rollbacks_left_before_first_journal_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()

            with patch.object(
                rotation,
                "_write_rotation_journal",
                side_effect=SimulatedRotationCrash("before-journal-write"),
            ):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            rollback_hot = root / ".history.db.generation-0002.rollback.sqlite3"
            rollback_catalog = segments_directory / ".generation-0001.catalog.rollback.json"
            self.assertTrue(rollback_hot.is_file())
            self.assertTrue(rollback_catalog.is_file())
            self.assertFalse(activation_pending_path(source).exists())

            receipt = rotation.recover_pending_rotation(
                source=source,
                segments_directory=segments_directory,
                apply=True,
            )

            self.assertEqual(receipt["recovery_state"], "orphan-rollbacks-removed")
            self.assertFalse(rollback_hot.exists())
            self.assertFalse(rollback_catalog.exists())

    def test_recovery_reports_segment_temp_left_by_process_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_copy_and_prune = segment_sealer._copy_and_prune
            original_unlink = Path.unlink
            orphan_path: Path | None = None

            def copy_then_kill(copy_source: Path, destination: Path, cutoff: str) -> dict[str, int]:
                nonlocal orphan_path
                original_copy_and_prune(copy_source, destination, cutoff)
                orphan_path = destination
                raise SimulatedRotationCrash("segment-copy")

            def preserve_process_kill_temp(path: Path, *args, **kwargs) -> None:
                if path == orphan_path:
                    return
                original_unlink(path, *args, **kwargs)

            with (
                patch.object(segment_sealer, "_copy_and_prune", side_effect=copy_then_kill),
                patch.object(Path, "unlink", preserve_process_kill_temp),
            ):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            self.assertIsNotNone(orphan_path)
            assert orphan_path is not None
            marker_path = activation_pending_path(source)
            self.assertTrue(marker_path.is_file())
            self.assertTrue(orphan_path.is_file())
            marker_bytes = marker_path.read_bytes()
            orphan_bytes = orphan_path.read_bytes()

            with self.assertRaises(ValueError) as raised:
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(
                str(raised.exception),
                "Segment rotation found unreferenced segment temporary artifact: "
                + json.dumps(str(orphan_path))
                + ".",
            )
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(orphan_path.read_bytes(), orphan_bytes)

    def test_recovery_fails_closed_on_unjournaled_staging_artifact(self) -> None:
        for missing_record in ("staged_hot", "candidate_catalog"):
            with self.subTest(missing_record=missing_record), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                source = root / "history.db"
                segments_directory = root / "segments"
                self._create_generation_0001(source, segments_directory)
                self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
                self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
                backup_directory, backup_status_path = self._create_full_backup_evidence(root)
                rotation = self._rotation_module()
                original_write_journal = rotation._write_rotation_journal

                def crash_before_journal_update(path: Path, payload: dict[str, object]) -> None:
                    if (
                        missing_record == "staged_hot"
                        and payload.get("phase") == "segment-published"
                        and "staged_hot" in payload
                    ):
                        raise SimulatedRotationCrash("staged-hot-before-journal")
                    if (
                        missing_record == "candidate_catalog"
                        and payload.get("phase") == "hot-staged"
                    ):
                        raise SimulatedRotationCrash("catalog-before-journal")
                    original_write_journal(path, payload)

                with patch.object(
                    rotation,
                    "_write_rotation_journal",
                    side_effect=crash_before_journal_update,
                ):
                    with self.assertRaises(SimulatedRotationCrash):
                        rotation.rotate_segmented_history(
                            source=source,
                            segments_directory=segments_directory,
                            cutoff="2025-01-03T00:00:00+00:00",
                            key_id="generation-key-2",
                            scheduled_backup_directory=backup_directory,
                            scheduled_backup_status_path=backup_status_path,
                            apply=True,
                        )

                marker_path = activation_pending_path(source)
                self.assertTrue(marker_path.is_file())
                generated_unexpected_paths = (
                    list(root.glob(".history.db.segmented-*.sqlite3"))
                    if missing_record == "staged_hot"
                    else list(segments_directory.glob(".rotation-catalog-*.json"))
                )
                self.assertEqual(len(generated_unexpected_paths), 1)
                extra_unexpected_paths = [
                    root / ".history.db.segmented-hostile\nname,extra.sqlite3",
                    segments_directory / ".rotation-catalog-hostile\x1b[31m.json",
                ]
                for index, path in enumerate(extra_unexpected_paths):
                    path.write_bytes(f"unexpected-{index}".encode())
                unexpected_paths = sorted(
                    [*generated_unexpected_paths, *extra_unexpected_paths],
                    key=str,
                )
                staging_paths = [
                    *root.glob(".history.db.segmented-*.sqlite3"),
                    *segments_directory.glob(".rotation-catalog-*.json"),
                ]
                artifact_bytes = {path: path.read_bytes() for path in staging_paths}
                journal_bytes = marker_path.read_bytes()

                with self.assertRaises(ValueError) as raised:
                    rotation.recover_pending_rotation(
                        source=source,
                        segments_directory=segments_directory,
                        apply=True,
                    )
                message = str(raised.exception)
                self.assertEqual(
                    message,
                    "Segment rotation found unauthenticated staging artifacts: "
                    + ", ".join(json.dumps(str(path)) for path in unexpected_paths)
                    + ".",
                )
                self.assertNotIn("\n", message)
                self.assertNotIn("\x1b", message)
                self.assertEqual(marker_path.read_bytes(), journal_bytes)
                self.assertEqual(
                    {path: path.read_bytes() for path in staging_paths},
                    artifact_bytes,
                )

    def test_rotation_file_record_rejects_path_replacement_during_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            candidate = root / "candidate"
            replacement = root / "replacement"
            candidate.write_bytes(b"a" * (2 * 1024 * 1024))
            replacement.write_bytes(b"b" * (2 * 1024 * 1024))
            rotation = self._rotation_module()
            original_read = rotation.os.read
            swapped = False

            def read_then_replace(descriptor: int, size: int) -> bytes:
                nonlocal swapped
                content = original_read(descriptor, size)
                if content and not swapped:
                    replacement.replace(candidate)
                    swapped = True
                return content

            with patch.object(rotation.os, "read", side_effect=read_then_replace):
                with self.assertRaisesRegex(ValueError, "changed"):
                    rotation._record_file(candidate)

            self.assertTrue(swapped)
            self.assertEqual(candidate.read_bytes(), b"b" * (2 * 1024 * 1024))

    def test_recovery_rejects_duplicate_journal_keys_without_mutating_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def write_then_crash(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                raise SimulatedRotationCrash("prepared")

            with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            marker_path = activation_pending_path(source)
            content = marker_path.read_text(encoding="utf-8")
            duplicate = content.replace(
                '"phase":"prepared"',
                '"phase":"prepared","phase":"cleanup"',
                1,
            )
            self.assertNotEqual(duplicate, content)
            marker_path.write_text(duplicate, encoding="utf-8")
            duplicate_bytes = marker_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "duplicate|journal"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(marker_path.read_bytes(), duplicate_bytes)
            self.assertTrue(marker_path.is_file())

    def test_recovery_rejects_hard_linked_journal_and_preserves_both_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def write_then_crash(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                raise SimulatedRotationCrash("prepared")

            with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            marker_path = activation_pending_path(source)
            alias_path = root / "journal-alias.json"
            os.link(marker_path, alias_path)
            marker_bytes = marker_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "journal"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(alias_path.read_bytes(), marker_bytes)
            self.assertEqual(marker_path.stat().st_nlink, 2)

    def test_recovery_does_not_remove_journal_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def write_then_crash(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                raise SimulatedRotationCrash("prepared")

            with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            marker_path = activation_pending_path(source)
            saved_marker_path = root / "saved-authenticated-journal.json"
            replacement_bytes = b"unrelated-journal-replacement"
            original_read = rotation._read_rotation_journal
            swapped = False

            def swap_after_read(path: Path) -> object:
                nonlocal swapped
                result = original_read(path)
                path.replace(saved_marker_path)
                path.write_bytes(replacement_bytes)
                swapped = True
                return result

            with patch.object(rotation, "_read_rotation_journal", side_effect=swap_after_read):
                with self.assertRaisesRegex(ValueError, "journal|integrity|changed"):
                    rotation.recover_pending_rotation(
                        source=source,
                        segments_directory=segments_directory,
                        apply=True,
                    )

            self.assertTrue(swapped)
            self.assertEqual(marker_path.read_bytes(), replacement_bytes)
            self.assertTrue(saved_marker_path.is_file())

    def test_candidate_recovery_rejects_immutable_segment_sqlite_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def crash_after_catalog_replace(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                if payload.get("phase") == "catalog-replaced":
                    raise SimulatedRotationCrash("catalog-replaced")

            with patch.object(rotation, "_write_rotation_journal", side_effect=crash_after_catalog_replace):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            sidecar_path = Path(f"{segments_directory / 'segment-0002.sqlite3'}-wal")
            sidecar_path.write_bytes(b"unauthenticated-sidecar")
            marker_path = activation_pending_path(source)

            with self.assertRaisesRegex(ValueError, "sidecar|integrity"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertTrue(sidecar_path.is_file())
            self.assertTrue(marker_path.is_file())

    def test_preexisting_reader_refuses_scope_reads_after_activation_begins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=source,
                catalog_path=segments_directory / "catalog.json",
            )
            marker_path = activation_pending_path(source)
            marker_path.write_text("pending", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "activation"):
                reader.list_scopes(include_activity_counts=False)

    def test_preexisting_reader_refuses_segment_sidecar_created_after_catalog_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=source,
                catalog_path=segments_directory / "catalog.json",
            )
            sidecar_path = Path(f"{segments_directory / 'segment-0001.sqlite3'}-shm")
            sidecar_path.write_bytes(b"late-sidecar")

            with self.assertRaisesRegex(ValueError, "sidecar"):
                reader.verify_catalog_segments()

    def test_recovery_reverifies_every_prior_segment_before_finalizing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def crash_after_catalog_replace(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                if payload.get("phase") == "catalog-replaced":
                    raise SimulatedRotationCrash("catalog-replaced")

            with patch.object(rotation, "_write_rotation_journal", side_effect=crash_after_catalog_replace):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            prior_segment = segments_directory / "segment-0001.sqlite3"
            prior_segment.write_bytes(b"tampered-prior-segment")
            tampered_sha256 = self._sha256_file(prior_segment)
            marker_path = activation_pending_path(source)

            with self.assertRaisesRegex(ValueError, "integrity"):
                rotation.recover_pending_rotation(
                    source=source,
                    segments_directory=segments_directory,
                    apply=True,
                )

            self.assertEqual(self._sha256_file(prior_segment), tampered_sha256)
            self.assertTrue(marker_path.is_file())

    def test_recovery_does_not_unlink_replacement_at_authenticated_cleanup_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            original_write_journal = rotation._write_rotation_journal

            def write_then_crash(path: Path, payload: dict[str, object]) -> None:
                original_write_journal(path, payload)
                raise SimulatedRotationCrash("prepared")

            with patch.object(rotation, "_write_rotation_journal", side_effect=write_then_crash):
                with self.assertRaises(SimulatedRotationCrash):
                    rotation.rotate_segmented_history(
                        source=source,
                        segments_directory=segments_directory,
                        cutoff="2025-01-03T00:00:00+00:00",
                        key_id="generation-key-2",
                        scheduled_backup_directory=backup_directory,
                        scheduled_backup_status_path=backup_status_path,
                        apply=True,
                    )

            journal = json.loads(activation_pending_path(source).read_text(encoding="utf-8"))
            rollback_path = source.parent / journal["prior_hot_rollback"]["file_name"]
            saved_rollback_path = root / "saved-authenticated-rollback.sqlite3"
            replacement_bytes = b"unrelated-replacement-must-survive"
            original_replace = rotation.os.replace
            swapped = False

            def swap_before_retirement(
                source_path: str | os.PathLike[str],
                destination_path: str | os.PathLike[str],
            ) -> None:
                nonlocal swapped
                if Path(source_path) == rollback_path and not swapped:
                    original_replace(rollback_path, saved_rollback_path)
                    rollback_path.write_bytes(replacement_bytes)
                    swapped = True
                original_replace(source_path, destination_path)

            with patch.object(rotation.os, "replace", side_effect=swap_before_retirement):
                with self.assertRaisesRegex(ValueError, "integrity|changed"):
                    rotation.recover_pending_rotation(
                        source=source,
                        segments_directory=segments_directory,
                        apply=True,
                    )

            self.assertTrue(swapped)
            self.assertEqual(rollback_path.read_bytes(), replacement_bytes)
            self.assertTrue(saved_rollback_path.is_file())
            self.assertTrue(activation_pending_path(source).is_file())

    def test_later_rotation_snaps_cutoff_for_segment_and_hot_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T06:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)

            self._rotation_module().rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T12:34:56+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=backup_status_path,
                apply=True,
            )

            catalog = json.loads((segments_directory / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["segments"][1]["sealed_at"], "2025-01-03T00:00:00+00:00")
            self.assertEqual(
                self._event_types(segments_directory / "segment-0002.sqlite3"),
                ["generation-1-hot", "generation-2-sealed"],
            )
            self.assertEqual(self._event_types(source), ["generation-2-hot"])

    def test_rotated_generation_exports_imports_and_queries_as_schema_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T00:00:00+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=backup_status_path,
                apply=True,
            )
            catalog_path = segments_directory / "catalog.json"
            source_settings = HistorySettings(
                sqlite_path=str(source),
                segment_catalog_path=str(catalog_path),
                backup_dir=str(root / "backups"),
                startup_grace_seconds=0,
            )
            source_service = SystemBackupService(
                source_settings,
                HistoryStore(
                    str(source),
                    recover_unreadable_database=False,
                    segment_catalog_path=catalog_path,
                ),
            )

            artifact = source_service.export_bundle_to_file(
                encrypt=False,
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            try:
                self.assertEqual(artifact.manifest["schema_version"], 2)
                self.assertEqual(
                    [
                        segment["segment_id"]
                        for segment in artifact.manifest["history_catalog"]["segments"]
                    ],
                    ["segment-0001", "segment-0002"],
                )
                target_root = root / "import-target"
                target_root.mkdir()
                target_source = target_root / "history.db"
                with sqlite3.connect(target_source) as connection:
                    connection.executescript(SCHEMA)
                target_catalog = target_root / "segments" / "catalog.json"
                target_settings = HistorySettings(
                    sqlite_path=str(target_source),
                    segment_catalog_path=str(target_catalog),
                    backup_dir=str(target_root / "backups"),
                    startup_grace_seconds=0,
                )
                target_store = HistoryStore(
                    str(target_source),
                    recover_unreadable_database=False,
                    segment_catalog_path=target_catalog,
                )
                target_service = SystemBackupService(target_settings, target_store)

                result = target_service.import_bundle(artifact.path.read_bytes())

                self.assertEqual(result["schema_version"], 2)
                self.assertTrue((target_catalog.parent / "segment-0001.sqlite3").is_file())
                self.assertTrue((target_catalog.parent / "segment-0002.sqlite3").is_file())
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in target_store.list_slot_events(
                            "synthetic-system",
                            "synthetic-enclosure",
                            1,
                        )
                    ],
                    [
                        "generation-2-hot",
                        "generation-2-sealed",
                        "generation-1-hot",
                        "generation-1-sealed",
                    ],
                )
            finally:
                artifact.cleanup()

    def test_hot_checkpoint_guard_refuses_pending_wal_frames_and_folds_them_once_readers_leave(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hot_path = Path(temporary_directory) / "history.db"
            wal_path = Path(f"{hot_path}-wal")

            _ImportActivationTransaction._checkpoint_hot_database(hot_path)  # missing hot file: nothing to do

            writer = sqlite3.connect(hot_path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE t (x INTEGER)")
            writer.commit()
            reader = sqlite3.connect(hot_path)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT count(*) FROM t").fetchone()  # hold a WAL snapshot
                writer.execute("INSERT INTO t VALUES (1)")
                writer.commit()
                self.assertGreater(wal_path.stat().st_size, 0)

                with _short_checkpoint_busy_wait(), self.assertRaisesRegex(ValueError, "cannot be checkpointed"):
                    _ImportActivationTransaction._checkpoint_hot_database(hot_path)
                self.assertGreater(wal_path.stat().st_size, 0)

                reader.rollback()
            finally:
                reader.close()

            with self.assertRaisesRegex(ValueError, "sidecars remain"):
                _ImportActivationTransaction._checkpoint_hot_database(hot_path)
            self.assertEqual(wal_path.stat().st_size, 0)
            self.assertEqual(writer.execute("SELECT count(*) FROM t").fetchone()[0], 1)
            writer.close()
            _ImportActivationTransaction._checkpoint_hot_database(hot_path)
            with sqlite3.connect(hot_path) as verification_connection:
                self.assertEqual(
                    verification_connection.execute("SELECT count(*) FROM t").fetchone()[0],
                    1,
                )

            Path(f"{hot_path}-journal").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "rollback journal"):
                _ImportActivationTransaction._checkpoint_hot_database(hot_path)

    def test_schema_v2_restore_refuses_a_hot_database_with_live_sidecars(self) -> None:
        # The v2 restore is a plain file replace. Committed frames still in the
        # live WAL would be replayed over the restored hot file by the next
        # connection, and blindly unlinking the WAL would strip the parked
        # original of frames a rollback needs. The restore must checkpoint
        # first and refuse while a reader keeps frames pending (issue #175).
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "history.db"
            segments_directory = root / "segments"
            self._create_generation_0001(source, segments_directory)
            self._append_event(source, "generation-2-sealed", "2025-01-02T12:00:00+00:00")
            self._append_event(source, "generation-2-hot", "2025-01-03T12:00:00+00:00")
            backup_directory, backup_status_path = self._create_full_backup_evidence(root)
            rotation = self._rotation_module()
            rotation.rotate_segmented_history(
                source=source,
                segments_directory=segments_directory,
                cutoff="2025-01-03T00:00:00+00:00",
                key_id="generation-key-2",
                scheduled_backup_directory=backup_directory,
                scheduled_backup_status_path=backup_status_path,
                apply=True,
            )
            catalog_path = segments_directory / "catalog.json"
            source_settings = HistorySettings(
                sqlite_path=str(source),
                segment_catalog_path=str(catalog_path),
                backup_dir=str(root / "backups"),
                startup_grace_seconds=0,
            )
            source_service = SystemBackupService(
                source_settings,
                HistoryStore(
                    str(source),
                    recover_unreadable_database=False,
                    segment_catalog_path=catalog_path,
                ),
            )

            artifact = source_service.export_bundle_to_file(
                encrypt=False,
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            try:
                target_root = root / "import-target"
                target_root.mkdir()
                target_source = target_root / "history.db"
                with sqlite3.connect(target_source) as connection:
                    connection.executescript(SCHEMA)
                target_catalog = target_root / "segments" / "catalog.json"
                target_settings = HistorySettings(
                    sqlite_path=str(target_source),
                    segment_catalog_path=str(target_catalog),
                    backup_dir=str(target_root / "backups"),
                    startup_grace_seconds=0,
                )
                target_store = HistoryStore(
                    str(target_source),
                    recover_unreadable_database=False,
                    segment_catalog_path=target_catalog,
                )
                target_service = SystemBackupService(target_settings, target_store)
                wal_path = Path(f"{target_source}-wal")
                reader = sqlite3.connect(target_source)
                try:
                    reader.execute("BEGIN")
                    reader.execute("SELECT count(*) FROM slot_events").fetchone()  # hold a WAL snapshot
                    target_store._execute_write(
                        lambda connection: connection.execute(
                            """
                            INSERT INTO slot_events (
                                id, observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (7, "2025-01-04T00:00:00+00:00", "target-system", "target-enclosure", 1, "slot-1", "live", "{}"),
                        )
                    )
                    self.assertGreater(wal_path.stat().st_size, 0)

                    with _short_checkpoint_busy_wait(), self.assertRaisesRegex(ValueError, "cannot be checkpointed"):
                        target_service.import_bundle(artifact.path.read_bytes())

                    self.assertGreater(wal_path.stat().st_size, 0)
                    self.assertFalse(target_catalog.exists())
                    self.assertFalse(activation_pending_path(target_source).exists())
                    with sqlite3.connect(target_source) as verification_connection:
                        self.assertEqual(
                            [
                                row[0]
                                for row in verification_connection.execute(
                                    """
                                    SELECT event_type
                                    FROM slot_events
                                    WHERE system_id = ? AND enclosure_key = ? AND slot = ?
                                    ORDER BY id
                                    """,
                                    ("target-system", "target-enclosure", 1),
                                )
                            ],
                            ["live"],
                        )
                    reader.rollback()
                finally:
                    reader.close()

                with sqlite3.connect(target_source) as checkpoint_connection:
                    checkpoint = checkpoint_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                self.assertEqual(tuple(int(value) for value in checkpoint), (0, 0, 0))
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{target_source}{suffix}").unlink(missing_ok=True)
                result = target_service.import_bundle(artifact.path.read_bytes())

                self.assertEqual(result["schema_version"], 2)
                self.assertFalse(wal_path.exists())
                self.assertFalse(Path(f"{target_source}-shm").exists())
                self.assertTrue((target_catalog.parent / "segment-0002.sqlite3").is_file())
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in target_store.list_slot_events("synthetic-system", "synthetic-enclosure", 1)
                    ],
                    ["generation-2-hot", "generation-2-sealed", "generation-1-hot", "generation-1-sealed"],
                )
            finally:
                artifact.cleanup()

    def _rotation_module(self) -> ModuleType:
        module_name = "history_service.segment_rotation"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            "later-generation segment rotation is not implemented",
        )
        return importlib.import_module(module_name)

    @staticmethod
    def _run_rotation_cli(
        source: Path,
        segments_directory: Path,
        backup_directory: Path,
        backup_status_path: Path,
        *,
        apply: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "scripts/rotate_segmented_history.py",
            "--source",
            str(source),
            "--segments-dir",
            str(segments_directory),
            "--cutoff",
            "2025-01-03T00:00:00+00:00",
            "--key-id",
            "generation-key-2",
            "--scheduled-backup-dir",
            str(backup_directory),
            "--scheduled-backup-status",
            str(backup_status_path),
        ]
        if apply:
            command.append("--apply")
        return subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            check=False,
            text=True,
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb", buffering=0) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _event_types(path: Path) -> list[str]:
        with sqlite3.connect(path) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT event_type FROM slot_events ORDER BY julianday(observed_at), id"
                )
            ]

    def _create_generation_0001(self, source: Path, segments_directory: Path) -> None:
        with sqlite3.connect(source) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT INTO slot_state_current (
                    system_id, enclosure_key, slot, slot_label, present,
                    identify_active, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "synthetic-system",
                    "synthetic-enclosure",
                    1,
                    "slot-1",
                    1,
                    0,
                    "2025-01-02T00:00:00+00:00",
                ),
            )
            for event_type, observed_at in (
                ("generation-1-sealed", "2025-01-01T00:00:00+00:00"),
                ("generation-1-hot", "2025-01-02T00:00:00+00:00"),
            ):
                self._insert_event(connection, event_type, observed_at)
        segment_migration.migrate_segmented_history(
            source=source,
            segments_directory=segments_directory,
            cutoff="2025-01-02T00:00:00+00:00",
            key_id="generation-key-1",
            apply=True,
        )

    def _append_event(
        self,
        source: Path,
        event_type: str,
        observed_at: str,
        *,
        journal_mode: str = "DELETE",
    ) -> None:
        connection = sqlite3.connect(source)
        try:
            connection.execute(f"PRAGMA journal_mode={journal_mode}")
            with connection:
                self._insert_event(connection, event_type, observed_at)
        finally:
            connection.close()

    def _create_service_wal_hot(self, source: Path) -> None:
        """Build a hot database the way the service leaves it: WAL header, closed cleanly, no sidecars."""
        connection = sqlite3.connect(source)
        try:
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                self._insert_event(connection, "generation-2-hot", "2025-01-03T12:00:00+00:00")
        finally:
            connection.close()
        self.assertEqual(source.read_bytes()[18:20], b"\x02\x02")
        self._assert_no_sqlite_sidecars(source)

    def _assert_no_sqlite_sidecars(self, source: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(f"{source}{suffix}").exists(), f"{source.name}{suffix} exists")

    @staticmethod
    def _insert_event(connection: sqlite3.Connection, event_type: str, observed_at: str) -> None:
        connection.execute(
            """
            INSERT INTO slot_events (
                observed_at, system_id, enclosure_key, slot, slot_label,
                event_type, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observed_at,
                "synthetic-system",
                "synthetic-enclosure",
                1,
                "slot-1",
                event_type,
                "{}",
            ),
        )

    def _create_full_backup_evidence(
        self,
        root: Path,
        *,
        nonce: str = "deadbeef",
        observed_at: datetime | None = None,
    ) -> tuple[Path, Path]:
        backup_directory = root / "scheduled-backups"
        backup_directory.mkdir(mode=0o700, exist_ok=True)
        artifact_name = f"jbod-scheduled-backup-20260901T120000Z-{nonce}.7z"
        artifact_path = backup_directory / artifact_name
        artifact_path.write_bytes(f"synthetic-encrypted-full-backup-{nonce}".encode("ascii"))
        artifact_sha256 = self._sha256_file(artifact_path)
        observed_at_text = (observed_at or datetime.now(timezone.utc)).isoformat()
        status_path = root / f"scheduled-backup-status-{nonce}.json"
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "included_groups": ["history_db"],
                    "success_count": 1,
                    "failure_count": 0,
                    "last_attempt_at": observed_at_text,
                    "last_success_at": observed_at_text,
                    "last_failure_at": None,
                    "last_size_bytes": artifact_path.stat().st_size,
                    "last_sha256": artifact_sha256,
                    "last_artifact_name": artifact_name,
                    "last_absent_groups": [],
                    "last_retention_removed": 0,
                    "last_error_code": None,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(status_path, 0o600)
        return backup_directory, status_path
