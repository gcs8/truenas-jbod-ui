from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
