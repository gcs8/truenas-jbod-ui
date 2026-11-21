"""Restore rollback retires the staged hot database by its record (#1409).

A refused restore leaves an unconsumed staged copy of the hot history
database beside the live one. Rollback must retire it through the
authenticated path (move aside, check against the journal record, then
delete), the same way it retires the staged segment tree (#1273), and never
by a plain unlink of whatever sits under the recorded name.

Every fixture is synthetic and lives in a temporary directory.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import history_service.segmented_restore as segmented_restore_module
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
    PASSPHRASE,
    PRIOR_RUNTIME,
    PRIOR_TLS,
    _Deployment,
    _deployment,
    _hot_events,
)

_ORIGINAL_RETIRE = segmented_restore_module._retire_authenticated
_REPLACEMENT_BYTES = b"synthetic replacement, not the staged hot database\n"


def _import_workspaces() -> set[Path]:
    temp = Path(tempfile.gettempdir())
    return set(temp.glob("truenas-jbod-ui-import-*")) | set(
        temp.glob("truenas-jbod-ui-file-import-*")
    )


class StagedHotRetirementTests(unittest.TestCase):
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

    def setUp(self) -> None:
        self._import_roots_before = _import_workspaces()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def tearDown(self) -> None:
        for import_root in _import_workspaces() - self._import_roots_before:
            shutil.rmtree(import_root, ignore_errors=True)

    def _target(self) -> _Deployment:
        target = _deployment(Path(self._directory.name) / "target", "prior")
        target.runtime.write_bytes(PRIOR_RUNTIME)
        target.tls.mkdir()
        (target.tls / PRIOR_TLS[0]).write_bytes(b"synthetic prior trust\n")
        return target

    def _staged_hot(self, target: _Deployment) -> list[Path]:
        return sorted(target.hot.parent.glob(f".{target.hot.name}.restore-*"))

    def _refused_import(
        self,
        target: _Deployment,
        before_refusal: Callable[[], None],
    ) -> tuple[BaseException, list[Path]]:
        """Fail configuration activation after history is staged, not activated."""
        retired: list[Path] = []

        def record_retirement(path: Path, **kwargs: Any) -> None:
            retired.append(Path(path))
            _ORIGINAL_RETIRE(path, **kwargs)

        def fail_configuration_folder(*_args: Any, **_kwargs: Any) -> None:
            before_refusal()
            raise RuntimeError("synthetic configuration folder activation failure")

        with (
            patch.object(
                _ImportActivationTransaction,
                "activate_directory",
                fail_configuration_folder,
            ),
            patch.object(
                segmented_restore_module,
                "_retire_authenticated",
                record_retirement,
            ),
        ):
            with self.assertRaises(Exception) as raised:
                target.service.import_bundle_from_file(
                    self.artifact.path,
                    passphrase=PASSPHRASE,
                )
        return raised.exception, retired

    def test_unconsumed_staged_hot_copy_is_retired_by_its_record(self) -> None:
        # Control: the staged copy is intact, rollback completes, and the
        # staged hot copy goes through the authenticated retirement.
        target = self._target()
        staged_seen: list[Path] = []

        def observe_staged_hot() -> None:
            staged_seen.extend(self._staged_hot(target))

        error, retired = self._refused_import(target, observe_staged_hot)

        self.assertIn("synthetic configuration folder activation failure", str(error))
        self.assertEqual(len(staged_seen), 1, staged_seen)
        staged = staged_seen[0]
        self.assertIn(staged, retired)
        self.assertFalse(os.path.lexists(staged))
        self.assertEqual(self._staged_hot(target), [])
        self.assertEqual(_hot_events(target.hot), ["prior-hot"])
        self.assertEqual(target.runtime.read_bytes(), PRIOR_RUNTIME)

    def test_replaced_staged_hot_copy_is_kept_not_unlinked(self) -> None:
        # The staged copy no longer matches its record: rollback must refuse
        # to delete it and keep it under the recorded name, not unlink it.
        target = self._target()
        staged_seen: list[Path] = []

        def replace_staged_hot() -> None:
            (staged,) = self._staged_hot(target)
            staged_seen.append(staged)
            replacement = staged.with_name(staged.name + ".replacement")
            replacement.write_bytes(_REPLACEMENT_BYTES)
            replacement.chmod(staged.stat().st_mode & 0o7777)
            os.replace(replacement, staged)

        error, retired = self._refused_import(target, replace_staged_hot)

        self.assertEqual(len(staged_seen), 1, staged_seen)
        staged = staged_seen[0]
        # The replacement is still under the recorded name, untouched.
        self.assertTrue(os.path.lexists(staged), "replaced staged hot copy was deleted")
        self.assertEqual(staged.read_bytes(), _REPLACEMENT_BYTES)
        self.assertIn(staged, retired)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("Import activation rollback is incomplete", str(error))
        self.assertIn(
            "Segmented restore recorded restore artifact integrity check failed.",
            str(error),
        )
        self.assertIn(str(staged), str(error))
        # Rollback itself still put the prior live state back.
        self.assertEqual(_hot_events(target.hot), ["prior-hot"])
        self.assertEqual(target.runtime.read_bytes(), PRIOR_RUNTIME)


if __name__ == "__main__":
    unittest.main()
