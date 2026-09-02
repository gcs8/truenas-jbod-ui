from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from app.models.domain import SystemSetupRequest
from app.services.system_setup import SystemSetupService


MARKER_COMMAND = "/usr/local/bin/check-state --value marker-command"


class AdminCommandStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self._write_config()
        self.service = SystemSetupService(str(self.config_path))

    def _write_config(self) -> None:
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "systems": [
                        {
                            "id": "source-system",
                            "label": "Source System",
                            "truenas": {
                                "host": "https://source.example.test",
                                "platform": "core",
                            },
                            "ssh": {
                                "enabled": True,
                                "host": "source.example.test",
                                "user": "operator",
                                "commands": [MARKER_COMMAND],
                            },
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _request(
        self,
        *,
        system_id: str,
        action: str,
        source_system_id: str | None = None,
        commands: list[str] | None = None,
        replace_existing: bool,
    ) -> SystemSetupRequest:
        return SystemSetupRequest(
            system_id=system_id,
            label="Updated System",
            platform="core",
            truenas_host="https://updated.example.test",
            ssh_enabled=True,
            ssh_host="updated.example.test",
            ssh_user="operator",
            ssh_commands=commands or [],
            ssh_commands_action=action,
            ssh_commands_source_system_id=source_system_id,
            replace_existing=replace_existing,
        )

    def _saved_commands(self, system_id: str) -> list[str]:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        system = next(item for item in payload["systems"] if item["id"] == system_id)
        return list(system["ssh"]["commands"])

    def test_preserve_action_keeps_commands_when_updating_source_system(self) -> None:
        self.service.save_system(
            self._request(
                system_id="source-system",
                action="preserve",
                source_system_id="source-system",
                replace_existing=True,
            )
        )

        self.assertEqual(self._saved_commands("source-system"), [MARKER_COMMAND])

    def test_preserve_action_copies_commands_to_a_new_system_id(self) -> None:
        self.service.save_system(
            self._request(
                system_id="copied-system",
                action="preserve",
                source_system_id="source-system",
                replace_existing=False,
            )
        )

        self.assertEqual(self._saved_commands("copied-system"), [MARKER_COMMAND])

    def test_replace_action_can_explicitly_clear_commands(self) -> None:
        self.service.save_system(
            self._request(
                system_id="source-system",
                action="replace",
                commands=[],
                replace_existing=True,
            )
        )

        self.assertEqual(self._saved_commands("source-system"), [])

    def test_preserve_action_rejects_an_unknown_source_system(self) -> None:
        with self.assertRaisesRegex(ValueError, "saved SSH command list is unavailable"):
            self.service.save_system(
                self._request(
                    system_id="copied-system",
                    action="preserve",
                    source_system_id="missing-system",
                    replace_existing=False,
                )
            )

    def test_preserve_action_rejects_replacement_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot include replacement commands"):
            self.service.save_system(
                self._request(
                    system_id="source-system",
                    action="preserve",
                    source_system_id="source-system",
                    commands=["/usr/local/bin/replacement --read-only"],
                    replace_existing=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
