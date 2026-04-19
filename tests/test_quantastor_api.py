from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.config import TrueNASConfig
from app.services.quantastor_api import QuantastorRESTClient


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
