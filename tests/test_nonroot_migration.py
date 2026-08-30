from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/prepare_nonroot_bind_mounts.py"
SPEC = importlib.util.spec_from_file_location("prepare_nonroot_bind_mounts", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load non-root migration helper")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NonRootMigrationTests(unittest.TestCase):
    def test_inventory_accepts_regular_bounded_runtime_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config/config.yaml").write_text("systems: []\n", encoding="utf-8")
            (root / "config/ssh").mkdir()
            (root / "config/ssh/id_truenas").write_text("test-key", encoding="utf-8")
            (root / "config/tls").mkdir()
            (root / "config/tls/ca.pem").write_text("test-ca", encoding="utf-8")
            (root / "data").mkdir()

            entries = MODULE.inventory(root)

            self.assertEqual(
                {path.relative_to(root).as_posix() for path, _ in entries},
                {
                    "config",
                    "config/config.yaml",
                    "config/ssh",
                    "config/ssh/id_truenas",
                    "config/tls",
                    "config/tls/ca.pem",
                    "data",
                },
            )

    def test_inventory_preserves_backup_identity_and_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config/backup-secrets").mkdir(parents=True)
            (root / "config/backup-secrets/passphrase").write_text("secret", encoding="utf-8")
            (root / "backups").mkdir()
            (root / "backups/archive.tar.zst").write_bytes(b"backup")
            (root / "backup-status").mkdir()
            (root / "backup-status/status.json").write_text("{}", encoding="utf-8")
            (root / "data").mkdir()

            entries = MODULE.inventory(root)

            migrated = {path.relative_to(root).as_posix() for path, _ in entries}
            self.assertEqual(migrated, {"config", "data"})
            self.assertFalse(any(path.startswith("config/backup-secrets") for path in migrated))
            self.assertFalse(any(path.startswith("backups") for path in migrated))
            self.assertFalse(any(path.startswith("backup-status") for path in migrated))

    def test_inventory_rejects_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir()
            victim = root / "victim"
            victim.write_text("keep", encoding="utf-8")
            (root / "data/link").symlink_to(victim)

            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.inventory(root)

            self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_inventory_rejects_stale_replacement_artifacts(self) -> None:
        for artifact_kind in ("file", "directory"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / "history").mkdir()
                artifact = root / "history/.history.db.restore-deadbeef"
                if artifact_kind == "file":
                    artifact.write_bytes(b"stale")
                else:
                    artifact.mkdir()

                with self.assertRaisesRegex(ValueError, "stale replacement artifact"):
                    MODULE.inventory(root)

    def test_open_verified_closes_descriptor_when_fstat_fails(self) -> None:
        expected = os.stat_result(
            (stat.S_IFREG | 0o600, 1, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0)
        )

        with (
            patch.object(MODULE.os, "open", return_value=12),
            patch.object(MODULE.os, "fstat", side_effect=OSError("fstat-failed")),
            patch.object(MODULE.os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "fstat-failed"):
                MODULE.open_verified(Path("runtime.db"), expected)

        close.assert_called_once_with(12)

    def test_inventory_rejects_special_file_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir()
            os.mkfifo(root / "data/runtime.fifo")

            with self.assertRaisesRegex(ValueError, "non-file runtime entry"):
                MODULE.inventory(root)

    def test_inventory_rejects_dangling_symlink_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").symlink_to(root / "missing")

            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.inventory(root)

    @unittest.skipIf(os.geteuid() == 0, "root can traverse mode-000 test directories")
    def test_inventory_fails_instead_of_skipping_unreadable_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_root = root / "data"
            runtime_root.mkdir()
            (runtime_root / "cache.json").write_text("{}", encoding="utf-8")
            runtime_root.chmod(0)
            try:
                with self.assertRaises(PermissionError):
                    MODULE.inventory(root)
            finally:
                runtime_root.chmod(0o700)

    def test_apply_opens_every_entry_before_first_ownership_change(self) -> None:
        first = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0))
        second = os.stat_result((stat.S_IFREG | 0o600, 2, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0))
        entries = [(Path("first"), first), (Path("second"), second)]

        with (
            patch.object(MODULE, "open_verified", side_effect=[10, OSError("changed")]),
            patch.object(MODULE.os, "fchown") as fchown,
            patch.object(MODULE.os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "changed"):
                MODULE.apply_ownership(entries, uid=10001, gid=10001)

        fchown.assert_not_called()
        close.assert_called_once_with(10)

    def test_apply_changes_only_app_paths_and_preserves_backup_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(mode=0o700)
            app_file = root / "data/cache.json"
            app_file.write_text("{}", encoding="utf-8")
            app_file.chmod(0o600)
            (root / "config/backup-secrets").mkdir(parents=True, mode=0o700)
            secret = root / "config/backup-secrets/passphrase"
            secret.write_text("secret", encoding="utf-8")
            secret.chmod(0o600)
            secret_before = secret.stat()

            MODULE.apply_ownership(
                MODULE.inventory(root),
                uid=os.getuid(),
                gid=os.getgid(),
            )

            self.assertEqual(stat.S_IMODE((root / "data").stat().st_mode), 0o770)
            self.assertEqual(stat.S_IMODE(app_file.stat().st_mode), 0o660)
            secret_after = secret.stat()
            self.assertEqual(secret.read_text(encoding="utf-8"), "secret")
            self.assertEqual(stat.S_IMODE(secret_after.st_mode), 0o600)
            self.assertEqual(
                (secret_after.st_uid, secret_after.st_gid),
                (secret_before.st_uid, secret_before.st_gid),
            )

    def test_apply_rolls_back_all_changed_descriptors_after_mid_apply_failure(self) -> None:
        first = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, 2001, 2002, 0, 0, 0, 0))
        second = os.stat_result((stat.S_IFREG | 0o640, 2, 1, 1, 3001, 3002, 0, 0, 0, 0))
        entries = [(Path("first"), first), (Path("second"), second)]

        with (
            patch.object(MODULE, "open_verified", side_effect=[10, 11]),
            patch.object(MODULE, "_verify_path_identity"),
            patch.object(MODULE.os, "fchown") as fchown,
            patch.object(
                MODULE.os,
                "fchmod",
                side_effect=[None, OSError("chmod failed"), None, None],
            ),
            patch.object(MODULE.os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "chmod failed"):
                MODULE.apply_ownership(entries, uid=10001, gid=10001)

        self.assertEqual(
            fchown.call_args_list,
            [
                call(10, 10001, 10001),
                call(11, 10001, 10001),
                call(11, 3001, 3002),
                call(10, 2001, 2002),
            ],
        )
        self.assertEqual(close.call_args_list, [call(11), call(10)])

    def test_apply_attempts_every_descriptor_close_and_reports_failure(self) -> None:
        metadata = os.stat_result(
            (stat.S_IFREG | 0o660, 1, 1, 1, 10001, 10001, 0, 0, 0, 0)
        )
        entries = [(Path("first"), metadata), (Path("second"), metadata)]

        with (
            patch.object(MODULE, "open_verified", side_effect=[10, 11]),
            patch.object(MODULE, "_verify_path_identity"),
            patch.object(
                MODULE.os,
                "close",
                side_effect=[OSError("close-11-failed"), None],
            ) as close,
        ):
            with self.assertRaisesRegex(RuntimeError, "descriptor cleanup failed"):
                MODULE.apply_ownership(entries, uid=10001, gid=10001)

        self.assertEqual(close.call_args_list, [call(11), call(10)])


if __name__ == "__main__":
    unittest.main()
