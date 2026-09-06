from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app import main as app_main
from app.config import HistoryConfig, Settings
from app.services import history_backend as history_backend_module
from app.services.history_backend import HistoryBackendPolicyError


class AppHistoryBoundsTests(unittest.TestCase):
    def _route(self, path: str):
        return next(route for route in app_main.app.routes if getattr(route, "path", None) == path)

    @staticmethod
    def _json_request(document: dict[str, object]) -> Request:
        body = json.dumps(document).encode()
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/history/scopes/bundle",
                "raw_path": b"/api/history/scopes/bundle",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
        )

    def _assert_busy_response(self, response) -> None:
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(
            json.loads(response.body),
            {"detail": "History read capacity is temporarily busy; retry later."},
        )

    def test_scope_route_rejects_missing_window_and_slots_before_backend(self) -> None:
        route = self._route("/api/history/scope")
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock()
        backend.get_scope_history = AsyncMock()
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=backend),
        ):
            for slots, window_hours in ((None, 24), ([], 24), ([0], None), ([0], 8761)):
                with self.subTest(slots=slots, window_hours=window_hours), self.assertRaises(HTTPException):
                    asyncio.run(
                        route.endpoint(
                            system_id=None,
                            enclosure_id="front",
                            slots=slots,
                            window_hours=window_hours,
                            metrics=["temperature_c"],
                            event_limit=0,
                            metric_limit=24,
                        )
                    )
        backend.get_scope_history.assert_not_awaited()

    def test_scope_route_preserves_sparse_slot_values_and_explicit_bounds(self) -> None:
        route = self._route("/api/history/scope")
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock(configured=True)
        backend.get_scope_history = AsyncMock(return_value={999999: {"available": True}})
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "resolve_read_layout_slots", AsyncMock(return_value=(None, "unavailable"))),
            patch.object(app_main, "get_history_backend", return_value=backend),
        ):
            response = asyncio.run(
                route.endpoint(
                    system_id=None,
                    enclosure_id="front",
                    slots=[999999],
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
            )
        self.assertEqual(response.status_code, 200)
        backend.get_scope_history.assert_awaited_once_with(
            system_id="synthetic",
            enclosure_id="front",
            slots=[999999],
            window_hours=24,
            metrics=["temperature_c"],
            event_limit=0,
            metric_limit=24,
        )

    def test_storage_view_history_batches_all_enclosure_scopes_once(self) -> None:
        route = self._route("/api/storage-views/{view_id}/history")
        runtime_view = SimpleNamespace(
            id="view-a",
            backing_enclosure_id="front",
            slots=[
                SimpleNamespace(slot_index=0, snapshot_slot=5),
                SimpleNamespace(slot_index=1, snapshot_slot=None),
            ],
        )
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        service.get_storage_view_runtime = AsyncMock(return_value=SimpleNamespace(views=[runtime_view]))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock(configured=True)
        backend.get_scopes_history = AsyncMock(
            return_value={
                "scopes": [
                    {"system_id": "synthetic", "enclosure_id": "front", "histories": {"5": {"available": True}}},
                    {"system_id": "synthetic", "enclosure_id": "storage-view:view-a", "histories": {"1": {"available": True}}},
                ]
            }
        )
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=backend),
        ):
            response = asyncio.run(
                route.endpoint(
                    view_id="view-a",
                    system_id=None,
                    enclosure_id=None,
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
            )
        self.assertEqual(response.status_code, 200)
        backend.get_scopes_history.assert_awaited_once()
        sent_scopes = backend.get_scopes_history.await_args.kwargs["scopes"]
        self.assertEqual(len(sent_scopes), 2)
        self.assertEqual(json.loads(response.body)["histories"], {"0": {"available": True}, "1": {"available": True}})

    def test_main_refresh_proxy_uses_internal_backend_client(self) -> None:
        route = self._route("/api/history/refresh")
        backend = Mock()
        backend.refresh = AsyncMock(return_value={"ok": True, "mode": "full"})
        with patch.object(app_main, "get_history_backend", return_value=backend):
            response = asyncio.run(route.endpoint(payload=SimpleNamespace(mode="full")))
        self.assertEqual(json.loads(response.body)["mode"], "full")
        backend.refresh.assert_awaited_once_with("full")
        dependency_names = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        self.assertIn("require_read_ui_mutation_authorization", dependency_names)

    def test_main_history_proxies_preserve_sidecar_policy_status(self) -> None:
        route = self._route("/api/history/refresh")
        backend = Mock()
        backend.refresh = AsyncMock(side_effect=HistoryBackendPolicyError(429))
        with patch.object(app_main, "get_history_backend", return_value=backend):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(route.endpoint(payload=SimpleNamespace(mode="full")))
        self.assertEqual(raised.exception.status_code, 429)

    def test_history_refresh_token_is_file_capable_in_main_config(self) -> None:
        config = HistoryConfig(refresh_token="synthetic-token")
        self.assertEqual(config.refresh_token.get_secret_value(), "synthetic-token")
        self.assertNotIn("synthetic-token", repr(config))
        self.assertIsInstance(Settings().history, HistoryConfig)

    def test_multi_scope_proxy_preserves_bounded_busy_response(self) -> None:
        route = self._route("/api/history/scopes/bundle")
        backend = Mock()
        backend.get_scopes_history = AsyncMock(
            side_effect=history_backend_module.HistoryBackendBusyError()
        )
        request = self._json_request(
            {
                "scopes": [{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
                "metrics": ["temperature_c"],
                "since": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),
                "event_limit": 0,
                "metric_limit": 1,
            }
        )
        with patch.object(app_main, "get_history_backend", return_value=backend):
            response = asyncio.run(route.endpoint(request=request))
        self._assert_busy_response(response)

    def test_scope_proxy_preserves_bounded_busy_response(self) -> None:
        route = self._route("/api/history/scope")
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock(configured=True)
        backend.get_scope_history = AsyncMock(
            side_effect=history_backend_module.HistoryBackendBusyError()
        )
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "resolve_read_layout_slots", AsyncMock(return_value=(None, "unavailable"))),
            patch.object(app_main, "get_history_backend", return_value=backend),
        ):
            response = asyncio.run(
                route.endpoint(
                    system_id=None,
                    enclosure_id="front",
                    slots=[0],
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=1,
                )
            )
        self._assert_busy_response(response)

    def test_storage_view_proxy_preserves_bounded_busy_response(self) -> None:
        route = self._route("/api/storage-views/{view_id}/history")
        runtime_view = SimpleNamespace(
            id="view-a",
            backing_enclosure_id="front",
            slots=[SimpleNamespace(slot_index=0, snapshot_slot=0)],
        )
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        service.get_storage_view_runtime = AsyncMock(return_value=SimpleNamespace(views=[runtime_view]))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock(configured=True)
        backend.get_scopes_history = AsyncMock(
            side_effect=history_backend_module.HistoryBackendBusyError()
        )
        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=backend),
        ):
            response = asyncio.run(
                route.endpoint(
                    view_id="view-a",
                    system_id=None,
                    enclosure_id=None,
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=1,
                )
            )
        self._assert_busy_response(response)


if __name__ == "__main__":
    unittest.main()
