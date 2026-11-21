from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main as app_main
from app import perf
from app import route_support
from app import routes as app_routes
from app.config import Settings
from app.models.domain import InventorySnapshot
from app.services import history_backend as history_backend_module
from app.services import history_status
from app.services.history_backend import HISTORY_BACKEND_DEGRADED_DETAIL
from app.services.history_backend import HistoryBackendBusyError
from history_service import operation_bounds, refresh_auth


class SlotHistoryRouteTests(unittest.TestCase):
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

        with patch.object(app_routes, "get_history_backend", return_value=history_backend):
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
                    "last_error": HISTORY_BACKEND_DEGRADED_DETAIL,
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
            patch.object(app_routes, "get_settings", return_value=Settings()),
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "get_history_backend", return_value=history_backend),
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


HISTORY_ENDPOINTS = (
    ("/api/storage-views/{view_id}/slots/{slot_index}/history", "GET", "get_storage_view_slot_history"),
    ("/api/history/status", "GET", "get_history_status"),
    ("/api/history/refresh", "POST", "refresh_history_proxy"),
    ("/api/history/scopes/bundle", "POST", "get_history_scopes_bundle"),
    ("/api/slots/{slot}/history", "GET", "get_slot_history"),
    ("/api/history/scope", "GET", "get_history_scope"),
    ("/api/storage-views/{view_id}/history", "GET", "get_storage_view_history"),
)


class HistoryRoutesModuleTests(unittest.TestCase):
    """The history endpoints live in app.history_routes behind app.routes."""

    def _history_route(self, path: str):
        route = next(route for route in app_main.app.routes if getattr(route, "path", None) == path)
        self.assertEqual(route.endpoint.__module__, "app.history_routes")
        return route

    def test_history_routes_module_imports_without_the_routes_module(self) -> None:
        code = (
            "import json, sys\n"
            "import app.history_routes as module\n"
            "print(json.dumps([\n"
            "    'app.routes' in sys.modules,\n"
            "    'app.main' in sys.modules,\n"
            "    callable(getattr(module, 'add_storage_view_slot_history_route', None)),\n"
            "    callable(getattr(module, 'add_history_routes', None)),\n"
            "]))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [False, False, True, True])

    def test_routes_module_keeps_the_names_the_history_endpoints_read(self) -> None:
        history_routes = importlib.import_module("app.history_routes")
        self.assertIs(app_routes.add_history_routes, history_routes.add_history_routes)
        self.assertIs(
            app_routes.add_storage_view_slot_history_route,
            history_routes.add_storage_view_slot_history_route,
        )
        reexports = {
            "get_history_backend": (route_support, "get_history_backend"),
            "get_inventory_registry": (route_support, "get_inventory_registry"),
            "ensure_read_slot_bounds": (route_support, "ensure_read_slot_bounds"),
            "resolve_read_layout_slots": (route_support, "resolve_read_layout_slots"),
            "check_slot_bounds": (route_support, "check_slot_bounds"),
            "require_read_ui_mutation_authorization": (route_support, "require_read_ui_mutation_authorization"),
            "HistoryRefreshProxyRequest": (route_support, "HistoryRefreshProxyRequest"),
            "HistoryScopeProxyRequest": (route_support, "HistoryScopeProxyRequest"),
            "HistoryScopesProxyRequest": (route_support, "HistoryScopesProxyRequest"),
            "add_perf_metadata": (perf, "add_perf_metadata"),
            "project_public_collector_status": (history_status, "project_public_collector_status"),
            "HISTORY_BACKEND_DEGRADED_DETAIL": (history_backend_module, "HISTORY_BACKEND_DEGRADED_DETAIL"),
            "build_history_read_plan": (operation_bounds, "build_history_read_plan"),
            "ALLOWED_HISTORY_METRICS": (operation_bounds, "ALLOWED_HISTORY_METRICS"),
            "MAX_TARGETS": (operation_bounds, "MAX_TARGETS"),
            "read_limited_request_body": (refresh_auth, "read_limited_request_body"),
        }
        for name, (module, source_name) in reexports.items():
            with self.subTest(name=name):
                self.assertIs(getattr(app_routes, name), getattr(module, source_name))
        for name in (
            "_history_read_busy_response",
            "_is_json_media_type",
            "_read_failure_http_exception",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(app_routes, name).__module__, "app.routes")
        self.assertEqual(app_routes.MAX_HISTORY_SCOPES_REQUEST_BYTES, 64 * 1024)
        self.assertEqual(
            app_routes.STORAGE_VIEW_HISTORY_FAILURE_DETAIL,
            "Storage view data could not be loaded.",
        )

    def test_build_router_registers_the_history_endpoints_from_their_module(self) -> None:
        router = app_routes.build_router()
        moved = [
            (route.path, method, route.name)
            for route in router.routes
            if route.endpoint.__module__ == "app.history_routes"
            for method in sorted(route.methods)
        ]
        self.assertEqual(moved, list(HISTORY_ENDPOINTS))

    def test_a_replaced_routes_helper_reaches_the_moved_endpoint(self) -> None:
        # Call-time lookup through app.routes: a private helper replaced there
        # is the one the moved endpoint answers with.
        route = self._history_route("/api/history/scope")
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="core"))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock()
        backend.get_scope_history = AsyncMock(side_effect=HistoryBackendBusyError())
        sentinel = object()
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "resolve_read_layout_slots", AsyncMock(return_value=(None, "unavailable"))),
            patch.object(app_routes, "get_history_backend", return_value=backend),
            patch.object(app_routes, "_history_read_busy_response", return_value=sentinel) as busy,
        ):
            response = asyncio.run(
                route.endpoint(
                    system_id=None,
                    enclosure_id="front",
                    slots=[0],
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
            )
        self.assertIs(response, sentinel)
        busy.assert_called_once_with()

    def test_a_replaced_routes_constant_reaches_the_moved_endpoint(self) -> None:
        # Call-time lookup through app.routes also covers module constants.
        route = self._history_route("/api/history/scope")
        registry = Mock()
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "MAX_TARGETS", 1),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(
                route.endpoint(
                    system_id=None,
                    enclosure_id="front",
                    slots=[0, 1],
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
            )
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(
            raised.exception.detail,
            "History can be requested for at most 1 slots at a time; 2 requested.",
        )
        registry.get_service.assert_not_called()

    def test_a_replaced_routes_dependency_reaches_the_moved_route_at_build_time(self) -> None:
        # Registration-time lookup through app.routes: the mutation guard is
        # read from app.routes when build_router runs, as it was before.
        def replacement_guard() -> None:
            return None

        with patch.object(app_routes, "require_read_ui_mutation_authorization", replacement_guard):
            router = app_routes.build_router()
        route = next(route for route in router.routes if route.path == "/api/history/refresh")
        self.assertEqual(route.endpoint.__module__, "app.history_routes")
        self.assertEqual(
            [dependency.dependency for dependency in route.dependencies],
            [replacement_guard],
        )


if __name__ == "__main__":
    unittest.main()
