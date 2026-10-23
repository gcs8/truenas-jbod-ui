from __future__ import annotations

import importlib
import importlib.util
import os
import resource
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call, patch

import yaml


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

            soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
            open_count = len(tuple(Path("/proc/self/fd").iterdir()))
            test_limit = max(open_count + 16, 48)
            if hard_limit != resource.RLIM_INFINITY and hard_limit < test_limit:
                self.skipTest("descriptor hard limit is too small for the bounded fixture")

            resource.setrlimit(resource.RLIMIT_NOFILE, (test_limit, hard_limit))
            try:
                entries = MODULE.inventory(root)
            finally:
                resource.setrlimit(
                    resource.RLIMIT_NOFILE,
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

    def _assert_regular_file_alias_rejected(self, phase: str) -> None:
        for selected_name in ("config/config.yaml", "data/nested/runtime.db"):
            for excluded_root in ("config/backup-secrets", "backups", "backup-status"):
                with (
                    self.subTest(selected=selected_name, excluded=excluded_root),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    root = Path(temp_dir)
                    selected = root / selected_name
                    selected.parent.mkdir(parents=True, mode=0o700)
                    selected.write_bytes(b"synthetic runtime")
                    selected.chmod(0o600)
                    for name in ("config/backup-secrets", "backups", "backup-status"):
                        directory = root / name
                        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        sentinel = directory / "untouched"
                        sentinel.write_bytes(b"synthetic excluded")
                        sentinel.chmod(0o600)
                    alias = root / excluded_root / "alias"

                    def snapshot(path):
                        metadata = path.stat()
                        return (
                            metadata.st_uid,
                            metadata.st_gid,
                            stat.S_IMODE(metadata.st_mode),
                            path.read_bytes() if path.is_file() else None,
                        )

                    watched = [root, *root.rglob("*")]
                    before = {path: snapshot(path) for path in watched}
                    alias_before = snapshot(selected)
                    self.assertEqual(alias_before[:2], (os.getuid(), os.getgid()))
                    entries = MODULE.inventory(root) if phase.startswith("apply") else None
                    linked = False
                    opened = []
                    closed = []
                    real_open, real_close = os.open, os.close
                    real_bind_child = MODULE._bind_child

                    def add_alias():
                        nonlocal linked
                        os.link(selected, alias)
                        linked = True
                        self.assertEqual(selected.stat().st_nlink, 2)
                        self.assertEqual(alias.stat().st_ino, selected.stat().st_ino)

                    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
                        if phase.endswith("during_open") and path == selected.name and not linked:
                            add_alias()
                        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                        opened.append(descriptor)
                        return descriptor

                    def recording_close(descriptor):
                        closed.append(descriptor)
                        return real_close(descriptor)

                    def link_after_bind(parent_descriptor, name, path, **requirements):
                        result = real_bind_child(parent_descriptor, name, path, **requirements)
                        if phase == "apply_after_bind" and path == selected and not linked:
                            add_alias()
                        return result

                    if phase in {"inventory", "apply"}:
                        add_alias()
                    rejection = None
                    with (
                        patch.object(MODULE.os, "open", side_effect=recording_open),
                        patch.object(MODULE.os, "close", side_effect=recording_close),
                        patch.object(MODULE, "_bind_child", side_effect=link_after_bind),
                        patch.object(MODULE.os, "fchown", wraps=os.fchown) as fchown,
                        patch.object(MODULE.os, "fchmod", wraps=os.fchmod) as fchmod,
                    ):
                        try:
                            if entries is None:
                                MODULE.inventory(root)
                            else:
                                MODULE.apply_ownership(entries, uid=os.getuid(), gid=os.getgid())
                        except ValueError as exc:
                            rejection = exc

                    self.assertTrue(linked)
                    self.assertCountEqual(opened, closed)
                    self.assertEqual({path: snapshot(path) for path in watched}, before)
                    self.assertEqual(snapshot(alias), alias_before)
                    fchown.assert_not_called()
                    fchmod.assert_not_called()
                    self.assertIsNotNone(rejection, "multiply-linked regular file was admitted")
                    self.assertRegex(str(rejection), "multiply-linked regular file")

    def test_inventory_rejects_multiply_linked_regular_file(self) -> None:
        self._assert_regular_file_alias_rejected("inventory")

    def test_inventory_rejects_regular_file_linked_during_descriptor_open(self) -> None:
        self._assert_regular_file_alias_rejected("inventory_during_open")

    def test_apply_rejects_regular_file_linked_after_inventory_before_any_change(self) -> None:
        self._assert_regular_file_alias_rejected("apply")

    def test_apply_rejects_regular_file_linked_during_descriptor_open(self) -> None:
        self._assert_regular_file_alias_rejected("apply_during_open")

    def test_apply_rechecks_regular_file_link_count_after_descriptor_binding(self) -> None:
        self._assert_regular_file_alias_rejected("apply_after_bind")

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


class _DisposableWriterAuthority:
    """Closed-world fixture, NOT a backend for product or arbitrary host writers.

    Only this fixture's launch path is controlled. Direct Popen/os.link/os.rename
    bypass it, deliberately demonstrated by the negative controls below.
    """

    def __init__(self, model, deployment):
        self.model = model
        self.deployment = deployment
        self.lock = threading.Lock()
        self.held = False
        self.children = {}
        self.events = []
        self.lease_id = "synthetic-generation-1"
        self.transform: Callable[[Any], Any] = lambda evidence: evidence
        self.ignore_stop = set()
        self.stop_error: BaseException | None = None

    def launch(self, writer, code="import sys; sys.stdin.read()", *args):
        with self.lock:
            if self.held:
                self.events.append(("denied", writer.name))
                raise self.model.FenceRefused("new writer admission closed")
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", code, *map(str, args)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.children[writer.name] = child
            return child

    def acquire(self, deployment):
        with self.lock:
            if self.held or deployment != self.deployment:
                raise self.model.FenceRefused("exclusive deployment ownership unavailable")
            self.held = True
            self.events.append(("acquire", deployment.key))
            return self

    def inspect(self):
        metadata = self.deployment.root.stat()
        observations = tuple(
            self.model.Observation(
                writer,
                "running" if writer.name in self.children
                and self.children[writer.name].poll() is None else "stopped",
                f"synthetic-slot-{writer.name}",
            )
            for writer in self.deployment.writers
        )
        self.events.append(("inspect", None))
        return self.transform(self.model.Evidence(
            self.deployment, self.lease_id, (metadata.st_dev, metadata.st_ino),
            self.held, self.held, True, observations,
        ))

    def stop(self, writer):
        self.events.append(("stop", writer.name))
        if self.stop_error:
            raise self.stop_error
        child = self.children.get(writer.name)
        if child is not None and child.poll() is None and writer.name not in self.ignore_stop:
            child.terminate()
            child.wait(timeout=5)

    def cleanup(self):
        # Fixture-only cleanup; the coordinator never reopens admission.
        for child in self.children.values():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


class NonRootWriterFenceDraftTests(unittest.TestCase):
    def setUp(self):
        self.fence = importlib.import_module("scripts.nonroot_writer_fence_draft")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.writers = tuple(
            self.fence.Writer(role, role, "synthetic-owner")
            for role in ("ui", "history", "admin", "backup", "backup-scheduler", "host-job")
        )
        self.deployment = self.fence.Deployment(
            "synthetic-deployment", "synthetic-owner", self.root, self.writers, ("host-job",),
        )
        self.backend = _DisposableWriterAuthority(self.fence, self.deployment)
        self.addCleanup(self.backend.cleanup)

    def coordinator(self, deployment=None, backend=None):
        return self.fence.Coordinator(
            deployment if deployment is not None else self.deployment,
            backend if backend is not None else self.backend,
        )

    def test_draft_core_roles_cover_supported_compose_writers(self):
        # Source-contract coverage only, not independent runtime discovery.
        for compose_name in ("docker-compose.yml", "docker-compose.dev.yml"):
            with self.subTest(compose=compose_name):
                services = yaml.safe_load(
                    (SCRIPT_PATH.parent.parent / compose_name).read_text(encoding="utf-8")
                )["services"]
                writer_roles = set()
                for name, service in services.items():
                    for binding in service.get("volumes", []):
                        # Fail visibly if Compose changes to an unmodeled syntax.
                        self.assertIsInstance(binding, str)
                        fields = binding.split(":")
                        self.assertIn(len(fields), (2, 3))
                        modes = fields[2].split(",") if len(fields) == 3 else []
                        if fields[0].startswith("./") and "ro" not in modes:
                            self.assertTrue(name.startswith("enclosure-"))
                            writer_roles.add(name.removeprefix("enclosure-"))
                self.assertIn("backup", writer_roles)
                self.assertEqual(self.fence.CORE_ROLES, writer_roles)
                self.assertEqual(
                    {writer.role for writer in self.writers if writer.role != "host-job"},
                    writer_roles,
                )

    def test_draft_omitted_backup_is_refused_before_acquisition_or_callbacks(self):
        declaration = replace(
            self.deployment,
            writers=tuple(writer for writer in self.writers if writer.role != "backup"),
        )
        # Match the incomplete declaration so backend mismatch cannot mask admission.
        authority = _DisposableWriterAuthority(self.fence, declaration)
        self.addCleanup(authority.cleanup)
        coordinator = self.coordinator(declaration, authority)
        inventory, mutate = Mock(), Mock()
        with self.assertRaisesRegex(self.fence.FenceRefused, "all core writer roles"):
            coordinator.run(inventory, mutate)
        inventory.assert_not_called()
        mutate.assert_not_called()
        self.assertEqual(authority.events, [])
        self.assertFalse(authority.held)
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)

    def test_draft_running_backup_is_stopped_and_verified_before_callbacks(self):
        backup = next(writer for writer in self.writers if writer.role == "backup")
        child = self.backend.launch(backup)
        self.assertIsNone(child.poll())
        coordinator = self.coordinator()

        def inventory(root, check):
            self.assertEqual(root, self.root)
            self.assertIsNotNone(child.poll())
            self.assertEqual(
                self.backend.events,
                [("acquire", self.deployment.key), ("inspect", None)]
                + [("stop", writer.name) for writer in self.writers]
                + [("inspect", None)],
            )
            self.backend.events.append(("inventory", backup.name))
            check()
            return "synthetic-backup-plan"

        def mutate(plan, check):
            self.assertEqual(plan, "synthetic-backup-plan")
            self.assertIsNotNone(child.poll())
            self.assertIn(("inventory", backup.name), self.backend.events)
            check()
            self.backend.events.append(("mutate", backup.name))

        refusal = None
        try:
            coordinator.run(inventory, mutate)
        except self.fence.FenceRefused as exc:
            refusal = exc
        self.assertIsNone(refusal, "supported one-shot backup must be admitted and stopped")
        self.assertIn(("mutate", backup.name), self.backend.events)
        self.assertEqual(coordinator.phase, self.fence.Phase.COMPLETE_HELD)
        self.assertTrue(self.backend.held)

    def test_draft_unstopped_backup_is_refused_before_inventory_or_mutation(self):
        backup = next(writer for writer in self.writers if writer.role == "backup")
        child = self.backend.launch(backup)
        self.assertIsNone(child.poll())
        self.backend.ignore_stop.add(backup.name)
        coordinator = self.coordinator()
        inventory, mutate = Mock(), Mock()
        with self.assertRaisesRegex(self.fence.FenceRefused, "writer termination not verified"):
            coordinator.run(inventory, mutate)
        inventory.assert_not_called()
        mutate.assert_not_called()
        self.assertIn(("stop", backup.name), self.backend.events)
        self.assertEqual(self.backend.events[-1], ("inspect", None))
        self.assertIsNone(child.poll())
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
        self.assertTrue(self.backend.held)

    def test_draft_concurrent_declared_launches_are_denied_through_both_windows(self):
        coordinator = self.coordinator()
        denied = []

        def attempt(writer):
            try:
                self.backend.launch(writer)
            except self.fence.FenceRefused:
                return writer.name
            self.fail("synthetic writer escaped closed admission")

        def window(*args):
            check = args[-1]
            check()
            with ThreadPoolExecutor(max_workers=len(self.writers)) as pool:
                denied.append(set(pool.map(attempt, self.writers)))
            check()

        coordinator.run(window, window)
        self.assertEqual(denied, [{writer.name for writer in self.writers}] * 2)
        self.assertEqual(self.backend.children, {})
        self.assertEqual(coordinator.phase, self.fence.Phase.COMPLETE_HELD)

    def test_draft_requires_an_explicit_backend_and_never_calls_callbacks_without_it(self):
        coordinator = self.fence.Coordinator(self.deployment)
        inventory, mutate = Mock(), Mock()
        with self.assertRaisesRegex(self.fence.FenceRefused, "backend"):
            coordinator.run(inventory, mutate)
        inventory.assert_not_called()
        mutate.assert_not_called()
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)

    def test_draft_rejects_incomplete_or_unowned_declarations_before_acquisition(self):
        cases = (
            replace(self.deployment, owner=""),
            replace(self.deployment, key=""),
            replace(self.deployment, root=Path("relative")),
            replace(self.deployment, root=self.root / ".." / "other"),
            replace(self.deployment, writers=self.writers[:-1]),
            replace(self.deployment, writers=self.writers[1:]),
            replace(self.deployment, writers=(*self.writers, self.writers[0])),
            replace(self.deployment, host_jobs=()),
            replace(self.deployment, host_jobs=None),
            replace(self.deployment, writers=(replace(self.writers[0], owner="foreign"), *self.writers[1:])),
            replace(self.deployment, writers=(replace(self.writers[0], role="unknown"), *self.writers[1:])),
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                inventory, mutate = Mock(), Mock()
                with self.assertRaises(self.fence.FenceRefused):
                    self.coordinator(declaration).run(inventory, mutate)
                inventory.assert_not_called()
                mutate.assert_not_called()
                self.assertFalse(self.backend.held)
                self.assertEqual(self.backend.events, [])

    def test_draft_explicit_empty_host_job_set_is_supported_not_inferred(self):
        declaration = replace(self.deployment, writers=self.writers[:-1], host_jobs=())
        authority = _DisposableWriterAuthority(self.fence, declaration)
        self.addCleanup(authority.cleanup)
        coordinator = self.coordinator(declaration, authority)
        coordinator.run(lambda root, check: check(), lambda plan, check: check())
        self.assertEqual(coordinator.phase, self.fence.Phase.COMPLETE_HELD)
        self.assertTrue(authority.held)

    def test_draft_stops_and_verifies_all_owned_processes_before_inventory(self):
        children = [self.backend.launch(writer) for writer in self.writers]
        coordinator = self.coordinator()
        events = []

        def inventory(root, check):
            check()
            self.assertEqual(root, self.root)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertEqual(
                [name for event, name in self.backend.events if event == "stop"],
                [writer.name for writer in self.writers],
            )
            events.append("inventory")
            return ("synthetic-plan",)

        def mutate(plan, check):
            check()
            self.assertEqual(plan, ("synthetic-plan",))
            events.append("mutate")

        coordinator.run(inventory, mutate)
        self.assertEqual(events, ["inventory", "mutate"])
        self.assertEqual(coordinator.phase, self.fence.Phase.COMPLETE_HELD)
        with self.assertRaises(self.fence.FenceRefused):
            self.backend.launch(self.writers[0])

    def test_draft_refuses_missing_extra_unowned_unknown_or_unverifiable_observations(self):
        original = replace(self.backend.inspect(), exclusive=True, admission_closed=True)
        first = original.observations[0]
        cases = (
            replace(original, observations=original.observations[1:]),
            replace(original, observations=(*original.observations, first)),
            replace(original, observations=(*original.observations, replace(first, writer=replace(first.writer, name="undeclared")))),
            replace(original, observations=(replace(first, writer=replace(first.writer, owner="foreign")), *original.observations[1:])),
            replace(original, observations=(replace(first, state="unknown"), *original.observations[1:])),
            replace(original, observations=(replace(first, identity=""), *original.observations[1:])),
            replace(original, inventory_complete=False),
            replace(original, admission_closed=False),
            replace(original, exclusive=False),
            replace(original, lease_id=""),
            replace(original, deployment=replace(self.deployment, key="other")),
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                authority = _DisposableWriterAuthority(self.fence, self.deployment)
                self.addCleanup(authority.cleanup)
                authority.transform = lambda value, evidence=evidence: evidence
                coordinator = self.coordinator(backend=authority)
                inventory, mutate = Mock(), Mock()
                with self.assertRaises(self.fence.FenceRefused):
                    coordinator.run(inventory, mutate)
                inventory.assert_not_called()
                mutate.assert_not_called()
                self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
                self.assertTrue(authority.held)

    def test_draft_stop_failure_or_still_running_writer_never_reaches_inventory(self):
        for failure in ("stop-error", "still-running"):
            with self.subTest(failure=failure):
                authority = _DisposableWriterAuthority(self.fence, self.deployment)
                self.addCleanup(authority.cleanup)
                authority.launch(self.writers[-1])
                if failure == "stop-error":
                    authority.stop_error = OSError("synthetic stop failure")
                else:
                    authority.ignore_stop.add(self.writers[-1].name)
                coordinator = self.coordinator(backend=authority)
                inventory, mutate = Mock(), Mock()
                with self.assertRaises((OSError, self.fence.FenceRefused)):
                    coordinator.run(inventory, mutate)
                inventory.assert_not_called()
                mutate.assert_not_called()
                self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
                self.assertTrue(authority.held)

    def test_draft_identity_or_authority_drift_after_inventory_prevents_mutation(self):
        for field in ("lease", "root", "writer", "gate", "inspection-error"):
            with self.subTest(field=field):
                authority = _DisposableWriterAuthority(self.fence, self.deployment)
                self.addCleanup(authority.cleanup)
                coordinator = self.coordinator(backend=authority)

                def inventory(root, check):
                    check()

                    def changed(evidence):
                        if field == "lease":
                            return replace(evidence, lease_id="replaced-generation")
                        if field == "root":
                            return replace(evidence, root_identity=(0, 0))
                        if field == "writer":
                            first, *rest = evidence.observations
                            return replace(evidence, observations=(replace(first, identity="replacement"), *rest))
                        if field == "gate":
                            return replace(evidence, admission_closed=False)
                        raise OSError("synthetic inspection unavailable")

                    authority.transform = changed

                mutate = Mock()
                with self.assertRaises((OSError, self.fence.FenceRefused)):
                    coordinator.run(inventory, mutate)
                mutate.assert_not_called()
                self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
                self.assertTrue(authority.held)

    def test_draft_failure_or_interrupt_holds_admission_and_disallows_reuse(self):
        for error in (ValueError("inventory"), KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(error=type(error).__name__):
                authority = _DisposableWriterAuthority(self.fence, self.deployment)
                self.addCleanup(authority.cleanup)
                coordinator = self.coordinator(backend=authority)
                with self.assertRaises(type(error)):
                    coordinator.run(Mock(side_effect=error), Mock())
                with self.assertRaises(self.fence.FenceRefused):
                    authority.launch(self.writers[0])
                with self.assertRaisesRegex(self.fence.FenceRefused, "single-use"):
                    coordinator.run(Mock(), Mock())
                self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)

    def test_draft_excludes_competing_coordinator_and_reentrant_run(self):
        coordinator = self.coordinator()
        competitor = self.coordinator()

        def inventory(root, check):
            for candidate in (coordinator, competitor):
                mutate = Mock()
                with self.assertRaises(self.fence.FenceRefused):
                    candidate.run(Mock(), mutate)
                mutate.assert_not_called()
            check()

        coordinator.run(inventory, lambda plan, check: check())
        self.assertEqual(coordinator.phase, self.fence.Phase.COMPLETE_HELD)
        with self.assertRaises(self.fence.FenceRefused):
            self.coordinator().run(Mock(), Mock())

    def test_draft_mutation_failure_never_reopens_gate_or_reports_completion(self):
        coordinator = self.coordinator()
        with self.assertRaisesRegex(OSError, "mutation"):
            coordinator.run(lambda root, check: check(), Mock(side_effect=OSError("mutation")))
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
        self.assertTrue(self.backend.held)
        with self.assertRaises(self.fence.FenceRefused):
            self.backend.launch(self.writers[-1])

    def test_draft_post_mutation_verification_cannot_report_success_after_lost_authority(self):
        coordinator = self.coordinator()

        def mutate(plan, check):
            check()
            self.backend.lease_id = "lost-lease"

        with self.assertRaises(self.fence.FenceRefused):
            coordinator.run(lambda root, check: check(), mutate)
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)

    def test_draft_swallowed_checkpoint_failure_cannot_restore_a_successful_transition(self):
        coordinator = self.coordinator()
        mutate = Mock()

        def inventory(root, check):
            self.backend.transform = lambda evidence: replace(evidence, admission_closed=False)
            with self.assertRaises(self.fence.FenceRefused):
                check()
            self.backend.transform = lambda evidence: evidence

        with self.assertRaises(self.fence.FenceRefused):
            coordinator.run(inventory, mutate)
        mutate.assert_not_called()
        self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)

    def test_draft_hardlink_and_rename_writer_attempts_cannot_reach_mutation(self):
        # These are admission tests, not a fix for arbitrary external writers.
        # First prove each real subprocess attack is effective without the fence.
        attack = (
            "import os, sys; "
            "os.link(sys.argv[2], sys.argv[3]) if sys.argv[1] == 'link' "
            "else os.rename(sys.argv[2], sys.argv[3])"
        )
        for operation in ("link", "rename"):
            for stage in ("inventory", "after-bind", "before-ownership"):
                with self.subTest(operation=operation, stage=stage):
                    selected = self.root / f"selected-{operation}-{stage}"
                    alias = self.root / f"excluded-{operation}-{stage}"
                    selected.write_bytes(b"synthetic runtime")
                    selected.chmod(0o600)
                    metadata = selected.stat()
                    child = self.backend.launch(self.writers[-1], attack, operation, selected, alias)
                    child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0)
                    self.assertEqual(alias.stat().st_ino, metadata.st_ino)
                    if operation == "link":
                        self.assertEqual(alias.stat().st_nlink, 2)
                    else:
                        self.assertFalse(selected.exists())
                        self.assertEqual(alias.stat().st_nlink, 1)
                        alias.rename(selected)
                    if alias.exists():
                        alias.unlink()
                    authority = _DisposableWriterAuthority(self.fence, self.deployment)
                    self.addCleanup(authority.cleanup)
                    coordinator = self.coordinator(backend=authority)
                    ownership = Mock()

                    def attempt():
                        authority.launch(self.writers[-1], attack, operation, selected, alias)

                    def inventory(root, check):
                        check()
                        if stage == "inventory":
                            attempt()
                        return selected

                    def mutate(path, check):
                        # Bind a real descriptor then attempt the namespace attack
                        # at each reported vulnerable point. No chown is issued.
                        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        try:
                            if stage == "after-bind":
                                attempt()
                            check()
                            if stage == "before-ownership":
                                attempt()
                            ownership(descriptor)
                        finally:
                            os.close(descriptor)

                    with self.assertRaisesRegex(self.fence.FenceRefused, "admission closed"):
                        coordinator.run(inventory, mutate)
                    ownership.assert_not_called()
                    self.assertFalse(alias.exists())
                    after = selected.stat()
                    self.assertEqual(
                        (after.st_ino, after.st_nlink, after.st_uid, after.st_gid, after.st_mode),
                        (metadata.st_ino, 1, metadata.st_uid, metadata.st_gid, metadata.st_mode),
                    )
                    self.assertEqual(selected.read_bytes(), b"synthetic runtime")
                    self.assertEqual(coordinator.phase, self.fence.Phase.FAILED)
                    self.assertIn(("denied", self.writers[-1].name), authority.events)


if __name__ == "__main__":
    unittest.main()
