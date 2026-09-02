from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.config import TrueNASConfig
from app.services.quantastor_api import QuantastorRESTClient
from app.services.truenas_ws import TrueNASAPIError


class QuantastorRESTClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_collects_endpoints_in_parallel(self) -> None:
        class TrackingClient(QuantastorRESTClient):
            def __init__(self) -> None:
                super().__init__(TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor"))
                self.active_calls = 0
                self.max_active_calls = 0
                self.lock = threading.Lock()

            def _track(self, result):
                with self.lock:
                    self.active_calls += 1
                    self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    time.sleep(0.02)
                    return result
                finally:
                    with self.lock:
                        self.active_calls -= 1

            def _fetch_required_list(self, endpoint: str):
                return self._track([{"endpoint": endpoint}])

            def _fetch_optional_list(self, endpoint: str):
                return self._track([{"endpoint": endpoint}])

        client = TrackingClient()

        payload = await client.fetch_all()

        self.assertGreater(client.max_active_calls, 1)
        self.assertEqual(payload.enclosures[0]["endpoint"], "storageSystemEnum")
        self.assertEqual(payload.disks[0]["endpoint"], "physicalDiskEnum")
        self.assertEqual(payload.pools[0]["endpoint"], "storagePoolEnum")
        self.assertEqual(payload.pool_devices[0]["endpoint"], "storagePoolDeviceEnum")
        self.assertEqual(payload.ha_groups[0]["endpoint"], "haGroupEnum")
        self.assertEqual(payload.hw_disks[0]["endpoint"], "hwDiskEnum")
        self.assertEqual(payload.hw_enclosures[0]["endpoint"], "hwEnclosureEnum")

    async def test_fetch_all_tolerates_an_appliance_with_no_storage_pools(self) -> None:
        responses = {
            "storageSystemEnum": [{"id": "sys-1", "name": "node-a"}],
            "physicalDiskEnum": [{"id": "disk-1", "serialNumber": "SERIAL-1"}],
            "storagePoolEnum": [],
            "storagePoolDeviceEnum": [],
            "haGroupEnum": [],
            "hwDiskEnum": [{"id": "hw-1"}],
            "hwEnclosureEnum": [{"id": "enc-1"}],
        }
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        with patch.object(
            QuantastorRESTClient,
            "_request_json",
            lambda self, endpoint, params=None: responses[endpoint],
        ):
            payload = await client.fetch_all()

        self.assertEqual(payload.pools, [])
        self.assertEqual([row["id"] for row in payload.disks], ["disk-1"])
        self.assertEqual([row["id"] for row in payload.systems], ["sys-1"])

    async def test_fetch_all_still_fails_when_required_disk_rows_are_missing(self) -> None:
        responses = {
            "storageSystemEnum": [{"id": "sys-1"}],
            "physicalDiskEnum": [],
            "storagePoolEnum": [],
            "storagePoolDeviceEnum": [],
            "haGroupEnum": [],
            "hwDiskEnum": [],
            "hwEnclosureEnum": [],
        }
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        with patch.object(
            QuantastorRESTClient,
            "_request_json",
            lambda self, endpoint, params=None: responses[endpoint],
        ):
            with self.assertRaisesRegex(TrueNASAPIError, "physicalDiskEnum returned no usable rows"):
                await client.fetch_all()

    def test_empty_pool_exemption_rejects_malformed_collection_payloads(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        malformed_payloads = (
            None,
            "malformed",
            [1, None],
            [{"id": "pool-1"}, 1],
            {"result": [1]},
            {"result": [{"id": "pool-1"}, 1]},
            {"result": [{"id": "pool-1"}], "items": [{"id": "pool-2"}]},
            {"result": [], "items": []},
            {"foo": "bar"},
            {"foo": []},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with patch.object(client, "_request_json", return_value=payload):
                    with self.assertRaisesRegex(TrueNASAPIError, "storagePoolEnum returned no usable rows"):
                        client._fetch_required_list("storagePoolEnum")

    def test_pool_rows_accept_supported_collection_shapes(self) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(api_user="admin", api_password="secret", platform="quantastor")
        )

        supported_payloads = [
            ([], []),
            ([{"id": "pool-1"}], [{"id": "pool-1"}]),
        ]
        for wrapper in ("result", "list", "items", "objects", "data"):
            supported_payloads.extend(
                [
                    ({wrapper: []}, []),
                    ({wrapper: [{"id": "pool-1"}]}, [{"id": "pool-1"}]),
                ]
            )

        for payload, expected in supported_payloads:
            with self.subTest(payload=payload):
                with patch.object(client, "_request_json", return_value=payload):
                    self.assertEqual(client._fetch_required_list("storagePoolEnum"), expected)

    @patch("app.services.quantastor_api.build_tls_client_context")
    @patch("app.services.quantastor_api.urlopen_with_tls_config")
    def test_request_json_passes_tls_server_name_override(
        self,
        urlopen_with_tls_config_mock: MagicMock,
        build_tls_client_context_mock: MagicMock,
    ) -> None:
        client = QuantastorRESTClient(
            TrueNASConfig(
                host="https://10.13.37.10",
                api_user="admin",
                api_password="secret",
                platform="quantastor",
                tls_server_name="TrueNAS.gcs8.io",
            )
        )

        ssl_context = MagicMock()
        build_tls_client_context_mock.return_value = ssl_context

        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"[]"
        urlopen_with_tls_config_mock.return_value = response

        payload = client._request_json("storageSystemEnum", {"flags": 0})

        self.assertEqual(payload, [])
        self.assertEqual(urlopen_with_tls_config_mock.call_args.kwargs["server_hostname"], "TrueNAS.gcs8.io")


if __name__ == "__main__":
    unittest.main()
