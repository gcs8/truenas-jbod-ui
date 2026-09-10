from __future__ import annotations

import hashlib
import json
import os
import re
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


class HistoryEnvDocumentationDriftTests(unittest.TestCase):
    """Wiki and `.env.example` must match what history_service/config.py enforces."""

    # Keys the history settings model reads that `docker-compose.yml` never
    # passes to the `enclosure-history` service, so a value set in `.env` has
    # no effect on the sidecar. Wiring one of these in Compose is a fix, not a
    # reason to extend this set (#TBD).
    UNWIRED_SIDECAR_KEYS = frozenset(
        {
            "RELEASE_CHECK_ENABLED",
            "RELEASE_CHECK_REPO",
            "RELEASE_CHECK_INTERVAL_SECONDS",
            "RELEASE_CHECK_TIMEOUT_SECONDS",
        }
    )
    LOOPBACK_VALUES = ("127.0.0.1", "::1", "localhost")

    def env_example(self) -> str:
        return (ROOT / ".env.example").read_text(encoding="utf-8")

    def documented_keys(self) -> set[str]:
        text = self.env_example()
        return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))

    def history_environment_block(self) -> str:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        return compose.split("  enclosure-history:", 1)[1].split("\n  enclosure-admin:", 1)[0]

    def test_example_marks_every_history_key_compose_does_not_pass_to_the_sidecar(self) -> None:
        from history_service.config import ENV_OVERRIDES

        block = self.history_environment_block()
        documented = self.documented_keys()
        # A key the service block sets from another variable (the published
        # bind address is derived from HISTORY_BIND_ADDRESS) still reaches the
        # sidecar; only a key the block never mentions is unwired.
        unwired = {
            key
            for key in ENV_OVERRIDES
            if key in documented and key + ":" not in block
        }

        self.assertEqual(unwired, set(self.UNWIRED_SIDECAR_KEYS))
        example = self.env_example()
        self.assertIn("does not reach the history sidecar", example)
        for key in sorted(self.UNWIRED_SIDECAR_KEYS):
            self.assertIn(key, example, key)

    def test_example_says_compose_derives_the_published_bind_address(self) -> None:
        example = self.env_example()
        block = self.history_environment_block()

        self.assertIn("HISTORY_PUBLISHED_BIND_ADDRESS: ${HISTORY_BIND_ADDRESS", block)
        marker = example.index("HISTORY_PUBLISHED_BIND_ADDRESS=")
        preamble = example[:marker].rsplit("HISTORY_BIND_ADDRESS=", 1)[-1]
        self.assertIn("derived by Compose", preamble)

    def test_no_public_document_offers_a_nonloopback_bind_without_the_required_keys(self) -> None:
        required = (
            "HISTORY_REFRESH_AUTH_MODE=token",
            "HISTORY_REFRESH_TOKEN",
            "HISTORY_PUBLIC_ORIGIN=",
        )
        documents = [ROOT / "README.md", *sorted((ROOT / "wiki").glob("*.md"))]
        offenders: list[str] = []
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for match in re.finditer(r"HISTORY_BIND_ADDRESS=(?P<value>[^\s`]+)", text):
                value = match.group("value").strip("`.,")
                if value in self.LOOPBACK_VALUES:
                    continue
                window = text[max(0, match.start() - 900) : match.end() + 900]
                if not all(key in window for key in required):
                    offenders.append(f"{document.name}: {match.group(0)}")
        self.assertEqual(offenders, [], offenders)

    def test_settings_model_still_enforces_what_the_documents_promise(self) -> None:
        source = (ROOT / "history_service" / "config.py").read_text(encoding="utf-8")

        self.assertIn('"HISTORY_BIND_ADDRESS is not loopback. Set HISTORY_REFRESH_AUTH_MODE=token, "', source)
        self.assertIn('"HISTORY_REFRESH_AUTH_MODE=token needs a token. Set HISTORY_REFRESH_TOKEN "', source)
        self.assertIn('"HISTORY_BIND_ADDRESS is not loopback, so HISTORY_PUBLIC_ORIGIN must be set to the "', source)


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
