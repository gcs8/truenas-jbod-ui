from __future__ import annotations

import asyncio
import hashlib
import re
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config_errors import ConfigurationError
from history_service.config import get_history_settings

import yaml


ROOT = Path(__file__).resolve().parents[1]


class HistoryOperationConfigContractTests(unittest.TestCase):
    def test_compose_preserves_loopback_and_routes_refresh_policy_to_both_services(self) -> None:
        for filename in ("docker-compose.yml", "docker-compose.dev.yml"):
            document = yaml.safe_load((ROOT / filename).read_text(encoding="utf-8"))
            services = document["services"]
            history = services["enclosure-history"]
            ui = services["enclosure-ui"]
            self.assertIn("${HISTORY_BIND_ADDRESS:-127.0.0.1}", history["ports"][0])
            for key in (
                "HISTORY_PUBLISHED_BIND_ADDRESS",
                "HISTORY_PUBLIC_ORIGIN",
                "HISTORY_REFRESH_AUTH_MODE",
                "HISTORY_FULL_REFRESH_COOLDOWN_SECONDS",
                "HISTORY_REFRESH_TOKEN",
            ):
                self.assertIn(key, history["environment"], (filename, key))
            self.assertIn("HISTORY_REFRESH_TOKEN", ui["environment"])

    def test_secret_overlay_mounts_one_read_only_refresh_token_into_ui_and_history(self) -> None:
        document = yaml.safe_load((ROOT / "docker-compose.secrets.yml").read_text(encoding="utf-8"))
        self.assertEqual(document["secrets"]["history_refresh_token"]["file"], "./secrets/history_refresh_token")
        for service_name in ("enclosure-ui", "enclosure-history"):
            service = document["services"][service_name]
            self.assertEqual(
                service["environment"]["HISTORY_REFRESH_TOKEN_FILE"],
                "/run/secrets/history_refresh_token",
            )
            self.assertIn("history_refresh_token", service["secrets"])

    def test_public_example_documents_network_and_token_modes_without_a_token_value(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in (
            "HISTORY_PUBLISHED_BIND_ADDRESS",
            "HISTORY_PUBLIC_ORIGIN",
            "HISTORY_REFRESH_AUTH_MODE",
            "HISTORY_FULL_REFRESH_COOLDOWN_SECONDS",
            "HISTORY_REFRESH_TOKEN_FILE",
        ):
            self.assertIn(f"{key}=", text)
        self.assertNotRegex(text, r"HISTORY_REFRESH_TOKEN=[^\n]+")


class HistoryComposeMigrationTests(unittest.TestCase):
    OVERLAY = ROOT / "docker-compose.history-bind.yml"
    LEGACY = ROOT / "tests/fixtures/compose/v0.22.2.yml"

    def test_migration_only_forwards_the_port_interpolation_address(self) -> None:
        self.assertTrue(self.OVERLAY.is_file(), "explicit history bind migration is missing")
        self.assertEqual(yaml.safe_load(self.OVERLAY.read_text(encoding="utf-8")), {
            "services": {"enclosure-history": {"environment": {
                "HISTORY_PUBLISHED_BIND_ADDRESS": "${HISTORY_BIND_ADDRESS:-127.0.0.1}",
            }}},
        })

    def test_legacy_fixture_is_the_exact_published_compose(self) -> None:
        self.assertEqual(hashlib.sha256(self.LEGACY.read_bytes()).hexdigest(),
                         "e2f1c95635b0a53506cbe4c2c7c2c2c9bded0d215209e7a9dd09716f3b753b3c")
        history = yaml.safe_load(self.LEGACY.read_text(encoding="utf-8"))["services"]["enclosure-history"]
        self.assertNotIn("env_file", history)
        self.assertNotIn("HISTORY_BIND_ADDRESS", history["environment"])
        self.assertNotIn("HISTORY_PUBLISHED_BIND_ADDRESS", history["environment"])

    def test_documented_inline_policy_forwards_one_source_to_both_services(self) -> None:
        document = (ROOT / "docs/HISTORY_COMPOSE_MIGRATION.md").read_text()
        blocks = re.findall(r"```yaml\n(.*?)```", document, re.S)
        policy = next(yaml.safe_load(block) for block in blocks if "HISTORY_REFRESH_AUTH_MODE" in block)
        services = policy["services"]
        expression = "${HISTORY_REFRESH_TOKEN:?Set a nonempty history refresh token}"
        for name in ("enclosure-ui", "enclosure-history"):
            self.assertEqual(services.get(name, {}).get("environment", {}).get("HISTORY_REFRESH_TOKEN"), expression)

    def test_real_documented_token_policy_uses_effective_loader_credentials(self) -> None:
        from app.config import get_settings
        from app.services.history_backend import HistoryBackendClient

        binary = os.environ.get("COMPOSE_BINARY") or shutil.which("docker-compose")
        if not binary:
            self.skipTest("Standalone Compose unavailable; token migration not validated")
        document = (ROOT / "docs/HISTORY_COMPOSE_MIGRATION.md").read_text()
        policy = next(block for block in re.findall(r"```yaml\n(.*?)```", document, re.S)
                      if "HISTORY_REFRESH_AUTH_MODE" in block)
        # Separate literal service .env, ordered CLI files, and shell sources.
        # Values are invented; assertions never display rendered credentials.
        cases = (
            ("default", (), {}, "literal", False),
            ("cli", ("first.env",), {}, "first", False),
            ("ordered-cli", ("first.env", "last.env"), {}, "last", False),
            ("reverse-cli", ("last.env", "first.env"), {}, "first", False),
            ("file-backed", ("first.env", "last.env"), {}, "last", False),
            ("shell", ("first.env", "last.env"), {
                "HISTORY_REFRESH_TOKEN": "synthetic-shell",
                "HISTORY_PUBLIC_ORIGIN": "https://shell.example.test"}, "shell", False),
            ("empty-shell", ("first.env",), {"HISTORY_REFRESH_TOKEN": ""}, None, True),
            ("missing", ("missing.env",), {}, None, True),
            ("empty", ("empty.env",), {}, None, True),
            ("missing-origin", ("no-origin.env",), {}, None, True),
        )
        with tempfile.TemporaryDirectory(prefix="history-token-migration-") as temporary:
            root = Path(temporary)
            shutil.copyfile(self.LEGACY, root / "compose.yaml")
            shutil.copyfile(self.OVERLAY, root / self.OVERLAY.name)
            (root / "auth.yml").write_text(policy)
            file_policy = yaml.safe_load(policy)
            # Apply the documented file-backed variant, retaining scoped mounts.
            secret = root / "synthetic-secret"
            secret.write_text("synthetic-file-backed\n")
            secret.chmod(0o600)
            for name in ("enclosure-ui", "enclosure-history"):
                service = file_policy["services"].setdefault(name, {"environment": {}})
                service["environment"].pop("HISTORY_REFRESH_TOKEN", None)
                service["environment"]["HISTORY_REFRESH_TOKEN_FILE"] = "/run/secrets/history_refresh_token"
                service["secrets"] = ["history_refresh_token"]
            file_policy["secrets"] = {"history_refresh_token": {"file": str(secret)}}
            (root / "file-auth.yml").write_text(yaml.safe_dump(file_policy))
            for name, source in ((".env", "literal"), ("first.env", "first"), ("last.env", "last")):
                (root / name).write_text(
                    f"HISTORY_REFRESH_TOKEN=synthetic-{source}\n"
                    f"HISTORY_PUBLIC_ORIGIN=https://{source}.example.test\n"
                    "APP_PUBLIC_ORIGIN=https://ui.example.test\n"
                    "HISTORY_BIND_ADDRESS=192.0.2.20\n")
            (root / "missing.env").write_text("HISTORY_PUBLIC_ORIGIN=https://missing.example.test\n")
            (root / "empty.env").write_text("HISTORY_REFRESH_TOKEN=\nHISTORY_PUBLIC_ORIGIN=https://empty.example.test\n")
            (root / "no-origin.env").write_text("HISTORY_REFRESH_TOKEN=synthetic-no-origin\n")
            (root / "site.yml").write_text(yaml.safe_dump({"services": {
                "enclosure-ui": {"user": "21001:21002", "labels": {"fixture.order": "site"}},
                "enclosure-history": {"user": "21001:21002", "labels": {"fixture.order": "site"}},
                "enclosure-backup": {"group_add": ["21002"]},
            }}))
            (root / "last.yml").write_text(yaml.safe_dump({"services": {
                "enclosure-history": {"labels": {"fixture.order": "last"}, "mem_limit": "512m"},
            }}))
            clean = {"PATH": os.environ.get("PATH", ""), "HOME": temporary,
                     "TMPDIR": temporary, "DOCKER_CONFIG": str(root / "docker-config"),
                     "DOCKER_HOST": "tcp://127.0.0.1:1"}

            def render(files, shell, chain):
                command = [binary, "--project-name", "token-migration"]
                for name in files:
                    command.extend(["--env-file", str(root / name)])
                for name in chain:
                    command.extend(["-f", str(root / name)])
                result = subprocess.run(command + ["--profile", "history", "--profile", "backup",
                                                   "config", "--format", "json"],
                                        cwd=root, env={**clean, **shell}, capture_output=True,
                                        text=True, timeout=30)
                return result.returncode, json.loads(result.stdout) if result.returncode == 0 else None

            for label, files, shell, expected, rejected in cases:
                for custom in ((), ("site.yml", "last.yml"), ("last.yml", "site.yml")):
                    with self.subTest(case=label, chain=custom):
                        file_backed = label == "file-backed"
                        chain = ("compose.yaml", *custom, "file-auth.yml" if file_backed else "auth.yml")
                        status, before = render(files, shell, chain)
                        if rejected:
                            self.assertNotEqual(status, 0, "missing/empty policy input must refuse Compose rendering")
                            continue
                        self.assertEqual(status, 0, "synthetic policy rendering failed")
                        status, after = render(files, shell, (*chain, self.OVERLAY.name))
                        self.assertEqual(status, 0, "synthetic migration rendering failed")
                        ui_env = dict(after["services"]["enclosure-ui"]["environment"])
                        history_env = dict(after["services"]["enclosure-history"]["environment"])
                        if file_backed:
                            # Model container secret mounts using only the synthetic source.
                            for name, env in (("enclosure-ui", ui_env), ("enclosure-history", history_env)):
                                mounts = after["services"][name]["secrets"]
                                self.assertEqual(mounts[0]["source"], "history_refresh_token")
                                self.assertEqual(env["HISTORY_REFRESH_TOKEN_FILE"], "/run/secrets/history_refresh_token")
                                env["HISTORY_REFRESH_TOKEN_FILE"] = after["secrets"][mounts[0]["source"]]["file"]
                        ui_env["APP_CONFIG_PATH"] = str(root / "ui/config/config.yaml")
                        history_env.update({"HISTORY_SQLITE_PATH": str(root / "history/history.db"),
                                            "HISTORY_BACKUP_DIR": str(root / "backups"),
                                            "HISTORY_LONG_TERM_BACKUP_DIR": str(root / "long-term")})
                        get_settings.cache_clear()
                        get_history_settings.cache_clear()
                        try:
                            with patch.dict(os.environ, ui_env, clear=True):
                                ui = get_settings()
                            with patch.dict(os.environ, history_env, clear=True):
                                history = get_history_settings()
                            self.assertIsNotNone(ui.history.refresh_token, "UI credential absent")
                            self.assertIsNotNone(history.refresh_token, "history credential absent")
                            ui_token = ui.history.refresh_token.get_secret_value()
                            history_token = history.refresh_token.get_secret_value()
                            self.assertTrue(ui_token == history_token, "effective UI/history credentials differ")
                            expected_token = "synthetic-file-backed" if file_backed else f"synthetic-{expected}"
                            self.assertTrue(history_token == expected_token, "wrong credential source")
                            self.assertEqual(ui.app.public_origin, "https://ui.example.test")
                            self.assertEqual(history.public_origin, f"https://{expected}.example.test")
                            self.assertEqual(history.refresh_auth_mode, "token")
                            client = HistoryBackendClient(ui.history)

                            def request(path, *, method, body, headers):
                                self.assertEqual(path, "/api/history/refresh")
                                self.assertEqual(method, "POST")
                                self.assertNotIn("Origin", headers)
                                self.assertTrue(headers.get("Authorization") == f"Bearer {history_token}",
                                                "refresh transport credential rejected")
                                return b'{"accepted":true}', {}

                            with patch.object(client, "_request_bytes_sync", side_effect=request):
                                self.assertTrue(asyncio.run(client.refresh("fast"))["accepted"])
                            if file_backed:
                                # Equal inline values cannot mask a divergent file-backed UI.
                                other = root / "other-synthetic-secret"
                                other.write_text("synthetic-other-file\n")
                                other.chmod(0o600)
                                get_settings.cache_clear()
                                with patch.dict(os.environ, {**ui_env,
                                        "HISTORY_REFRESH_TOKEN": history_token,
                                        "HISTORY_REFRESH_TOKEN_FILE": str(other)}, clear=True):
                                    divergent = get_settings().history.refresh_token
                                self.assertTrue(divergent.get_secret_value() != history_token,
                                                "file priority must not be hidden by inline equality")
                                for loader, env in ((get_settings, ui_env), (get_history_settings, history_env)):
                                    loader.cache_clear()
                                    with patch.dict(os.environ, {**env, "HISTORY_REFRESH_TOKEN_FILE": ""}, clear=True):
                                        with self.assertRaises(ValueError):
                                            loader()
                        finally:
                            get_settings.cache_clear()
                            get_history_settings.cache_clear()
                        del after["services"]["enclosure-history"]["environment"]["HISTORY_PUBLISHED_BIND_ADDRESS"]
                        self.assertTrue(after == before, "address migration altered customizations")

    def test_real_legacy_compose_migration_preserves_customizations_and_guard(self) -> None:
        binary = os.environ.get("COMPOSE_BINARY") or shutil.which("docker-compose")
        if binary:
            compose = [binary]
        elif shutil.which("docker"):
            compose = ["docker", "compose"]
            if subprocess.run(compose + ["version"], capture_output=True, timeout=30).returncode:
                self.skipTest("Compose plugin unavailable; daemon-free migration not validated")
        else:
            self.skipTest("Compose unavailable; daemon-free migration not validated")

        # All project bytes are public/synthetic. Do not read operator env/config.
        cases = (
            ("absent", "", "127.0.0.1", "network", False),
            ("blank", "HISTORY_BIND_ADDRESS=\n", "127.0.0.1", "network", False),
            ("loopback", "HISTORY_BIND_ADDRESS=127.0.0.2\n", "127.0.0.2", "network", False),
            ("exposed", "HISTORY_BIND_ADDRESS=0.0.0.0\n", "0.0.0.0", "network", True),
            ("no-token", "HISTORY_BIND_ADDRESS=0.0.0.0\nHISTORY_REFRESH_AUTH_MODE=token\n"
             "HISTORY_PUBLIC_ORIGIN=https://history.example.test\n", "0.0.0.0", "token", True),
            ("no-origin", "HISTORY_BIND_ADDRESS=0.0.0.0\nHISTORY_REFRESH_AUTH_MODE=token\n"
             "HISTORY_REFRESH_TOKEN=synthetic-test-token\n", "0.0.0.0", "token", True),
            ("token", "HISTORY_BIND_ADDRESS=192.0.2.20\nHISTORY_REFRESH_AUTH_MODE=token\n"
             "HISTORY_REFRESH_TOKEN=synthetic-test-token\n"
             "HISTORY_PUBLIC_ORIGIN=https://history.example.test\n", "192.0.2.20", "token", False),
            ("no-masking", "HISTORY_BIND_ADDRESS=0.0.0.0\nHISTORY_PUBLISHED_BIND_ADDRESS=127.0.0.1\n",
             "0.0.0.0", "network", True),
        )
        with tempfile.TemporaryDirectory(prefix="history-compose-migration-") as temporary:
            root = Path(temporary)
            shutil.copyfile(self.LEGACY, root / "compose.yaml")
            if self.OVERLAY.is_file():
                shutil.copyfile(self.OVERLAY, root / self.OVERLAY.name)
            else:
                # Missing migration is a no-op for RED against the actual old merger.
                (root / self.OVERLAY.name).write_text("services: {}\n", encoding="utf-8")
            # Auth is an existing explicit operator customization, not a migration default.
            (root / "site.yml").write_text(yaml.safe_dump({"services": {
                "enclosure-history": {"user": "${APP_UID:-10001}:${APP_GID:-10001}",
                    "labels": {"fixture.order": "site"},
                    "environment": {
                        "SITE_CUSTOM": "preserved",
                        "HISTORY_REFRESH_AUTH_MODE": "${HISTORY_REFRESH_AUTH_MODE:-network}",
                        "HISTORY_REFRESH_TOKEN": "${HISTORY_REFRESH_TOKEN:-}",
                        "HISTORY_PUBLIC_ORIGIN": "${HISTORY_PUBLIC_ORIGIN:-}",
                    }},
                "enclosure-backup": {"group_add": ["${APP_GID:-10001}"]},
            }}), encoding="utf-8")
            (root / "last.yml").write_text(yaml.safe_dump({"services": {
                "enclosure-history": {"labels": {"fixture.order": "last"}, "mem_limit": "512m"},
            }}), encoding="utf-8")
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": temporary,
                           "TMPDIR": temporary, "DOCKER_CONFIG": str(root / "docker-config"),
                           "DOCKER_HOST": "tcp://127.0.0.1:1"}

            def render(chain: tuple[str, ...]) -> dict:
                command = compose + ["--project-name", "migration", "--env-file", str(root / ".env")]
                for name in chain:
                    command.extend(["-f", str(root / name)])
                command.extend(["--profile", "history", "--profile", "backup", "config", "--format", "json"])
                result = subprocess.run(command, cwd=root, env=environment, text=True,
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            for label, values, address, mode, rejected in cases:
                for chain in (("compose.yaml",), ("compose.yaml", "site.yml", "last.yml"),
                              ("compose.yaml", "last.yml", "site.yml")):
                    with self.subTest(case=label, chain=chain):
                        custom = len(chain) > 1
                        identities = "APP_UID=21001\nAPP_GID=21002\nBACKUP_UID=22001\nBACKUP_GID=22002\n" if custom else ""
                        (root / ".env").write_text(values + identities, encoding="utf-8")
                        before = render(chain)
                        after = render((*chain, self.OVERLAY.name))
                        history = after["services"]["enclosure-history"]
                        history_env = history["environment"]
                        self.assertEqual(history["ports"][0]["host_ip"], address)
                        self.assertIn("HISTORY_PUBLISHED_BIND_ADDRESS", history_env)
                        self.assertEqual(history_env["HISTORY_PUBLISHED_BIND_ADDRESS"], address)
                        self.assertNotIn("HISTORY_BIND_ADDRESS", history_env)
                        self.assertEqual(history["profiles"], ["history"])
                        self.assertEqual(history["user"], "21001:21002" if custom else "0:0")
                        backup = after["services"]["enclosure-backup"]
                        self.assertEqual(backup["user"], "22001:22002" if custom else "1000:1000")
                        if custom:
                            self.assertEqual(backup["group_add"], ["21002"])
                            self.assertEqual(history["labels"]["fixture.order"], chain[-1].split(".")[0])
                        # Loader receives actual Compose env, except private filesystem sinks.
                        loader_env = {key: value for key, value in history_env.items() if value is not None}
                        loader_env.update({"HISTORY_SQLITE_PATH": str(root / "history/history.db"),
                                           "HISTORY_BACKUP_DIR": str(root / "backups"),
                                           "HISTORY_LONG_TERM_BACKUP_DIR": str(root / "long-term")})
                        get_history_settings.cache_clear()
                        try:
                            with patch.dict(os.environ, loader_env, clear=True):
                                if rejected or (mode == "token" and not custom):
                                    with self.assertRaises(ConfigurationError):
                                        get_history_settings()
                                else:
                                    settings = get_history_settings()
                                    self.assertEqual(settings.published_bind_address, address)
                                    self.assertEqual(settings.refresh_auth_mode, mode)
                        finally:
                            get_history_settings.cache_clear()
                        del history_env["HISTORY_PUBLISHED_BIND_ADDRESS"]
                        self.assertEqual(after, before, "migration changed more than publication metadata")


if __name__ == "__main__":
    unittest.main()
