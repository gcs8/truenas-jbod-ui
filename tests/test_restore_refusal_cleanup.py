"""Restore refusal and cleanup (#1273, #1287, #1292).

Every fixture is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from history_service.segment_catalog import activation_pending_path
from history_service.segment_rotation import recover_pending_rotation
from history_service.segmented_restore import record_file as record_restore_file
from history_service.segmented_restore import record_tree as record_restore_tree
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import (
    HISTORY_DB_KEY,
    MAPPING_FILE_KEY,
    RUNTIME_OVERRIDES_FILE_KEY,
    TLS_TRUST_KEY,
    SystemBackupService,
    _ImportActivationTransaction,
)
from tests.test_interrupted_restore_recovery import (
    CANDIDATE_MAPPING,
    CANDIDATE_RUNTIME,
    CANDIDATE_TLS,
    CRASH_EXIT,
    FINISHED_EXIT,
    PASSPHRASE,
    PRIOR_RUNTIME,
    PRIOR_TLS,
    UNEXPECTED_EXIT,
    _create_history,
    _Deployment,
    _deployment,
    _hot_events,
    _tree_snapshot,
)

_ORIGINAL_RMTREE = shutil.rmtree


def _catalog_generation(segments: Path) -> str:
    return json.loads((segments / "catalog.json").read_text(encoding="utf-8"))["generation_id"]


def _import_workspaces() -> set[Path]:
    # A hard-exited import child cannot clean up its own workspaces.
    temp = Path(tempfile.gettempdir())
    return set(temp.glob("truenas-jbod-ui-import-*")) | set(
        temp.glob("truenas-jbod-ui-file-import-*")
    )


class _ImportRootCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._import_roots_before = _import_workspaces()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def tearDown(self) -> None:
        for import_root in _import_workspaces() - self._import_roots_before:
            shutil.rmtree(import_root, ignore_errors=True)


class InterruptedStagedTreeCleanupTests(_ImportRootCleanup):
    """#1273: a crash while rollback deletes the unconsumed staged segment tree."""

    artifact: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._class_directory = tempfile.TemporaryDirectory()
        source = _deployment(Path(cls._class_directory.name) / "source", "candidate")
        source.runtime.write_bytes(CANDIDATE_RUNTIME)
        source.mapping.write_bytes(CANDIDATE_MAPPING)
        source.tls.mkdir()
        (source.tls / CANDIDATE_TLS[0]).write_bytes(b"synthetic candidate trust\n")
        cls.artifact = source.service.export_bundle_to_file(
            packaging="tar.zst",
            encrypt=True,
            passphrase=PASSPHRASE,
            included_paths=[
                HISTORY_DB_KEY,
                RUNTIME_OVERRIDES_FILE_KEY,
                MAPPING_FILE_KEY,
                TLS_TRUST_KEY,
            ],
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.artifact is not None:
            cls.artifact.cleanup()
        cls._class_directory.cleanup()

    def _target(self) -> _Deployment:
        target = _deployment(Path(self._directory.name) / "target", "prior")
        target.runtime.write_bytes(PRIOR_RUNTIME)
        target.tls.mkdir()
        (target.tls / PRIOR_TLS[0]).write_bytes(b"synthetic prior trust\n")
        self.prior_generation_id = _catalog_generation(target.segments)
        return target

    def _import_in_child(
        self,
        target: _Deployment,
        install_fault: Callable[[], list[Any]],
    ) -> int:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs only in the child
            code = UNEXPECTED_EXIT
            try:
                for active in install_fault():
                    active.start()
                target.service.import_bundle_from_file(
                    self.artifact.path,
                    passphrase=PASSPHRASE,
                )
                code = FINISHED_EXIT
            except BaseException:
                code = UNEXPECTED_EXIT
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def _recover(self, target: _Deployment, *, apply: bool) -> dict[str, Any]:
        config_path = str(target.runtime.parent / "config.yaml")
        with patch.dict(os.environ, {"APP_CONFIG_PATH": config_path}):
            return recover_pending_rotation(
                source=target.hot,
                segments_directory=target.segments,
                apply=apply,
            )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_crash_inside_staged_segment_tree_delete_recovers_the_prior_generation(
        self,
    ) -> None:
        target = self._target()
        staged_prefix = f".{target.segments.name}.restore-"

        def install_fault() -> list[Any]:
            def fail_configuration_folder(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("synthetic configuration folder activation failure")

            def delete_one_file_then_exit(path: Any, *args: Any, **kwargs: Any) -> None:
                if Path(path).name.startswith(staged_prefix):
                    # Process loss part-way through deleting the staged tree:
                    # one file is gone, the rest remain.
                    for child in sorted(Path(path).rglob("*")):
                        if child.is_file() and not child.is_symlink():
                            child.unlink()
                            break
                    os._exit(CRASH_EXIT)
                _ORIGINAL_RMTREE(path, *args, **kwargs)

            return [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_directory",
                    fail_configuration_folder,
                ),
                patch("shutil.rmtree", delete_one_file_then_exit),
            ]

        exit_code = self._import_in_child(target, install_fault)

        # The child stopped inside the staged-tree delete, after history was
        # staged and journaled and before history was activated.
        self.assertEqual(exit_code, CRASH_EXIT)
        marker = activation_pending_path(target.hot)
        self.assertTrue(marker.exists())
        journal = json.loads(marker.read_text(encoding="utf-8"))
        staged_name = journal["segments"]["staged_name"]
        self.assertTrue(staged_name.startswith(staged_prefix))
        self.assertEqual(_hot_events(target.hot), ["prior-hot"])
        self.assertEqual(_catalog_generation(target.segments), self.prior_generation_id)
        staged = target.segments.parent / staged_name

        dry_run = self._recover(target, apply=False)
        self.assertEqual(dry_run["recovery_state"], "prior-generation-ready-to-restore")
        self.assertTrue(marker.exists())

        result = self._recover(target, apply=True)

        self.assertEqual(result["recovery_state"], "prior-generation-restored")
        self.assertFalse(marker.exists())
        self.assertFalse(staged.exists())
        self.assertEqual(target.runtime.read_bytes(), PRIOR_RUNTIME)
        self.assertFalse(target.mapping.exists())
        self.assertEqual(sorted(os.listdir(target.tls)), PRIOR_TLS)
        self.assertEqual(_hot_events(target.hot), ["prior-hot"])
        self.assertEqual(_catalog_generation(target.segments), self.prior_generation_id)
        # History starts on the recovered generation.
        store = HistoryStore(
            str(target.hot),
            recover_unreadable_database=False,
            segment_catalog_path=target.segments / "catalog.json",
        )
        self.assertEqual(store._segmented_reader().counts()["event_count"], 2)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_malformed_configuration_target_kind_is_refused_and_changes_nothing(
        self,
    ) -> None:
        # #1287, same class: a whole-import journal's target kind is also a
        # discriminator tested for set membership.
        target = self._target()

        def hard_exit(*_args: Any, **_kwargs: Any) -> None:
            os._exit(CRASH_EXIT)

        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction, "activate_segmented_history", hard_exit
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        marker = activation_pending_path(target.hot)
        original = marker.read_bytes()
        # Control: the journal as written reaches the prior-generation result.
        dry_run = self._recover(target, apply=False)
        self.assertEqual(dry_run["recovery_state"], "prior-generation-ready-to-restore")
        for value in ("not-a-kind", [], {}):
            journal = json.loads(original)
            self.assertTrue(journal["targets"])
            journal["targets"][0]["kind"] = value
            marker.write_bytes(
                json.dumps(journal, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            marker.chmod(0o600)
            before = _tree_snapshot(target.root)
            for apply in (False, True):
                with self.subTest(value=value, apply=apply):
                    with self.assertRaises(ValueError) as refused:
                        self._recover(target, apply=apply)
                    self.assertEqual(getattr(refused.exception, "reason", None), "journal-invalid")
                    self.assertIn("a target kind is not recognised.", str(refused.exception))
                    self.assertEqual(_tree_snapshot(target.root), before)


class MalformedJournalDiscriminatorTests(_ImportRootCleanup):
    """#1287: malformed phase and prior-kind fields refuse without a TypeError."""

    def _history_only_journal(self) -> tuple[Path, Path, dict[str, Any]]:
        root = Path(self._directory.name)
        source, segments = _create_history(root / "live", "prior")
        candidate_source, candidate_segments = _create_history(root / "candidate", "candidate")
        transaction_id = "restore-test-0001"
        staged_hot = source.parent / f".{source.name}.restore-{transaction_id}"
        staged_segments = segments.parent / f".{segments.name}.restore-{transaction_id}"
        shutil.copy2(candidate_source, staged_hot)
        shutil.copytree(candidate_segments, staged_segments)
        journal = {
            "journal_version": 1,
            "operation": "segmented-restore",
            "transaction_id": transaction_id,
            "phase": "prepared",
            "generation_id": _catalog_generation(candidate_segments),
            "hot": {
                "target_name": source.name,
                "staged_name": staged_hot.name,
                "previous_name": f".{source.name}.previous-{transaction_id}",
                "prior": record_restore_file(source),
                "candidate": record_restore_file(staged_hot),
            },
            "segments": {
                "target_name": segments.name,
                "staged_name": staged_segments.name,
                "previous_name": f".{segments.name}.previous-{transaction_id}",
                "prior": record_restore_tree(segments),
                "candidate": record_restore_tree(staged_segments),
            },
        }
        shutil.rmtree(root / "candidate")
        return source, segments, journal

    @staticmethod
    def _write_journal(source: Path, journal: dict[str, Any]) -> None:
        marker = activation_pending_path(source)
        marker.write_bytes(
            json.dumps(journal, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        marker.chmod(0o600)

    def test_malformed_discriminators_are_refused_and_change_nothing(self) -> None:
        source, segments, journal = self._history_only_journal()
        root = Path(self._directory.name)

        # Control: the well-formed journal reaches the prior-generation result.
        self._write_journal(source, journal)
        control = recover_pending_rotation(
            source=source, segments_directory=segments, apply=False
        )
        self.assertEqual(control["recovery_state"], "prior-generation-ready-to-restore")

        cases: list[tuple[str, Callable[[dict[str, Any], Any], None], Any, str]] = []
        phase_message = "Segmented restore phase is invalid."
        record_message = "Segmented restore journal artifact record is invalid."

        def set_phase(payload: dict[str, Any], value: Any) -> None:
            payload["phase"] = value

        def set_hot_kind(payload: dict[str, Any], value: Any) -> None:
            payload["hot"]["prior"]["kind"] = value

        def set_segments_kind(payload: dict[str, Any], value: Any) -> None:
            payload["segments"]["prior"]["kind"] = value

        def set_operation(payload: dict[str, Any], value: Any) -> None:
            payload["operation"] = value

        # Control: an unknown string already takes the validation-error path.
        cases.append(("phase", set_phase, "not-a-phase", phase_message))
        cases.append(
            ("operation", set_operation, "not-an-operation", "History activation journal is invalid.")
        )
        for value in ([], {}):
            cases.append(
                ("operation", set_operation, value, "History activation journal is invalid.")
            )
            cases.append(("phase", set_phase, value, phase_message))
            cases.append(("hot.prior.kind", set_hot_kind, value, record_message))
            cases.append(("segments.prior.kind", set_segments_kind, value, record_message))

        for field, mutate, value, message in cases:
            malformed = json.loads(json.dumps(journal))
            mutate(malformed, value)
            self._write_journal(source, malformed)
            before = _tree_snapshot(root)
            for apply in (False, True):
                with self.subTest(field=field, value=value, apply=apply):
                    with self.assertRaises(ValueError) as refused:
                        recover_pending_rotation(
                            source=source, segments_directory=segments, apply=apply
                        )
                    self.assertEqual(str(refused.exception), message)
                    self.assertEqual(_tree_snapshot(root), before)


def _history_database(path: Path, timestamps: list[str]) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        for index, observed_at in enumerate(timestamps):
            connection.execute(
                """
                INSERT INTO metric_samples (
                    observed_at, system_id, enclosure_key, slot, slot_label,
                    metric_name, value_integer
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    "synthetic-system",
                    "synthetic-enclosure",
                    index,
                    f"slot-{index}",
                    "temperature_c",
                    30 + index,
                ),
            )
    connection.close()
    return path


class RestoreTimestampSizeTests(_ImportRootCleanup):
    """#1292: restore admission refuses runaway timestamp text."""

    ORDINARY = "2025-01-01T00:00:00+00:00"
    MEGABYTE = 1024 * 1024
    PADDED = ORDINARY + " " * MEGABYTE
    LONG_FRACTION = "2025-01-01T00:00:00." + "1" * MEGABYTE + "+00:00"
    NANOSECONDS = "2025-01-01T00:00:00.123456789+00:00"

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)

    def test_long_fraction_fixture_is_a_parseable_timestamp(self) -> None:
        from datetime import datetime

        self.assertIsNotNone(datetime.fromisoformat(self.LONG_FRACTION).tzinfo)

    def test_history_member_validation_refuses_oversized_timestamps(self) -> None:
        control = _history_database(
            self.root / "control.sqlite3", [self.ORDINARY, self.NANOSECONDS]
        )
        SystemBackupService._validate_history_member(control)
        for label, value in (("padded", self.PADDED), ("fraction", self.LONG_FRACTION)):
            with self.subTest(label=label):
                candidate = _history_database(
                    self.root / f"{label}.sqlite3", [self.ORDINARY, value]
                )
                with self.assertRaises(ValueError) as refused:
                    SystemBackupService._validate_history_member(candidate)
                self.assertIn("metric_samples.observed_at", str(refused.exception))
                self.assertIn("Nothing was restored", str(refused.exception))

    def test_slot_event_timestamp_is_bounded_too(self) -> None:
        candidate = _history_database(self.root / "events.sqlite3", [self.ORDINARY])
        with sqlite3.connect(candidate) as connection:
            connection.execute(
                """
                INSERT INTO slot_events (
                    observed_at, system_id, enclosure_key, slot,
                    slot_label, event_type, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self.PADDED, "synthetic-system", "synthetic-enclosure", 1, "slot-1", "x", "{}"),
            )
        connection.close()
        with self.assertRaises(ValueError) as refused:
            SystemBackupService._validate_history_member(candidate)
        self.assertIn("slot_events.observed_at", str(refused.exception))

    def test_direct_restore_refuses_and_leaves_live_history_byte_identical(self) -> None:
        live = self.root / "live" / "history.sqlite3"
        live.parent.mkdir()
        store = HistoryStore(str(live), recover_unreadable_database=False)
        control = _history_database(
            self.root / "control.sqlite3", [self.ORDINARY, self.NANOSECONDS]
        )
        store.restore_backup(control)
        with closing(sqlite3.connect(f"{live.as_uri()}?mode=ro", uri=True)) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM metric_samples").fetchone()[0],
                2,
            )
        for label, value in (("padded", self.PADDED), ("fraction", self.LONG_FRACTION)):
            with self.subTest(label=label):
                candidate = _history_database(
                    self.root / f"{label}.sqlite3", [self.ORDINARY, value]
                )
                before = _tree_snapshot(live.parent)
                with self.assertRaises(ValueError) as refused:
                    store.restore_backup(candidate)
                self.assertIn("metric_samples.observed_at", str(refused.exception))
                self.assertEqual(_tree_snapshot(live.parent), before)


def _metric_rows(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT observed_at, slot, value_integer FROM metric_samples ORDER BY id"
        ).fetchall()


def _dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        return list(connection.iterdump())


class _UnsegmentedBackupService(SystemBackupService):
    def _load_app_settings(self):  # type: ignore[override]
        return self.app_settings


class RollbackSnapshotTimestampBoundTests(_ImportRootCleanup):
    """#1292: the bound guards incoming backups, never the import's own rollback.

    Earlier versions admitted longer timestamp text, so a live history can
    already hold some. A failed import must still return that history.
    """

    ORDINARY = RestoreTimestampSizeTests.ORDINARY
    OVERSIZED = ORDINARY + " " * 300

    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self._directory.name)

    def _service(self, root: Path, history: Path) -> _UnsegmentedBackupService:
        from history_service.config import HistorySettings
        from tests.test_interrupted_restore_recovery import _settings

        return _UnsegmentedBackupService(
            HistorySettings(
                sqlite_path=str(history),
                backup_dir=str(root / "backups"),
                startup_grace_seconds=0,
            ),
            HistoryStore(str(history), recover_unreadable_database=False),
            _settings(root),
        )

    def _artifact(self) -> Any:
        source_history = _history_database(
            self.root / "source.sqlite3", [self.ORDINARY, self.ORDINARY]
        )
        source = self._service(self.root / "source", source_history)
        artifact = source.export_bundle_to_file(
            packaging="tar.zst",
            encrypt=True,
            passphrase=PASSPHRASE,
            included_paths=[HISTORY_DB_KEY],
        )
        self.addCleanup(artifact.cleanup)
        return artifact

    def test_failed_import_restores_a_live_history_with_an_oversized_timestamp(
        self,
    ) -> None:
        artifact = self._artifact()
        live_dir = self.root / "live-history"
        live_dir.mkdir()
        live = _history_database(live_dir / "history.sqlite3", [self.OVERSIZED])
        target = self._service(self.root / "target", live)
        original_rows = _metric_rows(live)
        self.assertEqual(original_rows, [(self.OVERSIZED, 0, 30)])
        original_dump = _dump(live)

        activated: list[bool] = []
        real_activate = _ImportActivationTransaction.activate_history

        def activate_then_mark(transaction, store, member_key):
            real_activate(transaction, store, member_key)
            activated.append(True)

        def fail_after_history_activation():
            if activated:
                raise RuntimeError("injected settings reload failure")
            return target.app_settings

        with patch.object(
            _ImportActivationTransaction, "activate_history", activate_then_mark
        ), patch.object(
            target, "_load_app_settings", side_effect=fail_after_history_activation
        ):
            with self.assertRaises(RuntimeError) as failed:
                target.import_bundle_from_file(artifact.path, passphrase=PASSPHRASE)

        self.assertEqual(activated, [True])
        self.assertIn("injected settings reload failure", str(failed.exception))
        self.assertNotIn("Rollback failed", str(failed.exception))
        # The rollback restores the original SQL contents (rows and full
        # dump), not identical file bytes: the SQLite copy rewrites header
        # fields such as the change counter, as it always has.
        self.assertEqual(_metric_rows(live), original_rows)
        self.assertEqual(_dump(live), original_dump)
        # The imported history was published and is now gone again.
        self.assertNotIn((self.ORDINARY, 0, 30), _metric_rows(live))

    def test_public_restore_refuses_an_oversized_backup_however_it_is_called(
        self,
    ) -> None:
        (self.root / "live").mkdir()
        live = _history_database(self.root / "live" / "history.sqlite3", [self.ORDINARY])
        store = HistoryStore(str(live), recover_unreadable_database=False)
        candidate = _history_database(
            self.root / "candidate.sqlite3", [self.ORDINARY, self.OVERSIZED]
        )
        original_dump = _dump(live)
        attempts: list[Callable[[], None]] = [
            lambda: store.restore_backup(candidate),
            lambda: store.restore_backup(source_path=candidate),
            lambda: store.restore_backup(candidate, rollback_snapshot=True),  # type: ignore[call-arg]
            lambda: store.restore_backup(candidate, True),  # type: ignore[call-arg]
        ]
        for index, attempt in enumerate(attempts):
            with self.subTest(attempt=index):
                with self.assertRaises((ValueError, TypeError)):
                    attempt()
                self.assertEqual(_dump(live), original_dump)
                self.assertNotIn((self.OVERSIZED, 0, 30), _metric_rows(live))

    def test_rollback_restore_only_accepts_the_snapshot_the_store_created(self) -> None:
        (self.root / "live").mkdir()
        live = _history_database(self.root / "live" / "history.sqlite3", [self.ORDINARY])
        store = HistoryStore(str(live), recover_unreadable_database=False)
        candidate = _history_database(
            self.root / "candidate.sqlite3", [self.ORDINARY, self.OVERSIZED]
        )
        original_dump = _dump(live)
        restore_snapshot: Callable[[Path], None] | None = getattr(
            store, "_restore_rollback_snapshot", None
        )
        assert restore_snapshot is not None, "the store has no private rollback restore"
        create_snapshot: Callable[[Path], Path] = getattr(store, "_create_rollback_snapshot")

        # A backup the store did not create as a rollback snapshot is refused.
        with self.assertRaises(ValueError):
            restore_snapshot(candidate)
        self.assertEqual(_dump(live), original_dump)

        # An ordinary store backup is not a rollback snapshot either.
        plain_backup = store.create_backup(self.root / "plain", retention_count=1)
        assert plain_backup is not None
        with self.assertRaises(ValueError):
            restore_snapshot(plain_backup)

        # A registered snapshot whose file was replaced is refused.
        snapshot = create_snapshot(self.root / "rollback-a")
        os.replace(candidate, snapshot)
        with self.assertRaises(ValueError):
            restore_snapshot(snapshot)
        self.assertEqual(_dump(live), original_dump)
        self.assertNotIn((self.OVERSIZED, 0, 30), _metric_rows(live))

        # The store's own snapshot restores once, with its original contents.
        snapshot = create_snapshot(self.root / "rollback-b")
        replacement = _history_database(self.root / "replacement.sqlite3", [self.ORDINARY] * 3)
        store.restore_backup(replacement)
        self.assertEqual(len(_metric_rows(live)), 3)
        restore_snapshot(snapshot)
        self.assertEqual(_dump(live), original_dump)
        with self.assertRaises(ValueError):
            restore_snapshot(snapshot)

    def test_incoming_backup_with_an_oversized_timestamp_is_still_refused(self) -> None:
        live = self.root / "live" / "history.sqlite3"
        live.parent.mkdir()
        store = HistoryStore(str(live), recover_unreadable_database=False)
        candidate = _history_database(
            self.root / "candidate.sqlite3", [self.ORDINARY, self.OVERSIZED]
        )
        before = live.read_bytes()
        with self.assertRaises(ValueError) as refused:
            store.restore_backup(candidate)
        self.assertIn("metric_samples.observed_at", str(refused.exception))
        self.assertEqual(live.read_bytes(), before)
        with self.assertRaises(ValueError):
            SystemBackupService._validate_history_member(candidate)


if __name__ == "__main__":
    unittest.main()
