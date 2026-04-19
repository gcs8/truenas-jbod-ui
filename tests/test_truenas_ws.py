from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import TrueNASConfig
from app.services.truenas_ws import TrueNASWebsocketClient


class TrueNASWebsocketClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_collects_payloads_in_parallel(self) -> None:
        class TrackingClient(TrueNASWebsocketClient):
            def __init__(self) -> None:
                super().__init__(TrueNASConfig(api_key="token"))
                self.active_calls = 0
                self.max_active_calls = 0

            @asynccontextmanager
            async def _session(self):
                class DummyWS:
                    async def send(self, _payload):
                        return None

                    async def recv(self):
                        await asyncio.Future()

                yield DummyWS()

            async def _track(self, result):
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    await asyncio.sleep(0.01)
                    return result
                finally:
                    self.active_calls -= 1

            async def _fetch_enclosures(self, _call_method):
                return await self._track([{"id": "enc-1"}])

            async def _fetch_disks(self, _call_method):
                return await self._track([{"name": "da0"}])

            async def _fetch_pools(self, _call_method):
                return await self._track([{"name": "tank"}])

            async def _fetch_disk_temperatures(self, _call_method):
                return await self._track({"da0": 30})

            async def _fetch_smart_test_results(self, _call_method):
                return await self._track([{"disk": "da0", "status": "SUCCESS"}])

        client = TrackingClient()

        payload = await client.fetch_all()

        self.assertEqual(client.max_active_calls, 5)
        self.assertEqual(payload.enclosures[0]["id"], "enc-1")
        self.assertEqual(payload.disks[0]["name"], "da0")
        self.assertEqual(payload.pools[0]["name"], "tank")
        self.assertEqual(payload.disk_temperatures["da0"], 30)
        self.assertEqual(payload.smart_test_results[0]["status"], "SUCCESS")

    @patch("app.services.truenas_ws.connect")
    async def test_session_passes_tls_server_name_override_to_connect(self, connect_mock: MagicMock) -> None:
        client = TrueNASWebsocketClient(
            TrueNASConfig(
                host="https://10.13.37.10",
                api_key="token",
                tls_server_name="TrueNAS.gcs8.io",
            )
        )

        websocket = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = websocket
        cm.__aexit__.return_value = False
        connect_mock.return_value = cm

        with patch.object(client, "_perform_handshake", AsyncMock()):
            async with client._session():
                pass

        self.assertEqual(connect_mock.call_args.kwargs["server_hostname"], "TrueNAS.gcs8.io")


if __name__ == "__main__":
    unittest.main()
