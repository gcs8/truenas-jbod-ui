"""Process loss during a segmented restore that also restores configuration (#1196).

Each case hard-exits a forked child running the real import, then recovers
through the public recovery entry point and checks that configuration and
history end in the same generation, or that recovery refuses with a named
reason and keeps its evidence. Every fixture is synthetic and lives in a
temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
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
from history_service.segmented_restore import (
    IMPORT_SEAL_PURPOSE,
    import_seal_path,
    read_activation_journal,
    write_restore_journal,
)
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
REPOSITORY = Path(__file__).resolve().parents[1]
RECOVERY_COMMAND = REPOSITORY / "scripts" / "rotate_segmented_history.py"
INCOMPLETE_STATE = "configuration-recovery-incomplete"
INCOMPLETE_EXIT = 3
BOUNDED_SECONDS = 15.0


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str | None]]:
    """Every entry under ``root`` with its type and, for files, its digest."""
    snapshot: dict[str, tuple[str, str | None]] = {}
    for path in sorted(root.rglob("*")):
        metadata = os.lstat(path)
        if stat.S_ISREG(metadata.st_mode):
            entry = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISDIR(metadata.st_mode):
            entry = ("directory", None)
        else:
            entry = ("other", None)
        snapshot[str(path.relative_to(root))] = entry
    return snapshot


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

    @staticmethod
    def _config_path(target: _Deployment) -> str:
        return str(target.runtime.parent / "config.yaml")

    def _recover(self, target: _Deployment, *, apply: bool = True) -> dict[str, Any]:
        # Recovery finds the configuration folders from the application's own
        # settings, as the admin service would, never from the history volume.
        with patch.dict(os.environ, {"APP_CONFIG_PATH": self._config_path(target)}):
            return recover_pending_rotation(
                source=target.hot,
                segments_directory=target.segments,
                apply=apply,
            )

    def _recover_with_command(
        self,
        target: _Deployment,
        *,
        apply: bool,
    ) -> tuple[int, dict[str, Any] | None, str]:
        """Run the documented recovery command; return exit code, receipt, stderr."""
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment.update(
            PYTHONDONTWRITEBYTECODE="1",
            APP_CONFIG_PATH=self._config_path(target),
        )
        command = [
            sys.executable,
            str(RECOVERY_COMMAND),
            "--source",
            str(target.hot),
            "--segments-dir",
            str(target.segments),
            "--recover",
        ]
        if apply:
            command.append("--apply")
        completed = subprocess.run(
            command,
            cwd=REPOSITORY,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        receipt = json.loads(completed.stdout) if completed.stdout.strip() else None
        return completed.returncode, receipt, completed.stderr

    def _run_in_bounded_child(self, action: Callable[[], Any]) -> dict[str, Any]:
        """Run ``action`` in a forked child; fail if it does not return in time."""
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs only in the child
            os.close(read_end)
            try:
                try:
                    outcome: dict[str, Any] = {"result": action()}
                except Exception as exc:
                    outcome = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "reason": getattr(exc, "reason", None),
                        "cause": str(exc.__cause__) if exc.__cause__ else None,
                    }
                os.write(write_end, json.dumps(outcome, default=str).encode("utf-8"))
            finally:
                os._exit(FINISHED_EXIT)
        os.close(write_end)
        try:
            ready, _, _ = select.select([read_end], [], [], BOUNDED_SECONDS)
            if not ready:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                self.fail(f"recovery did not return within {BOUNDED_SECONDS:g} seconds")
            chunks = []
            while chunk := os.read(read_end, 65536):
                chunks.append(chunk)
        finally:
            os.close(read_end)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), FINISHED_EXIT)
        return json.loads(b"".join(chunks))

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

    def _crash_before_history(self, target: _Deployment) -> Path:
        """Interrupt the import after configuration, before history; return the journal."""
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
        marker = activation_pending_path(target.hot)
        self.assertTrue(marker.exists())
        return marker

    def _assert_refused_and_unchanged(
        self,
        target: _Deployment,
        reason: str,
        *,
        protected: Path | None = None,
        protected_bytes: bytes | None = None,
    ) -> None:
        """Both dry run and apply refuse with ``reason`` and change nothing."""
        marker = activation_pending_path(target.hot)
        journal_before = marker.read_bytes()
        hot_before = target.hot.read_bytes()
        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaises(ValueError) as refused:
                    self._recover(target, apply=apply)
                self.assertEqual(getattr(refused.exception, "reason", None), reason)
                self.assertEqual(marker.read_bytes(), journal_before)
                self.assertEqual(target.hot.read_bytes(), hot_before)
                self.assertEqual(self._config_generation(target), "candidate")
                if protected is not None:
                    self.assertEqual(
                        protected.read_bytes() if protected.exists() else None,
                        protected_bytes,
                    )

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
        # The decision lives in the first configuration folder's record, not
        # in the journal a writer of the history folder could edit.
        anchor = json.loads(
            import_seal_path(target.runtime.parent, journal["transaction_id"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(anchor.get("decision"), "commit")
        self.assertNotIn("decision", journal)

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
        transaction_id = forged["transaction_id"]
        history_folder = target.hot.parent.stat()
        # Every other field is consistent with the hot database, so only the
        # overlap check can refuse this target.
        forged["targets"][0].update(
            {
                "path": str(target.hot),
                "parent": {
                    "device": history_folder.st_dev,
                    "inode": history_folder.st_ino,
                },
                "staged_name": f".{target.hot.name}.restore-{transaction_id}",
                "previous_name": f".{target.hot.name}.previous-{transaction_id}",
            }
        )
        marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

        with self.assertRaises(ValueError) as refused:
            self._recover(target)
        self.assertEqual(getattr(refused.exception, "reason", None), "journal-invalid")
        self.assertIn("overlaps history", str(refused.exception))
        self.assertTrue(marker.exists())
        self.assertEqual(target.runtime.read_bytes(), CANDIDATE_RUNTIME)

        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    # Folder records (journal edited by a writer of the history folder) ------

    def _runtime_entry(self, journal: dict[str, Any], target: _Deployment) -> dict[str, Any]:
        return next(item for item in journal["targets"] if item["path"] == str(target.runtime))

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_forged_target_in_a_folder_the_restore_changed(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        original = marker.read_bytes()
        journal = json.loads(original)
        transaction_id = journal["transaction_id"]
        # An unselected sibling in the folder the restore really changed and
        # sealed. The forged target claims it did not exist before, so a
        # return to the prior generation would delete it.
        victim = target.runtime.parent / "profiles.yaml"
        victim_bytes = b"synthetic profiles kept by the operator\n"
        victim.write_bytes(victim_bytes)
        forged_target = dict(self._runtime_entry(journal, target))
        forged_target.update(
            {
                "path": str(victim),
                "staged_name": f".{victim.name}.restore-{transaction_id}",
                "previous_name": f".{victim.name}.previous-{transaction_id}",
                "prior": {"kind": "missing"},
                "candidate": record_restore_file(victim),
            }
        )
        forged = json.loads(original)
        forged["targets"].append(forged_target)
        marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

        self._assert_refused_and_unchanged(
            target,
            "configuration-target-unrecorded",
            protected=victim,
            protected_bytes=victim_bytes,
        )

        # Control: the journal the restore wrote recovers and keeps the file.
        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )
        self.assertEqual(victim.read_bytes(), victim_bytes)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_listed_target_whose_records_were_altered(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        original = marker.read_bytes()
        for field in ("prior", "candidate"):
            with self.subTest(field=field):
                forged = json.loads(original)
                entry = self._runtime_entry(forged, target)
                entry[field] = dict(entry[field], sha256="0" * 64)
                marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))
                self._assert_refused_and_unchanged(target, "configuration-target-unrecorded")

        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_truncated_or_replaced_folder_record(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        transaction_id = json.loads(marker.read_bytes())["transaction_id"]
        record = import_seal_path(target.runtime.parent, transaction_id)
        other_folder_record = import_seal_path(target.mapping.parent, transaction_id)
        original = record.read_bytes()
        for label, content in (
            ("truncated", original[: len(original) // 2]),
            ("replaced", other_folder_record.read_bytes()),
        ):
            with self.subTest(record=label):
                record.write_bytes(content)
                self._assert_refused_and_unchanged(target, "configuration-target-unsealed")

        record.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_converges_a_restored_folder_the_journal_omits(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        forged = json.loads(marker.read_bytes())
        # Dropping a whole folder from the index must not leave its restored
        # files behind while every other target returns to the prior.
        forged["targets"] = [
            item
            for item in forged["targets"]
            if Path(item["path"]).parent != target.mapping.parent
        ]
        self.assertTrue(forged["targets"])
        marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

        result = self._recover(target)

        self.assertEqual(
            self._assert_one_generation(target, result, label="folder omitted"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_first_folder_record_planted_outside_configuration(self) -> None:
        target = self._target()
        # Every target, history included, holds the candidate and no decision
        # was recorded, so the restore's own records return to the prior.
        exit_code = self._import_in_child(
            target,
            lambda: [patch.object(_ImportActivationTransaction, "commit", self._hard_exit)],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        marker = activation_pending_path(target.hot)
        original = marker.read_bytes()
        journal = json.loads(original)
        transaction_id = journal["transaction_id"]

        def identity(folder: Path) -> dict[str, Any]:
            metadata = folder.stat()
            return {"path": str(folder), "device": metadata.st_dev, "inode": metadata.st_ino}

        # A writer of the history folder plants a first-folder record there
        # that claims the commit decision, listing the real configuration
        # folders or leaving them out.
        planted_folder = target.hot.parent
        planted = import_seal_path(planted_folder, transaction_id)
        hot_before = target.hot.read_bytes()
        for label, folders in (
            ("lists them", [planted_folder, target.runtime.parent, target.mapping.parent]),
            ("leaves them out", [planted_folder]),
        ):
            with self.subTest(planted=label):
                planted.unlink(missing_ok=True)
                descriptor = os.open(planted, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(
                        json.dumps(
                            {
                                "purpose": "restore-import-seal",
                                "transaction_id": transaction_id,
                                "folder": str(planted_folder),
                                "anchor": str(planted_folder),
                                "folders": [identity(folder) for folder in folders],
                                "decision": "commit",
                                "targets": [],
                            }
                        ).encode("utf-8")
                    )
                forged = json.loads(original)
                forged["anchor"] = identity(planted_folder)
                marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))
                for apply in (False, True):
                    with self.assertRaises(ValueError, msg=f"apply={apply}") as refused:
                        self._recover(target, apply=apply)
                    self.assertEqual(
                        getattr(refused.exception, "reason", None),
                        "configuration-target-unsealed",
                    )
                    self.assertTrue(marker.exists())
                    self.assertEqual(target.hot.read_bytes(), hot_before)
                    self.assertEqual(self._config_generation(target), "candidate")

        planted.unlink()
        marker.write_bytes(original)
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_takes_the_decision_from_the_configuration_folder(self) -> None:
        cases = (
            # Crash after the commit decision; the journal now claims rollback.
            ("committed", "_finalize_commit", "rollback", "candidate"),
            # Crash after every target, history included, held the candidate
            # and before any decision; the journal now claims commit.
            ("undecided", "commit", "commit", "prior"),
        )
        for label, crash_at, forged_decision, expected in cases:
            with self.subTest(case=label):
                target = self._target(f"target-{label}")
                exit_code = self._import_in_child(
                    target,
                    lambda name=crash_at: [
                        patch.object(_ImportActivationTransaction, name, self._hard_exit)
                    ],
                )
                self.assertEqual(exit_code, CRASH_EXIT)
                marker = activation_pending_path(target.hot)
                forged = json.loads(marker.read_bytes())
                forged["decision"] = forged_decision
                marker.write_bytes(json.dumps(forged, sort_keys=True).encode("utf-8"))

                result = self._recover(target)

                self.assertEqual(
                    self._assert_one_generation(target, result, label=label),
                    expected,
                )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_refuses_a_folder_record_that_names_another_folder(self) -> None:
        cases = ("folder field changed", "copied from a third folder")
        for label in cases:
            with self.subTest(record=label):
                target = self._target(f"target-{cases.index(label)}")
                marker = self._crash_before_history(target)
                original_journal = marker.read_bytes()
                journal = json.loads(original_journal)
                transaction_id = journal["transaction_id"]
                anchor_path = import_seal_path(target.runtime.parent, transaction_id)
                record_path = import_seal_path(target.mapping.parent, transaction_id)
                original_anchor = anchor_path.read_bytes()
                original_record = record_path.read_bytes()
                third = target.root / "third"
                if label == "folder field changed":
                    # Only the record's own-folder field is wrong.
                    record = json.loads(original_record)
                    record["folder"] = str(target.runtime.parent)
                    record_path.write_bytes(json.dumps(record).encode("utf-8"))
                else:
                    # A third listed folder holds an empty record (legitimate
                    # before anything there is staged). Copied over the data
                    # folder's record, it shares the anchor and would make
                    # recovery skip the data folder's restored files.
                    third.mkdir()
                    metadata = third.stat()
                    anchor = json.loads(original_anchor)
                    anchor["folders"].append(
                        {"path": str(third), "device": metadata.st_dev, "inode": metadata.st_ino}
                    )
                    anchor_path.write_bytes(json.dumps(anchor).encode("utf-8"))
                    write_restore_journal(
                        import_seal_path(third, transaction_id),
                        {
                            "purpose": IMPORT_SEAL_PURPOSE,
                            "transaction_id": transaction_id,
                            "folder": str(third),
                            "anchor": str(target.runtime.parent),
                            "targets": [],
                        },
                    )
                    record_path.write_bytes(import_seal_path(third, transaction_id).read_bytes())
                    journal["targets"] = [
                        item
                        for item in journal["targets"]
                        if Path(item["path"]).parent != target.mapping.parent
                    ]
                    marker.write_bytes(json.dumps(journal, sort_keys=True).encode("utf-8"))

                self._assert_refused_and_unchanged(target, "configuration-target-unsealed")

                # Control: the records and journal the restore wrote recover.
                record_path.write_bytes(original_record)
                anchor_path.write_bytes(original_anchor)
                marker.write_bytes(original_journal)
                if third.exists():
                    import_seal_path(third, transaction_id).unlink()
                    third.rmdir()
                result = self._recover(target)
                self.assertEqual(
                    self._assert_one_generation(target, result, label=f"{label} control"),
                    "prior",
                )

    # A FIFO, device or folder in place of a record must not stall recovery --

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_recovery_does_not_block_on_a_fifo_in_place_of_a_record_or_journal(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        transaction_id = json.loads(marker.read_bytes())["transaction_id"]
        record_path = import_seal_path(target.mapping.parent, transaction_id)
        original_record = record_path.read_bytes()
        record_path.unlink()
        os.mkfifo(record_path, 0o600)
        before = _tree_snapshot(target.root)
        for apply in (False, True):
            with self.subTest(fifo="folder record", apply=apply):
                outcome = self._run_in_bounded_child(
                    lambda apply=apply: self._recover(target, apply=apply)
                )
                self.assertEqual(outcome.get("reason"), "configuration-target-unsealed", outcome)
                self.assertEqual(outcome.get("cause"), "Restore folder record is invalid.")
                self.assertEqual(_tree_snapshot(target.root), before)
        record_path.unlink()
        record_path.write_bytes(original_record)
        record_path.chmod(0o600)

        # The journal reader shares the same bounded open.
        original_journal = marker.read_bytes()
        marker.unlink()
        os.mkfifo(marker, 0o600)
        with self.subTest(fifo="journal"):
            outcome = self._run_in_bounded_child(lambda: read_activation_journal(marker))
            self.assertEqual(outcome.get("error"), "History activation journal is invalid.", outcome)
        with self.subTest(fifo="journal", path="public recovery"):
            before = _tree_snapshot(target.root)
            outcome = self._run_in_bounded_child(lambda: self._recover(target, apply=True))
            self.assertEqual(outcome.get("result", {}).get("recovery_state"), INCOMPLETE_STATE, outcome)
            self.assertEqual(_tree_snapshot(target.root), before)
        marker.unlink()
        marker.write_bytes(original_journal)
        marker.chmod(0o600)

        # Control: the restored record and journal recover normally.
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="fifo control"),
            "prior",
        )

    # Records the journal no longer names (journal edited, removed or broken) -

    def _crash_mid_configuration(self, target: _Deployment) -> None:
        original_activate = _ImportActivationTransaction._activate_journaled_file

        def activate_one_then_exit(transaction: Any, *args: Any, **kwargs: Any) -> None:
            original_activate(transaction, *args, **kwargs)
            os._exit(CRASH_EXIT)

        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(
                    _ImportActivationTransaction,
                    "_activate_journaled_file",
                    activate_one_then_exit,
                )
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        self.assertEqual(self._history_generation(target), "prior")
        self.assertTrue(self._config_generation(target).startswith("mixed"))

    def _crash_mid_rollback(self, target: _Deployment) -> None:
        original_restore = _ImportActivationTransaction._restore_target

        def fail_commit(_transaction: Any) -> None:
            raise RuntimeError("synthetic failure after every target was activated")

        def restore_one_then_exit(transaction: Any, entry: Any) -> None:
            original_restore(transaction, entry)
            os._exit(CRASH_EXIT)

        exit_code = self._import_in_child(
            target,
            lambda: [
                patch.object(_ImportActivationTransaction, "commit", fail_commit),
                patch.object(_ImportActivationTransaction, "_restore_target", restore_one_then_exit),
            ],
        )
        self.assertEqual(exit_code, CRASH_EXIT)
        self.assertEqual(self._history_generation(target), "candidate")
        self.assertTrue(self._config_generation(target).startswith("mixed"))

    @staticmethod
    def _record_folders(target: _Deployment) -> list[str]:
        return sorted(
            {
                str(path.parent)
                for path in target.root.rglob(".truenas-jbod-ui-import-seal-*")
                if ".next-" not in path.name
            }
        )

    @staticmethod
    def _edit_journal(target: _Deployment, edit: str, *, decision: str) -> None:
        """Change only the history volume, as a writer of that volume could."""
        marker = activation_pending_path(target.hot)
        journal = json.loads(marker.read_bytes())
        if edit == "version 1":
            journal["journal_version"] = 1
            journal.pop("targets")
            journal.pop("anchor")
        elif edit == "deleted":
            marker.unlink()
            return
        elif edit == "corrupt":
            marker.write_bytes(b"{broken")
            return
        elif edit == "empty version 2 anchor":
            folder = target.hot.parent
            metadata = folder.stat()
            identity = {"path": str(folder), "device": metadata.st_dev, "inode": metadata.st_ino}
            write_restore_journal(
                import_seal_path(folder, journal["transaction_id"]),
                {
                    "purpose": IMPORT_SEAL_PURPOSE,
                    "transaction_id": journal["transaction_id"],
                    "folder": str(folder),
                    "anchor": str(folder),
                    "folders": [identity],
                    "decision": decision,
                    "targets": [],
                },
            )
            journal["targets"] = []
            journal["anchor"] = identity
        else:
            raise AssertionError(edit)
        marker.write_bytes(json.dumps(journal, sort_keys=True).encode("utf-8"))

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_command_reports_records_an_edited_journal_leaves_behind(self) -> None:
        scenarios = (
            ("mid-configuration", self._crash_mid_configuration, "pending"),
            ("mid-rollback", self._crash_mid_rollback, "commit"),
        )
        edits = ("version 1", "deleted", "corrupt", "empty version 2 anchor")
        for scenario, crash, decision in scenarios:
            for edit in edits:
                with self.subTest(scenario=scenario, edit=edit):
                    target = self._target(f"target-{scenario}-{edit.replace(' ', '-')}")
                    crash(target)
                    record_folders = [
                        folder
                        for folder in self._record_folders(target)
                        if folder != str(target.hot.parent)
                    ]
                    self.assertTrue(record_folders)
                    self._edit_journal(target, edit, decision=decision)
                    before = _tree_snapshot(target.root)
                    for apply in (False, True):
                        exit_code, receipt, stderr = self._recover_with_command(
                            target,
                            apply=apply,
                        )
                        self.assertEqual(
                            (exit_code, (receipt or {}).get("recovery_state")),
                            (INCOMPLETE_EXIT, INCOMPLETE_STATE),
                            f"apply={apply}: {stderr[-400:]}",
                        )
                        assert receipt is not None
                        self.assertEqual(receipt["folders"], record_folders)
                        self.assertEqual(receipt["apply"], apply)
                        self.assertEqual(_tree_snapshot(target.root), before)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_command_reports_success_when_no_record_is_left(self) -> None:
        for scenario, crash in (
            ("mid-configuration", self._crash_mid_configuration),
            ("mid-rollback", self._crash_mid_rollback),
        ):
            with self.subTest(scenario=scenario):
                target = self._target(f"target-{scenario}")
                crash(target)
                exit_code, receipt, stderr = self._recover_with_command(target, apply=False)
                self.assertEqual(exit_code, 0, stderr[-400:])
                assert receipt is not None
                self.assertEqual(receipt["recovery_state"], "prior-generation-ready-to-restore")
                exit_code, receipt, stderr = self._recover_with_command(target, apply=True)
                self.assertEqual(exit_code, 0, stderr[-400:])
                assert receipt is not None
                self._assert_one_generation(target, receipt, label=scenario)
                self.assertEqual(self._record_folders(target), [])
                # Nothing is pending and no record is left: the usual refusal.
                exit_code, receipt, stderr = self._recover_with_command(target, apply=True)
                self.assertEqual((exit_code, receipt), (1, None))
                self.assertIn("Segment rotation recovery is not pending.", stderr)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork to model process loss")
    def test_recovery_reports_a_record_of_another_restore_and_changes_nothing(self) -> None:
        target = self._target()
        marker = self._crash_before_history(target)
        transaction_id = json.loads(marker.read_bytes())["transaction_id"]
        # A record that this recovery does not settle: another transaction's,
        # left in a configuration folder the application knows about.
        stale = import_seal_path(target.mapping.parent, "an-earlier-restore")
        stale.write_bytes(import_seal_path(target.mapping.parent, transaction_id).read_bytes())
        stale.chmod(0o600)
        before = _tree_snapshot(target.root)
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = self._recover(target, apply=apply)
                self.assertEqual(result["recovery_state"], INCOMPLETE_STATE)
                self.assertEqual(result["folders"], [str(target.mapping.parent)])
                self.assertEqual(_tree_snapshot(target.root), before)

        stale.unlink()
        result = self._recover(target)
        self.assertEqual(
            self._assert_one_generation(target, result, label="control"),
            "prior",
        )


if __name__ == "__main__":
    unittest.main()
