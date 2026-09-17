from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from app.models.domain import SystemSetupRequest
from app.services.system_setup import SystemSetupService


class _DialectConfigMixin:
    """Shared temp-config helpers for the dialect carry-over suites."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "systems": [
                        {
                            "id": "saved-scale",
                            "label": "Saved Scale",
                            "truenas": {
                                "host": "https://nas.example.test",
                                "platform": "scale",
                                "api_dialect": "jsonrpc",
                                "api_version": "v25.10.0",
                            },
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.service = SystemSetupService(str(self.config_path))

    def _saved_truenas(self, system_id: str) -> dict:
        payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        system = next(item for item in payload["systems"] if item["id"] == system_id)
        return dict(system["truenas"])

    def _write_systems(self, systems: list[dict]) -> None:
        self.config_path.write_text(
            yaml.safe_dump({"systems": systems}, sort_keys=False),
            encoding="utf-8",
        )
        self.service = SystemSetupService(str(self.config_path))

    @staticmethod
    def _saved_entry(
        system_id: str,
        *,
        host: str = "https://nas.example.test",
        dialect: str,
        version: str,
    ) -> dict:
        return {
            "id": system_id,
            "label": system_id,
            "truenas": {
                "host": host,
                "platform": "scale",
                "api_dialect": dialect,
                "api_version": version,
            },
            "ssh": {"commands": [f"echo {system_id}"]},
        }

    def _request(
        self,
        *,
        system_id: str,
        host: str,
        source_system_id: str | None = None,
    ) -> SystemSetupRequest:
        extra: dict[str, object] = {}
        if source_system_id is not None:
            extra = {
                "ssh_commands_action": "preserve",
                "ssh_commands_source_system_id": source_system_id,
            }
        return SystemSetupRequest(
            system_id=system_id,
            label="Cloned Scale",
            platform="scale",
            truenas_host=host,
            api_key="cloned-api-key",
            replace_existing=False,
            **extra,
        )


class SystemSetupApiDialectTests(_DialectConfigMixin, unittest.TestCase):
    """The admin clone flow must not silently move a JSON-RPC host onto DDP."""

    def test_cloning_a_saved_system_keeps_the_jsonrpc_dialect(self) -> None:
        self.service.save_system(self._request(system_id="scale-clone", host="https://nas.example.test"))

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "jsonrpc")
        self.assertEqual(saved.get("api_version"), "v25.10.0")

    def test_cloning_matches_the_host_regardless_of_url_spelling(self) -> None:
        self.service.save_system(self._request(system_id="scale-clone", host="https://NAS.example.test/"))

        self.assertEqual(self._saved_truenas("scale-clone").get("api_dialect"), "jsonrpc")

    def test_a_new_unrelated_host_keeps_the_ddp_default(self) -> None:
        self.service.save_system(self._request(system_id="other-scale", host="https://other.example.test"))

        saved = self._saved_truenas("other-scale")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")


class SystemSetupMixedDialectEndpointTests(_DialectConfigMixin, unittest.TestCase):
    """Two systems on one endpoint must not decide each other's dialect."""

    def test_clone_takes_the_dialect_of_the_system_it_was_cloned_from(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                host="https://nas.example.test",
                source_system_id="saved-jsonrpc",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "jsonrpc")
        self.assertEqual(saved.get("api_version"), "v25.10.0")

    def test_clone_of_the_ddp_entry_does_not_inherit_the_jsonrpc_neighbour(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                host="https://nas.example.test",
                source_system_id="saved-ddp",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_mixed_dialect_endpoint_without_a_source_fails_closed(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(system_id="scale-clone", host="https://nas.example.test")
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")

    def test_agreeing_entries_on_one_endpoint_still_carry_the_dialect(self) -> None:
        self._write_systems(
            [
                self._saved_entry("saved-jsonrpc", dialect="jsonrpc", version="v25.10.0"),
                self._saved_entry("saved-jsonrpc-2", dialect="jsonrpc", version="v25.10.0"),
            ]
        )

        self.service.save_system(
            self._request(system_id="scale-clone", host="https://nas.example.test")
        )

        self.assertEqual(self._saved_truenas("scale-clone").get("api_dialect"), "jsonrpc")

    def test_a_source_system_on_another_endpoint_does_not_set_the_dialect(self) -> None:
        self._write_systems(
            [
                self._saved_entry(
                    "saved-elsewhere",
                    host="https://other.example.test",
                    dialect="jsonrpc",
                    version="v25.10.0",
                ),
                self._saved_entry("saved-ddp", dialect="ddp", version="current"),
            ]
        )

        self.service.save_system(
            self._request(
                system_id="scale-clone",
                host="https://nas.example.test",
                source_system_id="saved-elsewhere",
            )
        )

        saved = self._saved_truenas("scale-clone")
        self.assertEqual(saved.get("api_dialect"), "ddp")
        self.assertEqual(saved.get("api_version"), "current")


if __name__ == "__main__":
    unittest.main()
