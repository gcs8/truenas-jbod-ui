from __future__ import annotations

import unittest

from app.config import TrueNASConfig
from app.services.quantastor_api import QuantastorRESTClient, build_quantastor_api_base
from app.services.truenas_ws import TrueNASAPIError


class QuantastorClientTests(unittest.TestCase):
    def test_build_quantastor_api_base_normalizes_host(self) -> None:
        self.assertEqual(
            build_quantastor_api_base("quantastor.example.local"),
            "https://quantastor.example.local/qstorapi",
        )
        self.assertEqual(
            build_quantastor_api_base("https://quantastor.example.local"),
            "https://quantastor.example.local/qstorapi",
        )
        self.assertEqual(
            build_quantastor_api_base("https://quantastor.example.local/custom"),
            "https://quantastor.example.local/custom/qstorapi",
        )
        self.assertEqual(
            build_quantastor_api_base("https://quantastor.example.local/qstorapi"),
            "https://quantastor.example.local/qstorapi",
        )

    def test_ensure_list_handles_common_wrapper_shapes(self) -> None:
        self.assertEqual(
            QuantastorRESTClient._ensure_list({"result": [{"id": 1}], "status": "ok"}),
            [{"id": 1}],
        )
        self.assertEqual(
            QuantastorRESTClient._ensure_list({"items": [{"id": 2}], "count": 1}),
            [{"id": 2}],
        )
        self.assertEqual(
            QuantastorRESTClient._ensure_list({"a": {"id": 3}, "b": {"id": 4}}),
            [{"id": 3}, {"id": 4}],
        )
        self.assertEqual(
            QuantastorRESTClient._ensure_list({"id": 5, "name": "single"}),
            [{"id": 5, "name": "single"}],
        )

    def test_request_json_requires_quantastor_credentials(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="https://quantastor.example.local",
                platform="quantastor",
                api_user="",
                api_password="",
            )
        )

        with self.assertRaises(TrueNASAPIError):
            client._request_json("storageSystemEnum")

    def test_optional_error_payload_is_detected(self) -> None:
        self.assertTrue(QuantastorRESTClient._is_error_payload({"RestError": "No method"}))
        self.assertTrue(QuantastorRESTClient._is_error_payload({"status": "error", "message": "failed"}))
        self.assertFalse(QuantastorRESTClient._is_error_payload([{"id": 1}]))
