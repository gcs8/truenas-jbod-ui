"""Restore refusal and cleanup (#1273).

Every fixture is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from history_service.segment_catalog import activation_pending_path
from history_service.segment_rotation import recover_pending_rotation
from history_service.store import HistoryStore
from history_service.system_backup import (
    HISTORY_DB_KEY,
    MAPPING_FILE_KEY,
    RUNTIME_OVERRIDES_FILE_KEY,
    TLS_TRUST_KEY,
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
    _Deployment,
    _deployment,
    _hot_events,
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


if __name__ == "__main__":
    unittest.main()
