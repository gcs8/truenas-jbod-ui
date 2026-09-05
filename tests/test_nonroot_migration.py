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

    def test_inventory_rejects_tree_beyond_explicit_depth_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "data"
            current.mkdir()
            for _ in range(257):
                current /= "d"
                current.mkdir()

            with self.assertRaisesRegex(ValueError, "bounded depth limit"):
                MODULE.inventory(root)

    def test_inventory_does_not_retain_one_descriptor_per_tree_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "data"
            current.mkdir()
            for _ in range(200):
                current /= "d"
                current.mkdir()
            (current / "runtime.db").write_bytes(b"runtime")

            soft_limit, hard_limit = MODULE.resource.getrlimit(MODULE.resource.RLIMIT_NOFILE)
            open_count = len(tuple(Path("/proc/self/fd").iterdir()))
            test_limit = max(open_count + 16, 48)
            if hard_limit != MODULE.resource.RLIM_INFINITY and hard_limit < test_limit:
                self.skipTest("descriptor hard limit is too small for the bounded fixture")

            MODULE.resource.setrlimit(MODULE.resource.RLIMIT_NOFILE, (test_limit, hard_limit))
            try:
                entries = MODULE.inventory(root)
            finally:
                MODULE.resource.setrlimit(
                    MODULE.resource.RLIMIT_NOFILE,
                    (soft_limit, hard_limit),
                )

            self.assertIn(current / "runtime.db", {path for path, _ in entries})

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

    def test_inventory_rejects_intermediate_symlink_substitution_during_descriptor_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "data/nested"
            nested.mkdir(parents=True)
            (nested / "runtime.db").write_bytes(b"runtime")
            parked = root / "parked-nested"
            real_open = os.open
            substituted = False

            def substitute_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal substituted
                if path == "nested" and dir_fd is not None and not substituted:
                    substituted = True
                    nested.rename(parked)
                    nested.symlink_to(parked, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch.object(MODULE.os, "open", side_effect=substitute_before_open),
                self.assertRaisesRegex(ValueError, "symlink"),
            ):
                MODULE.inventory(root)

            self.assertTrue(substituted)
            self.assertEqual((parked / "runtime.db").read_bytes(), b"runtime")

    def test_inventory_rejects_intermediate_directory_rebind_during_descriptor_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "data/nested"
            nested.mkdir(parents=True)
            (nested / "runtime.db").write_bytes(b"runtime")
            parked = root / "parked-nested"
            replacement = root / "replacement-nested"
            replacement.mkdir()
            (replacement / "runtime.db").write_bytes(b"replacement")
            real_open = os.open
            rebound = False

            def rebind_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal rebound
                if path == "nested" and dir_fd is not None and not rebound:
                    rebound = True
                    nested.rename(parked)
                    replacement.rename(nested)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with (
                patch.object(MODULE.os, "open", side_effect=rebind_before_open),
                self.assertRaisesRegex(ValueError, "changed during descriptor binding"),
            ):
                MODULE.inventory(root)

            self.assertTrue(rebound)
            self.assertEqual((parked / "runtime.db").read_bytes(), b"runtime")
            self.assertEqual((nested / "runtime.db").read_bytes(), b"replacement")

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

    def test_bind_child_closes_descriptor_when_fstat_fails(self) -> None:
        expected = os.stat_result(
            (stat.S_IFREG | 0o600, 1, 1, 1, os.getuid(), os.getgid(), 0, 0, 0, 0)
        )

        with (
            patch.object(MODULE.os, "stat", return_value=expected),
            patch.object(MODULE.os, "open", return_value=12),
            patch.object(MODULE.os, "fstat", side_effect=OSError("fstat-failed")),
            patch.object(MODULE.os, "close") as close,
        ):
            with self.assertRaisesRegex(OSError, "fstat-failed"):
                MODULE._bind_child(9, "runtime.db", Path("runtime.db"))

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

    def test_inventory_rejects_symlink_in_deployment_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            real_root = parent / "real-deployment"
            real_root.mkdir()
            (real_root / "data").mkdir()
            linked_root = parent / "linked-deployment"
            linked_root.symlink_to(real_root, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.inventory(linked_root)

    def test_inventory_fails_closed_without_descriptor_relative_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            MODULE,
            "_OPEN_SUPPORTS_DIR_FD",
            False,
        ):
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                MODULE.inventory(Path(temp_dir))

    def test_inventory_rejects_ambiguous_parent_traversal_in_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            root = parent / "deployment"
            root.mkdir()
            (root / "data").mkdir()
            ambiguous_root = parent / "unused" / ".." / "deployment"

            with self.assertRaisesRegex(ValueError, "parent traversal"):
                MODULE.inventory(ambiguous_root)

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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(mode=0o700)
            (root / "data/first").write_bytes(b"first")
            (root / "data/second").write_bytes(b"second")
            entries = MODULE.inventory(root)
            events: list[tuple[str, Path | int]] = []
            real_bind_child = MODULE._bind_child

            def recording_bind_child(parent_descriptor, name, path, **requirements):
                result = real_bind_child(
                    parent_descriptor,
                    name,
                    path,
                    **requirements,
                )
                events.append(("bind", path))
                return result

            def recording_fchown(descriptor, uid, gid):
                events.append(("chown", descriptor))

            with (
                patch.object(MODULE, "_bind_child", side_effect=recording_bind_child),
                patch.object(MODULE.os, "fchown", side_effect=recording_fchown) as fchown,
                patch.object(MODULE.os, "fchmod"),
            ):
                MODULE.apply_ownership(
                    entries,
                    uid=os.getuid() + 1,
                    gid=os.getgid() + 1,
                )

            first_chown = next(index for index, event in enumerate(events) if event[0] == "chown")
            self.assertFalse(any(event[0] == "bind" for event in events[first_chown:]))
            self.assertEqual(fchown.call_count, len(entries))

    def test_apply_uses_bound_parent_after_intermediate_directory_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            original_file = data_root / "nested/runtime.db"
            original_file.parent.mkdir(parents=True, mode=0o700)
            original_file.write_bytes(b"approved")
            original_file.chmod(0o600)
            entries = MODULE.inventory(root)

            replacement_nested = data_root / "replacement-nested"
            replacement_file = replacement_nested / "runtime.db"
            replacement_nested.mkdir(mode=0o700)
            replacement_file.write_bytes(b"outside")
            replacement_file.chmod(0o600)
            parked_nested = data_root / "parked-nested"
            real_open = os.open
            rebound = False

            def rebind_parent_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal rebound
                if path == "runtime.db" and dir_fd is not None and not rebound:
                    rebound = True
                    original_file.parent.rename(parked_nested)
                    replacement_nested.rename(original_file.parent)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch.object(MODULE.os, "open", side_effect=rebind_parent_before_open):
                MODULE.apply_ownership(entries, uid=os.getuid(), gid=os.getgid())

            self.assertTrue(rebound)
            self.assertEqual(stat.S_IMODE(parked_nested.stat().st_mode), 0o770)
            self.assertEqual(stat.S_IMODE((parked_nested / "runtime.db").stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE(original_file.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(original_file.stat().st_mode), 0o600)
            self.assertEqual((parked_nested / "runtime.db").read_bytes(), b"approved")
            self.assertEqual(original_file.read_bytes(), b"outside")

    def test_apply_rejects_deployment_root_rebind_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            root = parent / "deployment"
            (root / "data").mkdir(parents=True, mode=0o700)
            entries = MODULE.inventory(root)
            parked_root = parent / "parked-deployment"
            root.rename(parked_root)
            (root / "data").mkdir(parents=True, mode=0o700)

            with (
                patch.object(MODULE.os, "fchown") as fchown,
                self.assertRaisesRegex(ValueError, "deployment root changed"),
            ):
                MODULE.apply_ownership(entries, uid=os.getuid(), gid=os.getgid())

            fchown.assert_not_called()
            self.assertEqual(stat.S_IMODE((parked_root / "data").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "data").stat().st_mode), 0o700)

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
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data").mkdir(mode=0o700)
            runtime_file = root / "data/runtime.db"
            runtime_file.write_bytes(b"runtime")
            runtime_file.chmod(0o600)
            entries = MODULE.inventory(root)
            original_ids = [
                (metadata.st_uid, metadata.st_gid)
                for _, metadata in entries
            ]
            target_uid = os.getuid() + 1
            target_gid = os.getgid() + 1

            with (
                patch.object(MODULE.os, "fchown") as fchown,
                patch.object(
                    MODULE.os,
                    "fchmod",
                    side_effect=[None, OSError("chmod failed"), None, None],
                ),
            ):
                with self.assertRaisesRegex(OSError, "chmod failed"):
                    MODULE.apply_ownership(entries, uid=target_uid, gid=target_gid)

            calls = fchown.call_args_list
            self.assertEqual(len(calls), 4)
            changed_descriptors = [calls[0].args[0], calls[1].args[0]]
            self.assertEqual(
                [(item.args[1], item.args[2]) for item in calls],
                [
                    (target_uid, target_gid),
                    (target_uid, target_gid),
                    original_ids[1],
                    original_ids[0],
                ],
            )
            self.assertEqual(
                [calls[2].args[0], calls[3].args[0]],
                list(reversed(changed_descriptors)),
            )

    def test_close_opened_descriptors_attempts_every_close_and_reports_failure(self) -> None:
        metadata = os.stat_result(
            (stat.S_IFREG | 0o660, 1, 1, 1, 10001, 10001, 0, 0, 0, 0)
        )
        opened = [(10, Path("first"), metadata), (11, Path("second"), metadata)]

        with patch.object(
            MODULE.os,
            "close",
            side_effect=[OSError("close-11-failed"), None],
        ) as close:
            with self.assertRaisesRegex(RuntimeError, "descriptor cleanup failed"):
                MODULE._close_opened_descriptors(opened)

        self.assertEqual(close.call_args_list, [call(11), call(10)])


if __name__ == "__main__":
    unittest.main()
