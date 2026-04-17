from __future__ import annotations

import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
