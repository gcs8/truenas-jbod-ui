"""Credential modes at creation, without starting QA runtimes or privileged commands."""
from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_compose_runtime_matrix as matrix
from scripts import run_image_upgrade_smoke as smoke
from scripts import run_private_qa_restore as restore


ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def creation_modes(target: Path):
    """Observe the real descriptor before any caller can write or chmod it."""
    seen: list[int] = []
    real_open = os.open
    real_io_open = io.open

    def observe(path, descriptor: int) -> None:
        if isinstance(path, (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(path))
            if path.parent == target.parent and (
                path.name == target.name
                or (path.name.startswith(target.name + ".") and path.name.endswith(".tmp"))
            ):
                seen.append(stat.S_IMODE(os.fstat(descriptor).st_mode))

    def open_descriptor(path, flags, mode=0o777, **kwargs):
        descriptor = real_open(path, flags, mode, **kwargs)
        if flags & os.O_CREAT:
            observe(path, descriptor)
        return descriptor

    def open_handle(path, mode="r", *args, **kwargs):
        handle = real_io_open(path, mode, *args, **kwargs)
        if any(flag in mode for flag in "wax"):
            observe(path, handle.fileno())
        return handle

    with patch("os.open", side_effect=open_descriptor), patch("io.open", side_effect=open_handle):
        yield seen


@unittest.skipUnless(os.name == "posix", "POSIX permission bits")
class QaScriptCredentialTests(unittest.TestCase):
    def test_private_restore_environment_is_private_from_creation(self) -> None:
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=f"{umask:03o}"), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "runtime"
                target = root / ".env"
                previous = os.umask(umask)
                try:
                    with creation_modes(target) as modes:
                        restore._write_runtime_files(
                            ROOT, root, "synthetic-image", (18080, 18081, 18082),
                            "operator", "synthetic-restore-password", live_read_only=False,
                        )
                finally:
                    os.umask(previous)
                text = target.read_text(encoding="utf-8")
                self.assertIn("READ_UI_AUTH_PASSWORD=synthetic-restore-password\n", text)
                self.assertIn("ADMIN_AUTH_PASSWORD=synthetic-restore-password\n", text)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(modes, [0o600], "credential mode immediately after creation")

    def test_compose_environment_is_private_from_creation(self) -> None:
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=f"{umask:03o}"), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                target = root / ".env"
                previous = os.umask(umask)
                try:
                    with creation_modes(target) as modes:
                        matrix._write_environment(root, "synthetic-image", matrix.Ports(18080, 18081, 18082))
                finally:
                    os.umask(previous)
                data = target.read_bytes()
                self.assertIn(f"READ_UI_AUTH_PASSWORD={matrix.AUTH_PASSWORD}\n".encode(), data)
                self.assertIn(f"ADMIN_AUTH_PASSWORD={matrix.AUTH_PASSWORD}\n".encode(), data)
                self.assertTrue(data.endswith(b"\n"))
                self.assertNotIn(b"\r", data)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertEqual(modes, [0o600], "credential mode immediately after creation")

    def test_compose_backup_passphrase_is_private_from_creation(self) -> None:
        variant = next(variant for variant in matrix.VARIANTS if variant.scheduler_policy_enabled)
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=f"{umask:03o}"), tempfile.TemporaryDirectory() as raw:
                root = Path(raw) / "runtime"
                target = root / ".synthetic-backup-passphrase"
                installed: list[bytes] = []

                def record_install(command, **kwargs):
                    if str(target) in command:
                        self.assertEqual(command[command.index("-m") + 1], "0600")
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                        installed.append(target.read_bytes())
                    return ""

                previous = os.umask(umask)
                try:
                    with creation_modes(target) as modes, patch.object(matrix, "_run", side_effect=record_install):
                        matrix._prepare_variant_root(
                            root, variant=variant, compose_path=ROOT / "docker-compose.yml",
                            config_fixture=ROOT / "tests/fixtures/ci-smoke-config.yaml",
                            image="synthetic-image", ports=matrix.Ports(18080, 18081, 18082),
                        )
                finally:
                    os.umask(previous)
                self.assertEqual(installed, [matrix.SYNTHETIC_BACKUP_PASSPHRASE.encode("utf-8")])
                self.assertFalse(target.exists(), "the install source is still removed")
                self.assertEqual(modes, [0o600], "credential mode immediately after creation")

    def test_image_upgrade_backup_passphrase_stays_private_from_creation(self) -> None:
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=f"{umask:03o}"), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                target = root / "backup-passphrase.tmp"
                installed: list[bytes] = []
                smoke._record_created_root(root)
                self.addCleanup(smoke._OWNED_ROOTS.pop, root)

                def record_install(_root, command, **kwargs):
                    if str(target) in command:
                        self.assertEqual(command[command.index("-m") + 1], "0600")
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                        installed.append(target.read_bytes())
                    return ""

                previous = os.umask(umask)
                try:
                    with creation_modes(target) as modes, patch.object(smoke, "owned_run", side_effect=record_install):
                        smoke.install_backup_secrets(root)
                finally:
                    os.umask(previous)
                self.assertEqual(len(installed), 1)
                self.assertTrue(installed[0].endswith(b"\n"))
                self.assertFalse(target.exists(), "the install source is still removed")
                self.assertEqual(modes, [0o600], "credential mode immediately after creation")
