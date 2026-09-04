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
        self.assertEqual(json.loads(response.body), backend_payload)
        registry.get_service.assert_called_once_with(None)
        history_backend.get_slot_history.assert_awaited_once_with(
            5,
            "system-a",
            "enc-a",
            window_hours=24,
        )

    def test_slot_history_queries_the_resolved_system_for_an_unknown_system_id(self) -> None:
        """Regression for #286: the registry silently resolves an unknown or
        retired ``system_id`` to the default system, and the slot bounds are
        checked against that system. The history query must scope to the same
        resolved system, as ``/api/history/scope`` already does, instead of
        forwarding the raw query string to the backend."""
        route = self._route("/api/slots/{slot}/history")
        service = Mock()
        service.system = SimpleNamespace(id="default-system", truenas=SimpleNamespace(platform="core"))
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
        history_backend = Mock()
        history_backend.get_slot_history = AsyncMock(return_value={"available": False})
        perf_metadata = Mock()

        with (
            patch.object(app_main, "get_settings", return_value=Settings()),
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
            patch.object(app_main, "add_perf_metadata", perf_metadata),
        ):
            asyncio.run(
                route.endpoint(
                    slot=5,
                    system_id="unknown-system",
                    enclosure_id="enc-a",
                    window_hours=24,
                )
            )

        registry.get_service.assert_called_once_with("unknown-system")
        history_backend.get_slot_history.assert_awaited_once_with(
            5,
            "default-system",
            "enc-a",
            window_hours=24,
        )
        self.assertEqual(perf_metadata.call_args.kwargs["system_id"], "default-system")

    def test_slot_history_and_history_scope_agree_on_the_system_for_the_same_query(self) -> None:
        slot_route = self._route("/api/slots/{slot}/history")
        scope_route = self._route("/api/history/scope")
        service = Mock()
        service.system = SimpleNamespace(id="default-system", truenas=SimpleNamespace(platform="core"))
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
        history_backend = Mock()
        history_backend.configured = True
        history_backend.get_slot_history = AsyncMock(return_value={"available": False})
        history_backend.get_scope_history = AsyncMock(return_value={})

        with (
            patch.object(app_main, "get_settings", return_value=Settings()),
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend),
            patch.object(app_main, "add_perf_metadata"),
        ):
            asyncio.run(
                slot_route.endpoint(slot=5, system_id="retired-nas", enclosure_id="enc-a", window_hours=24)
            )
            asyncio.run(
                scope_route.endpoint(
                    system_id="retired-nas",
                    enclosure_id="enc-a",
                    slots=[5],
                    window_hours=24,
                    metrics=None,
                    event_limit=12,
                )
            )

        slot_system_id = history_backend.get_slot_history.await_args.args[1]
        scope_system_id = history_backend.get_scope_history.await_args.kwargs["system_id"]
        self.assertEqual(scope_system_id, "default-system")
        self.assertEqual(slot_system_id, scope_system_id)


if __name__ == "__main__":
    unittest.main()
