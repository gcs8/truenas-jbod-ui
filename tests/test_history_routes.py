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
    def test_status_asgi_reprojects_recovery_without_internal_top_level_fields(self):
        async def request():
            messages = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                messages.append(message)
            path = "/api/history/status"
            await app_main.app({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": [], "client": ("synthetic.test", 1), "server": ("synthetic.test", 80)}, receive, send)
            return next(m["status"] for m in messages if m["type"] == "http.response.start"), b"".join(
                m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        backend = Mock()
        backend.get_status = AsyncMock(return_value={"configured": True, "available": True,
            "ready": True, "recovery_required": True, "collection_paused": False,
            "recovery_state": "status-leak-ZXQ9", "recovery_id": "status-leak-ZXQ9",
            "detail": "status-leak-ZXQ9", "counts": {"tracked_slots": 0}, "collector": {}})
        with patch.object(app_main, "get_history_backend", return_value=backend):
            status, body = asyncio.run(request())
        self.assertEqual(status, 200, "Status observation is not service readiness")
        payload = json.loads(body)
        self.assertIs(payload.get("ready"), False)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["status"], "recovery_required")
        self.assertEqual(payload["recovery_state"], "unavailable")
        self.assertNotIn(b"status-leak-ZXQ9", body)

    def _route(self, path: str):
        return next(route for route in app_main.app.routes if getattr(route, "path", None) == path)

    def test_history_status_route_reprojects_backend_result(self) -> None:
        route = self._route("/api/history/status")
        backend_payload = {
            "configured": True,
            "available": True,
            "detail": None,
            "counts": {"tracked_slots": 12},
            "scopes": [{"system_id": "synthetic-system"}],
            "collector": {
                "collector_running": True,
                "last_success_at": "2026-09-06T10:00:00+00:00",
                "last_completed_at": "2026-09-06T09:59:00+00:00",
                "last_error": "status-leak-ZXQ9 raw backend failure",
                "source_base_url": "https://collector.status-leak-ZXQ9.example.test",
                "sqlite_path": "/synthetic/private/status-leak-ZXQ9/history.db",
                "collection_stage_timings": [{"error": "status-leak-ZXQ9"}],
                "future_internal_metadata": "status-leak-ZXQ9",
            },
        }
        history_backend = Mock()
        history_backend.get_status = AsyncMock(return_value=backend_payload)

        with patch.object(app_main, "get_history_backend", return_value=history_backend):
            response = asyncio.run(route.endpoint())

        payload = json.loads(response.body)
        self.assertEqual(
            payload,
            {
                "configured": True,
                "available": True,
                "detail": None,
                "counts": {"tracked_slots": 12},
                "scopes": [{"system_id": "synthetic-system"}],
                "collector": {
                    "collector_running": True,
                    "last_success_at": "2026-09-06T10:00:00+00:00",
                    "last_completed_at": "2026-09-06T09:59:00+00:00",
                    "last_error": "History backend is degraded; see history service logs.",
                },
            },
        )
        self.assertNotIn("status-leak-ZXQ9", response.body.decode())

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
