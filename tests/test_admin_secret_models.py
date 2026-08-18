from __future__ import annotations

import unittest

from app.models.domain import ESXiHostPrepInstallRequest
from app.models.domain import QuantastorNodeDiscoveryRequest
from app.models.domain import StorageViewRequest
from app.models.domain import SystemSetupRequest
from app.services import system_setup


class AdminSecretFlowModelTests(unittest.TestCase):
    def test_storage_view_request_accepts_an_explicitly_empty_label(self) -> None:
        storage_view = StorageViewRequest(
            id="manual-view",
            label="",
            kind="manual",
            template_id="manual-4",
        )

        self.assertEqual(storage_view.label, "")

    def test_secondary_request_models_retain_saved_system_identity(self) -> None:
        discovery = QuantastorNodeDiscoveryRequest(
            system_id="saved-quantastor",
            truenas_host="https://192.0.2.30",
            api_user="admin",
            api_password="sentinel",
        )
        host_prep = ESXiHostPrepInstallRequest(
            system_id="saved-esxi",
            host="192.0.2.25",
            user="root",
            password="sentinel",
            upload_token="package-token",
        )

        self.assertEqual(getattr(discovery, "system_id", None), "saved-quantastor")
        self.assertEqual(getattr(host_prep, "system_id", None), "saved-esxi")

    def test_system_setup_request_preserves_distinct_label_only_ha_nodes(self) -> None:
        payload = SystemSetupRequest(
            label="Quantastor HA",
            platform="quantastor",
            truenas_host="https://192.0.2.30",
            ha_enabled=True,
            ha_nodes=[
                {"label": "Node Alpha"},
                {"label": "Node Beta"},
            ],
        )

        self.assertEqual(
            [node.label for node in payload.ha_nodes],
            ["Node Alpha", "Node Beta"],
        )

    def test_preserved_secret_helper_resolves_only_from_saved_value(self) -> None:
        resolver = getattr(system_setup, "resolve_preserved_secret", None)
        self.assertTrue(callable(resolver), "canonical preserved-secret resolver is missing")

        self.assertEqual(
            resolver(system_setup.PRESERVE_SECRET_SENTINEL, "saved-secret"),
            "saved-secret",
        )
        with self.assertRaisesRegex(ValueError, "saved secret"):
            resolver(system_setup.PRESERVE_SECRET_SENTINEL, None)
        self.assertEqual(resolver("replacement", "saved-secret"), "replacement")


if __name__ == "__main__":
    unittest.main()
