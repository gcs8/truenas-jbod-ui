"""Process loss during a segmented restore that also restores configuration (#1196).

Each case hard-exits a forked child running the real import, then recovers
through the public recovery entry point and checks that configuration and
history end in the same generation, or that recovery refuses with a named
reason and keeps its evidence. Every fixture is synthetic and lives in a
temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from app.config import Settings
from history_service import segment_migration
from history_service.config import HistorySettings
from history_service.segment_catalog import activation_pending_path
from history_service.segment_rotation import recover_pending_rotation
from history_service.segmented_restore import record_file as record_restore_file
from history_service.store import SCHEMA, HistoryStore
from history_service.system_backup import (
    HISTORY_DB_KEY,
    MAPPING_FILE_KEY,
    RUNTIME_OVERRIDES_FILE_KEY,
    TLS_TRUST_KEY,
    SystemBackupService,
    _ImportActivationTransaction,
)

PASSPHRASE = "synthetic-restore-passphrase-0001"
PRIOR_RUNTIME = b"app: {source_bundle_cache_ttl_seconds: 60}\n"
CANDIDATE_RUNTIME = b"app: {source_bundle_cache_ttl_seconds: 123}\n"
CANDIDATE_MAPPING = b"{}\n"
PRIOR_TLS = ["prior.pem"]
CANDIDATE_TLS = ["candidate.pem"]
CRASH_EXIT = 23
FINISHED_EXIT = 24
UNEXPECTED_EXIT = 25
_ORIGINAL_RMTREE = shutil.rmtree


class _FixedSettingsBackupService(SystemBackupService):
    """Use injected deployment settings instead of reading a config file."""

    def _load_app_settings(self) -> Settings:
        return self.app_settings


@dataclass
class _Deployment:
    root: Path
    hot: Path
    segments: Path
    runtime: Path
    mapping: Path
    tls: Path
    service: SystemBackupService


def _settings(root: Path) -> Settings:
    config = root / "config"
    data = root / "data"
    config.mkdir(parents=True)
    data.mkdir()
    return Settings(
        config_file=str(config / "config.yaml"),
        paths={
            "runtime_overrides_file": str(config / "runtime-overrides.yaml"),
            "profile_file": str(config / "profiles.yaml"),
            "mapping_file": str(data / "slot_mappings.json"),
            "sas_fabric_alias_file": str(data / "sas_fabric_aliases.json"),
            "slot_detail_cache_file": str(data / "slot_detail_cache.json"),
            "log_file": str(root / "logs" / "app.log"),
        },
    )


def _create_history(root: Path, label: str) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    hot = root / "history.db"
    segments = root / "segments"
    with sqlite3.connect(hot) as connection:
        connection.executescript(SCHEMA)
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
    connection.close()
    segment_migration.migrate_segmented_history(
        source=hot,
        segments_directory=segments,
        cutoff="2025-01-02T00:00:00+00:00",
        key_id=f"{label}-key",
        apply=True,
    )
    return hot, segments


def _deployment(root: Path, label: str) -> _Deployment:
    app_settings = _settings(root)
    hot, segments = _create_history(root / "history", label)
    history_settings = HistorySettings(
        sqlite_path=str(hot),
        segment_catalog_path=str(segments / "catalog.json"),
        backup_dir=str(root / "history" / "backups"),
        startup_grace_seconds=0,
    )
    store = HistoryStore(
        str(hot),
        recover_unreadable_database=False,
        segment_catalog_path=segments / "catalog.json",
    )
    return _Deployment(
        root=root,
        hot=hot,
        segments=segments,
        runtime=Path(app_settings.paths.runtime_overrides_file),
        mapping=Path(app_settings.paths.mapping_file),
        tls=Path(app_settings.config_file).parent / "tls",
        service=_FixedSettingsBackupService(history_settings, store, app_settings),
    )


def _hot_events(hot: Path) -> list[str]:
    # immutable=1 reads without creating WAL or shared-memory sidecars, which
    # recovery would otherwise refuse.
    connection = sqlite3.connect(f"{hot.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM slot_events ORDER BY julianday(observed_at), id"
            )
        ]
    finally:
        connection.close()


class InterruptedRestoreRecoveryTests(unittest.TestCase):
    artifact: Any = None
    candidate_generation_id = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls._class_directory = tempfile.TemporaryDirectory()
        source = _deployment(Path(cls._class_directory.name) / "source", "candidate")
        source.runtime.write_bytes(CANDIDATE_RUNTIME)
        source.mapping.write_bytes(CANDIDATE_MAPPING)
        source.tls.mkdir()
        (source.tls / CANDIDATE_TLS[0]).write_bytes(b"synthetic candidate trust\n")
        cls.candidate_generation_id = json.loads(
            (source.segments / "catalog.json").read_text(encoding="utf-8")
        )["generation_id"]
        # TLS trust is a locked group, so the export must be encrypted.
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

    def setUp(self) -> None:
        self._import_roots_before = set(
            Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*")
        )
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def tearDown(self) -> None:
        for import_root in (
            set(Path(tempfile.gettempdir()).glob("truenas-jbod-ui-import-*"))
            - self._import_roots_before
        ):
            shutil.rmtree(import_root, ignore_errors=True)

    # Fixtures ---------------------------------------------------------------

    def _target(self, name: str = "target") -> _Deployment:
        target = _deployment(Path(self._directory.name) / name, "prior")
        target.runtime.write_bytes(PRIOR_RUNTIME)
        target.tls.mkdir()
        (target.tls / PRIOR_TLS[0]).write_bytes(b"synthetic prior trust\n")
        self.prior_generation_id = json.loads(
            (target.segments / "catalog.json").read_text(encoding="utf-8")
        )["generation_id"]
        return target

    def _import_in_child(
        self,
        target: _Deployment,
        install_fault: Callable[[], list[Any]],
    ) -> int:
        """Run the real import in a forked child that the fault hard-exits."""
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs only in the child
            code = UNEXPECTED_EXIT
            try:
                patches = install_fault()
                for active in patches:
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

    def _config_generation(self, target: _Deployment) -> str:
        runtime = target.runtime.read_bytes() if target.runtime.exists() else None
        mapping = target.mapping.read_bytes() if target.mapping.exists() else None
        tls = sorted(os.listdir(target.tls)) if target.tls.is_dir() else None
        if (runtime, mapping, tls) == (PRIOR_RUNTIME, None, PRIOR_TLS):
            return "prior"
        if (runtime, mapping, tls) == (CANDIDATE_RUNTIME, CANDIDATE_MAPPING, CANDIDATE_TLS):
            return "candidate"
        return f"mixed(runtime={runtime!r}, mapping={mapping!r}, tls={tls!r})"

    def _history_generation(self, target: _Deployment) -> str:
        events = _hot_events(target.hot)
        catalog_id = json.loads(
            (target.segments / "catalog.json").read_text(encoding="utf-8")
        )["generation_id"]
        if events == ["prior-hot"] and catalog_id == self.prior_generation_id:
            return "prior"
        if events == ["candidate-hot"] and catalog_id == self.candidate_generation_id:
            return "candidate"
        return f"mixed(events={events!r}, catalog={catalog_id!r})"

    def _leftover_restore_artifacts(
        self,
        target: _Deployment,
        *,
        history_transaction_id: str | None = None,
    ) -> list[str]:
        leftovers: list[str] = []
        for directory in (
            target.runtime.parent,
            target.mapping.parent,
            target.hot.parent,
        ):
            for path in directory.iterdir():
                if (
                    directory == target.hot.parent
                    and history_transaction_id is not None
                    and history_transaction_id not in path.name
                ):
                    # An in-process rollback parks a displaced history
                    # candidate under an unrecorded random name; a crash while
                    # deleting it can leave it behind. That predates #1196 and
                    # does not mix generations.
                    continue
                # A crash inside authenticated removal can also leave a private
                # ".segmented-restore-retired-*" quarantine holding an already
                # retired copy. That predates #1196 and does not mix
                # generations, so it is not asserted here.
                if (
                    ".previous-" in path.name
                    or ".restore-" in path.name
                    or "-import-seal-" in path.name
                ):
                    leftovers.append(str(path.relative_to(target.root)))
        return sorted(leftovers)

    def _recover(self, target: _Deployment, *, apply: bool = True) -> dict[str, Any]:
        return recover_pending_rotation(
            source=target.hot,
            segments_directory=target.segments,
            apply=apply,
        )

    def _assert_one_generation(
        self,
        target: _Deployment,
        result: dict[str, Any],
        *,
        label: str,
        history_transaction_id: str | None = None,
    ) -> str:
        config_side = self._config_generation(target)
        history_side = self._history_generation(target)
        self.assertEqual(
            config_side,
            history_side,
            f"{label}: configuration and history are from different generations",
        )
        expected_state = {
            "prior": "prior-generation-restored",
            "candidate": "candidate-finalized",
        }[history_side]
        self.assertEqual(result["recovery_state"], expected_state, label)
        self.assertFalse(activation_pending_path(target.hot).exists(), label)
        self.assertEqual(
            self._leftover_restore_artifacts(
                target,
                history_transaction_id=history_transaction_id,
            ),
            [],
            label,
        )
        return history_side

    @staticmethod
    def _hard_exit(*_args: Any, **_kwargs: Any) -> None:
        os._exit(CRASH_EXIT)

    # Crash matrix -----------------------------------------------------------

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_process_loss_after_config_before_history_recovers_one_generation(self) -> None:
        target = self._target()

        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_segmented_history",
                    self._hard_exit,
                )
            ],
        )

        self.assertEqual(exit_code, CRASH_EXIT)
        self.assertEqual(self._config_generation(target), "candidate")
        self.assertTrue(activation_pending_path(target.hot).exists())

        dry_run = self._recover(target, apply=False)
        self.assertEqual(dry_run["recovery_state"], "prior-generation-ready-to-restore")
        self.assertEqual(self._config_generation(target), "candidate")
        self.assertTrue(activation_pending_path(target.hot).exists())

        result = self._recover(target)

        side = self._assert_one_generation(target, result, label="before history")
        self.assertEqual(side, "prior")

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_process_loss_after_every_rename_recovers_one_generation(self) -> None:
        outcomes: dict[int, str] = {}
        crash_after = 0
        while True:
            crash_after += 1
            self.assertLess(crash_after, 80, "the import never finished")
            target = self._target(f"target-{crash_after:02d}")

            def install_fault(limit: int = crash_after) -> list[Any]:
                original_replace = os.replace
                count = 0

                def replace_then_maybe_exit(*args: Any, **kwargs: Any) -> None:
                    nonlocal count
                    original_replace(*args, **kwargs)
                    count += 1
                    if count == limit:
                        os._exit(CRASH_EXIT)

                return [patch("os.replace", replace_then_maybe_exit)]

            exit_code = self._import_in_child(target, install_fault)
            if exit_code == FINISHED_EXIT:
                self.assertFalse(activation_pending_path(target.hot).exists())
                break
            self.assertEqual(exit_code, CRASH_EXIT, f"rename {crash_after}")
            with self.subTest(crash_after_rename=crash_after):
                if not activation_pending_path(target.hot).exists():
                    # The journal was already retired: the import finished
                    # its last durable step and there is nothing to recover.
                    self.assertEqual(self._config_generation(target), "candidate")
                    self.assertEqual(self._history_generation(target), "candidate")
                    outcomes[crash_after] = "candidate"
                    continue
                result = self._recover(target)
                outcomes[crash_after] = self._assert_one_generation(
                    target,
                    result,
                    label=f"crash after rename {crash_after}",
                )
        self.assertIn("prior", outcomes.values())
        self.assertIn("candidate", outcomes.values())

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_process_loss_inside_every_recursive_delete_recovers_one_generation(self) -> None:
        for import_fails in (False, True):
            with self.subTest(import_fails=import_fails):
                self._crash_inside_every_recursive_delete(import_fails=import_fails)

    def _crash_inside_every_recursive_delete(self, *, import_fails: bool) -> None:
        outcomes: dict[int, str] = {}
        crash_inside = 0
        while True:
            crash_inside += 1
            self.assertLess(crash_inside, 40, "the import never finished")
            target = self._target(f"target-{int(import_fails)}-{crash_inside:02d}")

            def install_fault(limit: int = crash_inside) -> list[Any]:
                count = 0

                def fail_commit(_transaction: Any) -> None:
                    raise RuntimeError("synthetic failure after every target was activated")

                def delete_one_file_then_exit(path: Any, *args: Any, **kwargs: Any) -> None:
                    nonlocal count
                    count += 1
                    if count == limit:
                        # Model process loss part-way through a recursive
                        # delete: one child is gone, the rest remain.
                        for child in sorted(Path(path).rglob("*")):
                            if child.is_file() and not child.is_symlink():
                                child.unlink()
                                break
                        os._exit(CRASH_EXIT)
                    _ORIGINAL_RMTREE(path, *args, **kwargs)

                patches = [patch("shutil.rmtree", delete_one_file_then_exit)]
                if import_fails:
                    patches.append(
                        patch.object(_ImportActivationTransaction, "commit", fail_commit)
                    )
                return patches

            exit_code = self._import_in_child(target, install_fault)
            if exit_code in {FINISHED_EXIT, UNEXPECTED_EXIT} and not activation_pending_path(
                target.hot
            ).exists():
                # Finished: committed, or rolled back in process as designed.
                expected = "prior" if import_fails else "candidate"
                self.assertEqual(self._config_generation(target), expected)
                self.assertEqual(self._history_generation(target), expected)
                break
            self.assertEqual(exit_code, CRASH_EXIT, f"recursive delete {crash_inside}")
            with self.subTest(crash_inside_recursive_delete=crash_inside):
                if not activation_pending_path(target.hot).exists():
                    side = self._config_generation(target)
                    self.assertEqual(self._history_generation(target), side)
                    outcomes[crash_inside] = side
                    continue
                transaction_id = json.loads(
                    activation_pending_path(target.hot).read_text(encoding="utf-8")
                )["transaction_id"]
                result = self._recover(target)
                outcomes[crash_inside] = self._assert_one_generation(
                    target,
                    result,
                    label=f"crash inside recursive delete {crash_inside}",
                    history_transaction_id=transaction_id,
                )
        self.assertTrue(outcomes, "no recursive delete was interrupted")

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_process_loss_during_failed_import_rollback_recovers_one_generation(self) -> None:
        target = self._target()

        def install_fault() -> list[Any]:
            original_restore = _ImportActivationTransaction._restore_target

            def fail_commit(_transaction: Any) -> None:
                raise RuntimeError("synthetic failure after every target was activated")

            def restore_one_then_exit(transaction: Any, entry: Any) -> None:
                original_restore(transaction, entry)
                os._exit(CRASH_EXIT)

            return [
                patch.object(_ImportActivationTransaction, "commit", fail_commit),
                patch.object(
                    _ImportActivationTransaction,
                    "_restore_target",
                    restore_one_then_exit,
                ),
            ]

        exit_code = self._import_in_child(target, install_fault)

        self.assertEqual(exit_code, CRASH_EXIT)
        self.assertEqual(self._history_generation(target), "candidate")
        self.assertNotEqual(self._config_generation(target), "candidate")

        result = self._recover(target)

        side = self._assert_one_generation(target, result, label="during rollback")
        self.assertEqual(side, "prior")

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_process_loss_after_commit_decision_finishes_the_candidate(self) -> None:
        target = self._target()

        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "_finalize_commit",
                    self._hard_exit,
                )
            ],
        )

        self.assertEqual(exit_code, CRASH_EXIT)
        journal = json.loads(activation_pending_path(target.hot).read_text(encoding="utf-8"))
        self.assertEqual(journal.get("decision"), "commit")

        result = self._recover(target)

        side = self._assert_one_generation(target, result, label="after commit decision")
        self.assertEqual(side, "candidate")

    # Refusals ---------------------------------------------------------------

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_when_a_configuration_target_is_unreachable(self) -> None:
        target = self._target()
        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_segmented_history",
                    self._hard_exit,
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        hot_before = target.hot.read_bytes()
        config_directory = target.runtime.parent
        hidden = config_directory.with_name("config-not-mounted")
        # Recovery run where the configuration volume is not mounted.
        config_directory.rename(hidden)

        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaises(ValueError) as refused:
                    self._recover(target, apply=apply)
                self.assertEqual(
                    getattr(refused.exception, "reason", None),
                    "configuration-target-unavailable",
                )
                self.assertTrue(activation_pending_path(target.hot).exists())
                self.assertEqual(target.hot.read_bytes(), hot_before)
                self.assertEqual(
                    (hidden / target.runtime.name).read_bytes(),
                    CANDIDATE_RUNTIME,
                )

        hidden.rename(config_directory)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_journal_written_by_another_identity(self) -> None:
        target = self._target()
        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_segmented_history",
                    self._hard_exit,
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)

        with patch("os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError) as refused:
                self._recover(target)
        self.assertEqual(getattr(refused.exception, "reason", None), "journal-owner-mismatch")
        self.assertTrue(activation_pending_path(target.hot).exists())
        self.assertEqual(target.runtime.read_bytes(), CANDIDATE_RUNTIME)

        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_target_added_by_a_writer_of_history_only(self) -> None:
        target = self._target()
        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_segmented_history",
                    self._hard_exit,
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        marker = activation_pending_path(target.hot)
        journal = json.loads(marker.read_text(encoding="utf-8"))
        self.assertTrue(
            journal.get("targets"),
            "the restore journal must record the configuration targets it activated",
        )
        # A service that can write the history folder but not this folder
        # forges a target that says the victim did not exist before the
        # restore, so rollback would delete it.
        protected = target.root / "protected"
        protected.mkdir()
        victim = protected / "keep.yaml"
        victim.write_bytes(b"synthetic protected settings\n")
        folder = protected.stat()
        forged_target = dict(journal["targets"][0])
        forged_target.update(
            {
                "path": str(victim),
                "kind": "file",
                "parent": {"device": folder.st_dev, "inode": folder.st_ino},
                "staged_name": f".{victim.name}.restore-{journal['transaction_id']}",
                "previous_name": f".{victim.name}.previous-{journal['transaction_id']}",
                "prior": {"kind": "missing"},
                "candidate": record_restore_file(victim),
            }
        )
        original = marker.read_bytes()
        forged = json.loads(original)
        forged["targets"].append(forged_target)
        marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

        with self.assertRaises(ValueError) as refused:
            self._recover(target)
        self.assertEqual(
            getattr(refused.exception, "reason", None),
            "configuration-target-unsealed",
        )
        self.assertEqual(victim.read_bytes(), b"synthetic protected settings\n")
        self.assertTrue(marker.exists())
        self.assertEqual(target.runtime.read_bytes(), CANDIDATE_RUNTIME)

        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )
        self.assertEqual(victim.read_bytes(), b"synthetic protected settings\n")

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_configuration_target_aimed_at_history(self) -> None:
        target = self._target()
        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "activate_segmented_history",
                    self._hard_exit,
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        marker = activation_pending_path(target.hot)
        journal = json.loads(marker.read_text(encoding="utf-8"))
        self.assertTrue(
            journal.get("targets"),
            "the restore journal must record the configuration targets it activated",
        )
        original = marker.read_bytes()
        forged = json.loads(original)
        forged["targets"][0]["path"] = str(target.hot)
        marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

        with self.assertRaises(ValueError) as refused:
            self._recover(target)
        self.assertEqual(getattr(refused.exception, "reason", None), "journal-invalid")
        self.assertTrue(marker.exists())
        self.assertEqual(target.runtime.read_bytes(), CANDIDATE_RUNTIME)

        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )


if __name__ == "__main__":
    unittest.main()
