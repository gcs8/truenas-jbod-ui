from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from app.models.domain import SystemSetupRequest
from app.services.system_setup import SystemSetupService


class SystemSetupApiDialectTests(unittest.TestCase):
    """The admin clone flow must not silently move a JSON-RPC host onto DDP."""

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

    def _request(self, *, system_id: str, host: str) -> SystemSetupRequest:
        return SystemSetupRequest(
            system_id=system_id,
            label="Cloned Scale",
            platform="scale",
            truenas_host=host,
            api_key="cloned-api-key",
            replace_existing=False,
        )

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


if __name__ == "__main__":
    unittest.main()
