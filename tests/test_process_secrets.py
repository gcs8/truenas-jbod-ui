from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import (
    BMCConfig,
    SSHConfig,
    Settings,
    SystemConfig,
    TrueNASConfig,
    get_settings,
)


class ProcessSecretSettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_secret_bearing_models_hide_rejected_input_values(self) -> None:
        for model_type in (TrueNASConfig, SSHConfig, BMCConfig, SystemConfig, Settings):
            with self.subTest(model=model_type.__name__):
                self.assertTrue(model_type.model_config.get("hide_input_in_errors"))

    def test_public_docs_cover_file_secret_precedence_and_safe_fallback(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
        deployment_doc = (repo_root / "wiki" / "Docker-and-GHCR-Deployment.md").read_text(
            encoding="utf-8"
        )
        combined = f"{env_example}\n{deployment_doc}"
        for key in (
            "TRUENAS_API_KEY_FILE",
            "TRUENAS_API_PASSWORD_FILE",
            "SSH_PASSWORD_FILE",
            "SSH_SUDO_PASSWORD_FILE",
            "ADMIN_AUTH_PASSWORD_FILE",
        ):
            with self.subTest(key=key):
                self.assertIn(key, combined)
        self.assertIn("docker-compose.secrets.yml", combined)
        self.assertIn("takes precedence", combined)
        self.assertIn("umask 077", combined)
        self.assertIn("chmod 600", combined)
        self.assertIn("saved multi-system", combined)

    def test_direct_secret_environment_values_remain_exact_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config" / "config.yaml"
            environment = {
                "APP_CONFIG_PATH": str(config_path),
                "TRUENAS_API_KEY": "true",
                "TRUENAS_API_USER": "123",
                "TRUENAS_API_PASSWORD": "null",
                "SSH_PASSWORD": "false",
                "SSH_SUDO_PASSWORD": "00123",
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                try:
                    settings = get_settings()
                except Exception as exc:  # pragma: no cover - expected only before the fix
                    self.fail(f"secret environment values entered scalar parsing: {type(exc).__name__}")

        self.assertEqual(settings.truenas.api_key, "true")
        self.assertEqual(settings.truenas.api_user, "123")
        self.assertEqual(settings.truenas.api_password, "null")
        self.assertEqual(settings.ssh.password, "false")
        self.assertEqual(settings.ssh.sudo_password, "00123")

    def test_secret_file_values_override_direct_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "config" / "config.yaml"
            environment = {
                "APP_CONFIG_PATH": str(config_path),
                "TRUENAS_API_USER": "direct-api-user",
            }
            api_key_path = temp_path / "truenas-api-key"
            api_key_path.write_text("  api-key  \r\n", encoding="utf-8")
            api_password_path = temp_path / "truenas-api-password"
            api_password_path.write_text(" password with spaces \n", encoding="utf-8")
            ssh_password_path = temp_path / "ssh-password"
            ssh_password_path.write_text("ssh-pass\n", encoding="utf-8")
            sudo_password_path = temp_path / "ssh-sudo-password"
            sudo_password_path.write_text("sudo-pass", encoding="utf-8")
            for fixture_path in (api_key_path, api_password_path, ssh_password_path, sudo_password_path):
                fixture_path.chmod(0o600)
            environment.update(
                {
                    "TRUENAS_API_KEY": "direct-value-must-not-win",
                    "TRUENAS_API_KEY_FILE": str(api_key_path),
                    "TRUENAS_API_PASSWORD": "direct-value-must-not-win",
                    "TRUENAS_API_PASSWORD_FILE": str(api_password_path),
                    "SSH_PASSWORD": "direct-value-must-not-win",
                    "SSH_PASSWORD_FILE": str(ssh_password_path),
                    "SSH_SUDO_PASSWORD": "direct-value-must-not-win",
                    "SSH_SUDO_PASSWORD_FILE": str(sudo_password_path),
                }
            )

            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                settings = get_settings()

        self.assertEqual(settings.truenas.api_key, "  api-key  ")
        self.assertEqual(settings.truenas.api_user, "direct-api-user")
        self.assertEqual(settings.truenas.api_password, " password with spaces ")
        self.assertEqual(settings.ssh.password, "ssh-pass")
        self.assertEqual(settings.ssh.sudo_password, "sudo-pass")

    def test_blank_secret_file_variable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "APP_CONFIG_PATH": str(Path(temp_dir) / "config" / "config.yaml"),
                "TRUENAS_API_KEY": "direct-api-key",
                "TRUENAS_API_KEY_FILE": "",
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*regular file"):
                    get_settings()

    def test_secret_file_reference_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            secret_path = temp_path / "secret"
            secret_path.write_text("synthetic-secret\n", encoding="utf-8")
            secret_path.chmod(0o600)
            symlink_path = temp_path / "secret-link"
            symlink_path.symlink_to(secret_path)
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(symlink_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*regular file"):
                    get_settings()

    def test_secret_file_reference_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fifo_path = Path(temp_dir) / "secret-fifo"
            os.mkfifo(fifo_path, 0o600)
            script = """
import os
import sys
from app.secret_files import load_secret_environment_value

os.environ["TRUENAS_API_KEY_FILE"] = sys.argv[1]
try:
    load_secret_environment_value("TRUENAS_API_KEY")
except ValueError:
    raise SystemExit(0)
raise SystemExit(1)
"""
            try:
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(fifo_path)],
                    cwd=Path(__file__).resolve().parents[1],
                    check=False,
                    timeout=0.75,
                )
            except subprocess.TimeoutExpired:
                self.fail("secret-file validation blocked while opening a FIFO")
            self.assertEqual(completed.returncode, 0)

    def test_secret_file_reference_rejects_group_or_world_writable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            secret_path = temp_path / "writable-secret"
            secret_path.write_text("synthetic-secret\n", encoding="utf-8")
            secret_path.chmod(0o666)
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(secret_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*writable"):
                    get_settings()

    def test_secret_file_reference_rejects_oversized_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            secret_path = temp_path / "oversized-secret"
            secret_path.write_bytes(b"x" * 65_537)
            secret_path.chmod(0o600)
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(secret_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*65536 bytes"):
                    get_settings()

    def test_secret_file_reference_rejects_invalid_utf8_without_echoing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            secret_path = temp_path / "invalid-utf8-secret"
            secret_path.write_bytes(b"synthetic-marker-\xff")
            secret_path.chmod(0o600)
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(secret_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*UTF-8") as caught:
                    get_settings()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("synthetic-marker", repr(caught.exception))

    def test_secret_file_open_error_does_not_retain_path_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            missing_path = temp_path / "synthetic-private-path-marker"
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(missing_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*regular file") as caught:
                    get_settings()
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("synthetic-private-path-marker", repr(caught.exception))

    def test_secret_file_reference_rejects_nul_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            secret_path = temp_path / "nul-secret"
            secret_path.write_bytes(b"synthetic-before\x00synthetic-after")
            secret_path.chmod(0o600)
            environment = {
                "APP_CONFIG_PATH": str(temp_path / "config" / "config.yaml"),
                "TRUENAS_API_KEY_FILE": str(secret_path),
            }
            with patch.dict("os.environ", environment, clear=True):
                get_settings.cache_clear()
                with self.assertRaisesRegex(ValueError, "TRUENAS_API_KEY_FILE.*NUL"):
                    get_settings()


if __name__ == "__main__":
    unittest.main()
