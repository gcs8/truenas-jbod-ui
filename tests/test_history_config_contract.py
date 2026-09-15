from __future__ import annotations

import re
import unittest
from pathlib import Path

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
        unwired = {
            key
            for key in ENV_OVERRIDES
            if key in documented and "${" + key not in block
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

        self.assertIn("Non-loopback history exposure requires refresh token mode.", source)
        self.assertIn("History refresh token mode requires a non-empty token.", source)
        self.assertIn("Non-loopback history exposure requires a valid HISTORY_PUBLIC_ORIGIN.", source)


if __name__ == "__main__":
    unittest.main()
