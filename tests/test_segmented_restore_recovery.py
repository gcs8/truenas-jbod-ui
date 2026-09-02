from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from history_service import segment_migration
from history_service.config import HistorySettings
from history_service.segment_catalog import activation_pending_path
from history_service.segment_rotation import recover_pending_rotation
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import (
    HISTORY_DB_KEY,
    SystemBackupService,
    _ImportActivationTransaction,
)


class SimulatedRestoreCrash(BaseException):
    pass


class SegmentedRestoreRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._import_roots_before = set(
            Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*")
        )

    def tearDown(self) -> None:
        for import_root in (
            set(Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*"))
            - self._import_roots_before
        ):
            shutil.rmtree(import_root, ignore_errors=True)

    def test_public_restore_refuses_candidate_mutation_after_marker_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source, source_segments = self._create_generation(
                root / "source",
                "candidate",
            )
            target, target_segments = self._create_generation(
                root / "target",
                "prior",
            )
            source_service = self._service(source, source_segments)
            target_service = self._service(target, target_segments)
            artifact = source_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            original_create_marker = SystemBackupService._create_segmented_activation_marker
            staged_hot_path: Path | None = None

            def mutate_candidate_after_marker(store: HistoryStore, payload: dict[str, Any]):
                nonlocal staged_hot_path
                marker = original_create_marker(store, payload)
                candidate_path = target.parent / payload["hot"]["staged_name"]
                staged_hot_path = candidate_path
                candidate_path.write_bytes(b"post-marker-divergent-candidate")
                return marker

            try:
                with patch.object(
                    SystemBackupService,
                    "_create_segmented_activation_marker",
                    side_effect=mutate_candidate_after_marker,
                ):
                    with self.assertRaisesRegex(RuntimeError, "preserv|incomplete|integrity"):
                        target_service.import_bundle_from_file(artifact.path)

                self.assertIsNotNone(staged_hot_path)
                assert staged_hot_path is not None
                self.assertEqual(staged_hot_path.read_bytes(), b"post-marker-divergent-candidate")
                self.assertTrue(activation_pending_path(target).is_file())
                self.assertEqual(self._event_types(target), ["prior-hot"])
            finally:
                artifact.cleanup()

    def test_public_restore_refuses_sidecar_created_after_marker_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source, source_segments = self._create_generation(
                root / "source",
                "candidate",
            )
            target, target_segments = self._create_generation(
                root / "target",
                "prior",
            )
            source_service = self._service(source, source_segments)
            target_service = self._service(target, target_segments)
            artifact = source_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            original_create_marker = SystemBackupService._create_segmented_activation_marker
            sidecar = Path(f"{target}-wal")

            def create_sidecar_after_marker(store: HistoryStore, payload: dict[str, Any]):
                marker = original_create_marker(store, payload)
                sidecar.write_bytes(b"post-marker-untrusted-sidecar")
                return marker

            try:
                with patch.object(
                    SystemBackupService,
                    "_create_segmented_activation_marker",
                    side_effect=create_sidecar_after_marker,
                ):
                    with self.assertRaisesRegex(RuntimeError, "preserv|incomplete|sidecar"):
                        target_service.import_bundle_from_file(artifact.path)

                self.assertEqual(sidecar.read_bytes(), b"post-marker-untrusted-sidecar")
                self.assertTrue(activation_pending_path(target).is_file())
                self.assertEqual(self._event_types(target), ["prior-hot"])
            finally:
                artifact.cleanup()

    def test_public_restore_refuses_exact_copy_of_parked_prior_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source, source_segments = self._create_generation(
                root / "source",
                "candidate",
            )
            target, target_segments = self._create_generation(
                root / "target",
                "prior",
            )
            source_service = self._service(source, source_segments)
            target_service = self._service(target, target_segments)
            artifact = source_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            original_commit = _ImportActivationTransaction.commit
            replaced_previous_path: Path | None = None

            def replace_parked_prior_with_exact_copy(transaction: _ImportActivationTransaction) -> None:
                nonlocal replaced_previous_path
                previous_paths = list(target.parent.glob(f".{target.name}.previous-*"))
                self.assertEqual(len(previous_paths), 1)
                previous_path = previous_paths[0]
                replacement_path = previous_path.with_name(f"{previous_path.name}.replacement")
                shutil.copy2(previous_path, replacement_path)
                os.replace(replacement_path, previous_path)
                replaced_previous_path = previous_path
                original_commit(transaction)

            try:
                with patch.object(
                    _ImportActivationTransaction,
                    "commit",
                    autospec=True,
                    side_effect=replace_parked_prior_with_exact_copy,
                ):
                    with self.assertRaisesRegex(RuntimeError, "preserv|incomplete|integrity"):
                        target_service.import_bundle_from_file(artifact.path)

                self.assertIsNotNone(replaced_previous_path)
                assert replaced_previous_path is not None
                self.assertTrue(replaced_previous_path.is_file())
                self.assertTrue(activation_pending_path(target).is_file())
                self.assertEqual(self._event_types(target), ["candidate-hot"])
            finally:
                artifact.cleanup()

    def test_public_restore_preflights_all_parked_prior_artifacts_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source, source_segments = self._create_generation(
                root / "source",
                "candidate",
            )
            target, target_segments = self._create_generation(
                root / "target",
                "prior",
            )
            source_service = self._service(source, source_segments)
            target_service = self._service(target, target_segments)
            artifact = source_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            original_commit = _ImportActivationTransaction.commit
            previous_hot_path: Path | None = None
            previous_segments_path: Path | None = None

            def replace_parked_segment_tree(transaction: _ImportActivationTransaction) -> None:
                nonlocal previous_hot_path, previous_segments_path
                previous_hot_paths = list(target.parent.glob(f".{target.name}.previous-*"))
                previous_segment_paths = list(
                    target_segments.parent.glob(f".{target_segments.name}.previous-*")
                )
                self.assertEqual(len(previous_hot_paths), 1)
                self.assertEqual(len(previous_segment_paths), 1)
                previous_hot_path = previous_hot_paths[0]
                previous_segments_path = previous_segment_paths[0]
                saved_tree = previous_segments_path.with_name(
                    f"{previous_segments_path.name}.saved"
                )
                os.replace(previous_segments_path, saved_tree)
                shutil.copytree(saved_tree, previous_segments_path, copy_function=shutil.copy2)
                original_commit(transaction)

            try:
                with patch.object(
                    _ImportActivationTransaction,
                    "commit",
                    autospec=True,
                    side_effect=replace_parked_segment_tree,
                ):
                    with self.assertRaisesRegex(RuntimeError, "preserv|incomplete|integrity"):
                        target_service.import_bundle_from_file(artifact.path)

                self.assertIsNotNone(previous_hot_path)
                self.assertIsNotNone(previous_segments_path)
                assert previous_hot_path is not None
                assert previous_segments_path is not None
                self.assertTrue(previous_hot_path.is_file())
                self.assertTrue(previous_segments_path.is_dir())
                self.assertTrue(activation_pending_path(target).is_file())
            finally:
                artifact.cleanup()

    def test_public_restore_recovers_after_every_post_marker_rename(self) -> None:
        expected_states = {
            1: ("prior-generation-restored", ["prior-hot"]),
            2: ("prior-generation-restored", ["prior-hot"]),
            3: ("prior-generation-restored", ["prior-hot"]),
            4: ("candidate-finalized", ["candidate-hot"]),
        }
        for crash_after, (expected_state, expected_events) in expected_states.items():
            with self.subTest(crash_after=crash_after):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    source, source_segments = self._create_generation(
                        root / "source",
                        "candidate",
                    )
                    target, target_segments = self._create_generation(
                        root / "target",
                        "prior",
                    )
                    source_service = self._service(source, source_segments)
                    target_service = self._service(target, target_segments)
                    artifact = source_service.export_bundle_to_file(
                        packaging="zip",
                        included_paths=[HISTORY_DB_KEY],
                    )
                    original_replace = os.replace
                    replace_count = 0
                    import_roots_before = set(Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*"))

                    def crash_after_restore_rename(source_path, target_path, *args, **kwargs):
                        nonlocal replace_count
                        source_path = Path(source_path)
                        target_path = Path(target_path)
                        result = original_replace(source_path, target_path, *args, **kwargs)
                        if target_path.parent == target.parent and (
                            target_path in {target, target_segments}
                            or target_path.name.startswith(f".{target.name}.previous-")
                            or target_path.name.startswith(f".{target_segments.name}.previous-")
                        ):
                            replace_count += 1
                            if replace_count == crash_after:
                                raise SimulatedRestoreCrash(f"restore-rename-{crash_after}")
                        return result

                    try:
                        with (
                            patch.object(
                                _ImportActivationTransaction,
                                "__exit__",
                                return_value=False,
                            ),
                            patch(
                                "history_service.system_backup.os.replace",
                                side_effect=crash_after_restore_rename,
                            ),
                        ):
                            with self.assertRaises(SimulatedRestoreCrash):
                                target_service.import_bundle_from_file(artifact.path)

                        self.assertEqual(replace_count, crash_after)
                        self.assertTrue(activation_pending_path(target).is_file())

                        result = recover_pending_rotation(
                            source=target,
                            segments_directory=target_segments,
                            apply=True,
                        )

                        self.assertEqual(result["recovery_state"], expected_state)
                        self.assertEqual(self._event_types(target), expected_events)
                        self.assertFalse(activation_pending_path(target).exists())
                        self.assertFalse(
                            any(target.parent.glob(f".{target.name}.restore-*"))
                        )
                        self.assertFalse(
                            any(target.parent.glob(f".{target.name}.previous-*"))
                        )
                        self.assertFalse(
                            any(target.parent.glob(f".{target_segments.name}.restore-*"))
                        )
                        self.assertFalse(
                            any(target.parent.glob(f".{target_segments.name}.previous-*"))
                        )
                    finally:
                        artifact.cleanup()
                        for import_root in (
                            set(Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*"))
                            - import_roots_before
                        ):
                            shutil.rmtree(import_root, ignore_errors=True)

    def test_schema_v2_staging_crash_happens_before_activation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            target_root = root / "target"
            source, source_segments = self._create_generation(source_root, "candidate")
            target, target_segments = self._create_generation(target_root, "prior")
            target_events = self._event_types(target)
            target_catalog_bytes = (target_segments / "catalog.json").read_bytes()
            source_service = self._service(source, source_segments)
            target_service = self._service(target, target_segments)
            artifact = source_service.export_bundle_to_file(
                packaging="zip",
                included_paths=[HISTORY_DB_KEY],
            )
            original_copyfile = shutil.copyfile

            def crash_during_adjacent_stage(source_path, target_path, *args, **kwargs):
                target_path = Path(target_path)
                if target_path.name.startswith(f".{target.name}.restore-"):
                    target_path.write_bytes(b"partial-candidate")
                    raise SimulatedRestoreCrash("candidate-hot-stage")
                return original_copyfile(source_path, target_path, *args, **kwargs)

            try:
                with patch(
                    "history_service.system_backup.shutil.copyfile",
                    side_effect=crash_during_adjacent_stage,
                ):
                    with self.assertRaises(SimulatedRestoreCrash):
                        target_service.import_bundle_from_file(artifact.path)

                self.assertFalse(activation_pending_path(target).exists())
                self.assertEqual(self._event_types(target), target_events)
                self.assertEqual((target_segments / "catalog.json").read_bytes(), target_catalog_bytes)
            finally:
                artifact.cleanup()

    def test_restore_recovery_restores_prior_from_mixed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(candidate_root, "candidate")
            marker = self._prepare_marker(source, segments, candidate_source, candidate_segments)
            prior_hot_sha256 = self._sha256(source)
            prior_catalog_sha256 = self._sha256(segments / "catalog.json")

            hot = marker["hot"]
            staged_hot = source.parent / hot["staged_name"]
            previous_hot = source.parent / hot["previous_name"]
            os.replace(source, previous_hot)
            os.replace(staged_hot, source)
            marker["phase"] = "hot-replaced"
            self._write_marker(source, marker)

            result = self._recover_or_fail(source, segments)

            self.assertEqual(result["recovery_state"], "prior-generation-restored")
            self.assertEqual(self._sha256(source), prior_hot_sha256)
            self.assertEqual(self._sha256(segments / "catalog.json"), prior_catalog_sha256)
            self.assertFalse(previous_hot.exists())
            self.assertFalse((segments.parent / marker["segments"]["staged_name"]).exists())
            self.assertFalse(activation_pending_path(source).exists())

    def test_restore_recovery_finalizes_fully_active_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(candidate_root, "candidate")
            marker = self._prepare_marker(source, segments, candidate_source, candidate_segments)
            candidate_hot_sha256 = marker["hot"]["candidate"]["sha256"]
            candidate_catalog_sha256 = next(
                item["sha256"]
                for item in marker["segments"]["candidate"]["files"]
                if item["path"] == "catalog.json"
            )

            hot = marker["hot"]
            segment_tree = marker["segments"]
            previous_hot = source.parent / hot["previous_name"]
            previous_segments = segments.parent / segment_tree["previous_name"]
            os.replace(source, previous_hot)
            os.replace(source.parent / hot["staged_name"], source)
            os.replace(segments, previous_segments)
            os.replace(segments.parent / segment_tree["staged_name"], segments)
            marker["phase"] = "segments-replaced"
            self._write_marker(source, marker)

            result = self._recover_or_fail(source, segments)

            self.assertEqual(result["recovery_state"], "candidate-finalized")
            self.assertEqual(self._sha256(source), candidate_hot_sha256)
            self.assertEqual(self._sha256(segments / "catalog.json"), candidate_catalog_sha256)
            self.assertFalse(previous_hot.exists())
            self.assertFalse(previous_segments.exists())
            self.assertFalse(activation_pending_path(source).exists())

    def test_restore_recovery_refuses_divergent_staged_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(candidate_root, "candidate")
            marker = self._prepare_marker(source, segments, candidate_source, candidate_segments)
            staged_hot = source.parent / marker["hot"]["staged_name"]
            staged_hot.write_bytes(b"divergent-candidate")
            self._write_marker(source, marker)

            with self.assertRaisesRegex(ValueError, "restore|integrity|divergent"):
                recover_pending_rotation(
                    source=source,
                    segments_directory=segments,
                    apply=True,
                )

            self.assertTrue(activation_pending_path(source).is_file())
            self.assertEqual(staged_hot.read_bytes(), b"divergent-candidate")

    def test_restore_recovery_refuses_a_candidate_generation_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(
                candidate_root,
                "candidate",
            )
            marker = self._prepare_marker(
                source,
                segments,
                candidate_source,
                candidate_segments,
            )
            marker["generation_id"] = "different-authenticated-generation"

            hot = marker["hot"]
            segment_tree = marker["segments"]
            os.replace(source, source.parent / hot["previous_name"])
            os.replace(source.parent / hot["staged_name"], source)
            os.replace(segments, segments.parent / segment_tree["previous_name"])
            os.replace(
                segments.parent / segment_tree["staged_name"],
                segments,
            )
            self._write_marker(source, marker)

            with self.assertRaisesRegex(ValueError, "generation"):
                recover_pending_rotation(
                    source=source,
                    segments_directory=segments,
                    apply=True,
                )

            self.assertTrue(activation_pending_path(source).is_file())
            self.assertTrue((source.parent / hot["previous_name"]).is_file())
            self.assertTrue((segments.parent / segment_tree["previous_name"]).is_dir())

    def test_restore_recovery_preserves_a_journal_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(
                candidate_root,
                "candidate",
            )
            marker = self._prepare_marker(
                source,
                segments,
                candidate_source,
                candidate_segments,
            )
            marker_path = activation_pending_path(source)
            self._write_marker(source, marker)
            saved_marker_path = root / "saved-authenticated-restore-journal.json"
            replacement_bytes = b"unrelated-restore-journal-replacement"
            from history_service import segment_rotation as rotation

            original_read = rotation.read_activation_journal
            swapped = False

            def swap_after_read(path: Path):
                nonlocal swapped
                result = original_read(path)
                path.replace(saved_marker_path)
                path.write_bytes(replacement_bytes)
                path.chmod(0o600)
                swapped = True
                return result

            with patch.object(
                rotation,
                "read_activation_journal",
                side_effect=swap_after_read,
            ):
                with self.assertRaisesRegex(ValueError, "journal|integrity"):
                    recover_pending_rotation(
                        source=source,
                        segments_directory=segments,
                        apply=True,
                    )

            self.assertTrue(swapped)
            self.assertEqual(marker_path.read_bytes(), replacement_bytes)
            self.assertTrue(saved_marker_path.is_file())
            self.assertEqual(self._event_types(source), ["prior-hot"])

    def test_restore_recovery_preserves_an_exact_copy_journal_replacement_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(
                candidate_root,
                "candidate",
            )
            marker = self._prepare_marker(
                source,
                segments,
                candidate_source,
                candidate_segments,
            )
            marker_path = activation_pending_path(source)
            self._write_marker(source, marker)
            saved_marker_path = root / "saved-authenticated-restore-journal.json"
            from history_service import segment_rotation as rotation

            original_read = rotation.read_activation_journal
            swapped = False

            def swap_exact_copy_after_read(path: Path):
                nonlocal swapped
                result = original_read(path)
                path.replace(saved_marker_path)
                shutil.copy2(saved_marker_path, path)
                swapped = True
                return result

            with patch.object(
                rotation,
                "read_activation_journal",
                side_effect=swap_exact_copy_after_read,
            ):
                with self.assertRaisesRegex(ValueError, "journal|integrity"):
                    recover_pending_rotation(
                        source=source,
                        segments_directory=segments,
                        apply=True,
                    )

            self.assertTrue(swapped)
            self.assertTrue(marker_path.is_file())
            self.assertTrue(saved_marker_path.is_file())

    def test_restore_recovery_refuses_sidecar_beside_missing_prior_hot_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            live_root = root / "live"
            candidate_root = root / "candidate"
            source, segments = self._create_generation(live_root, "prior")
            candidate_source, candidate_segments = self._create_generation(
                candidate_root,
                "candidate",
            )
            marker = self._prepare_marker(
                source,
                segments,
                candidate_source,
                candidate_segments,
            )
            source.unlink()
            shutil.rmtree(segments)
            marker["hot"]["prior"] = {"kind": "missing"}
            marker["segments"]["prior"] = {"kind": "missing"}
            sidecar = Path(f"{source}-wal")
            sidecar.write_bytes(b"untrusted-stale-sidecar")
            self._write_marker(source, marker)

            with self.assertRaisesRegex(ValueError, "sidecar|integrity|restore"):
                recover_pending_rotation(
                    source=source,
                    segments_directory=segments,
                    apply=True,
                )

            self.assertTrue(activation_pending_path(source).is_file())
            self.assertEqual(sidecar.read_bytes(), b"untrusted-stale-sidecar")

    def _recover_or_fail(self, source: Path, segments: Path) -> dict[str, Any]:
        try:
            return recover_pending_rotation(
                source=source,
                segments_directory=segments,
                apply=True,
            )
        except ValueError as exc:
            self.fail(f"segmented restore recovery unexpectedly failed: {exc}")

    def _prepare_marker(
        self,
        source: Path,
        segments: Path,
        candidate_source: Path,
        candidate_segments: Path,
    ) -> dict[str, Any]:
        transaction_id = "restore-test-0001"
        staged_hot_name = f".{source.name}.restore-{transaction_id}"
        previous_hot_name = f".{source.name}.previous-{transaction_id}"
        staged_segments_name = f".{segments.name}.restore-{transaction_id}"
        previous_segments_name = f".{segments.name}.previous-{transaction_id}"
        staged_hot = source.parent / staged_hot_name
        staged_segments = segments.parent / staged_segments_name
        shutil.copy2(candidate_source, staged_hot)
        shutil.copytree(candidate_segments, staged_segments)
        return {
            "journal_version": 1,
            "operation": "segmented-restore",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "generation_id": json.loads(
                (candidate_segments / "catalog.json").read_text(encoding="utf-8")
            )["generation_id"],
            "hot": {
                "target_name": source.name,
                "staged_name": staged_hot_name,
                "previous_name": previous_hot_name,
                "prior": self._file_record(source),
                "candidate": self._file_record(staged_hot),
            },
            "segments": {
                "target_name": segments.name,
                "staged_name": staged_segments_name,
                "previous_name": previous_segments_name,
                "prior": self._tree_record(segments),
                "candidate": self._tree_record(staged_segments),
            },
        }

    @staticmethod
    def _write_marker(source: Path, payload: dict[str, Any]) -> None:
        marker_path = activation_pending_path(source)
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        marker_path.write_bytes(content)
        marker_path.chmod(0o600)

    @classmethod
    def _file_record(cls, path: Path) -> dict[str, Any]:
        metadata = path.stat(follow_symlinks=False)
        return {
            "kind": "file",
            "sha256": cls._sha256(path),
            "size_bytes": metadata.st_size,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }

    @classmethod
    def _tree_record(cls, root: Path) -> dict[str, Any]:
        metadata = root.stat(follow_symlinks=False)
        directories = []
        files = []
        for path in sorted(root.rglob("*")):
            relative_path = path.relative_to(root).as_posix()
            item_metadata = path.stat(follow_symlinks=False)
            if path.is_dir() and not path.is_symlink():
                directories.append(
                    {
                        "path": relative_path,
                        "device": item_metadata.st_dev,
                        "inode": item_metadata.st_ino,
                        "mode": stat.S_IMODE(item_metadata.st_mode),
                        "uid": item_metadata.st_uid,
                        "gid": item_metadata.st_gid,
                    }
                )
            elif path.is_file() and not path.is_symlink():
                record = cls._file_record(path)
                record["path"] = relative_path
                files.append(record)
            else:
                raise AssertionError("restore fixture tree contains an unsupported entry")
        return {
            "kind": "directory",
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "directories": directories,
            "files": files,
        }

    @staticmethod
    def _sha256(path: Path) -> str:
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

    @classmethod
    def _create_generation(cls, root: Path, label: str) -> tuple[Path, Path]:
        root.mkdir(parents=True)
        source = root / "history.db"
        segments = root / "segments"
        with sqlite3.connect(source) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                INSERT INTO slot_state_current (
                    system_id, enclosure_key, slot, slot_label, present,
                    identify_active, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("synthetic-system", "synthetic-enclosure", 1, "slot-1", 1, 0, "2025-01-02T00:00:00+00:00"),
            )
            for event_type, observed_at in (
                (f"{label}-sealed", "2025-01-01T00:00:00+00:00"),
                (f"{label}-hot", "2025-01-02T00:00:00+00:00"),
            ):
                connection.execute(
                    """
                    INSERT INTO slot_events (
                        observed_at, system_id, enclosure_key, slot,
                        slot_label, event_type, details_json
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
        segment_migration.migrate_segmented_history(
            source=source,
            segments_directory=segments,
            cutoff="2025-01-02T00:00:00+00:00",
            key_id=f"{label}-key",
            apply=True,
        )
        return source, segments

    @staticmethod
    def _service(source: Path, segments: Path) -> SystemBackupService:
        settings = HistorySettings(
            sqlite_path=str(source),
            segment_catalog_path=str(segments / "catalog.json"),
            backup_dir=str(source.parent / "backups"),
            startup_grace_seconds=0,
        )
        return SystemBackupService(
            settings,
            HistoryStore(
                str(source),
                recover_unreadable_database=False,
                segment_catalog_path=segments / "catalog.json",
            ),
        )
