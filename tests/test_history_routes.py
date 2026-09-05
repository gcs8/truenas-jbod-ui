from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app import main as app_main
from app.config import Settings
from app.models.domain import InventorySnapshot


class SlotHistoryRouteTests(unittest.TestCase):
    def _route(self, path: str):
        return next(route for route in app_main.app.routes if getattr(route, "path", None) == path)

    def test_slot_history_resolves_the_default_system_when_system_id_is_omitted(self) -> None:
        route = self._route("/api/slots/{slot}/history")
        service = Mock()
        service.system = SimpleNamespace(id="system-a", truenas=SimpleNamespace(platform="core"))
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(
                slots=[],
                layout_slot_count=60,
                selected_enclosure_id="enc-a",
                refresh_interval_seconds=30,
            )
        )
        registry = Mock()
        registry.get_service.return_value = service
        backend_payload = {"configured": True, "available": True, "slot": 5, "metrics": {}}
        history_backend = Mock()
        history_backend.get_slot_history = AsyncMock(return_value=backend_payload)

        with (
            patch.object(app_main, "get_settings", return_value=Settings()),
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
        ):
            response = asyncio.run(
                route.endpoint(slot=5, system_id=None, enclosure_id="enc-a", window_hours=24)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {**backend_payload, "layout_bounds": "verified"},
        )
        registry.get_service.assert_called_once_with(None)
        history_backend.get_slot_history.assert_awaited_once_with(
            5,
            "system-a",
            "enc-a",
            window_hours=24,
        )

if __name__ == "__main__":
    unittest.main()
