from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Request
from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from admin_service import main as admin_main
from app import main as app_main
from app.config import Settings, SystemConfig
from app.models.domain import (
    InventorySnapshot,
    StorageViewRuntimePayload,
    SystemLocatorRequest,
    SystemLocatorStatusView,
    SystemOption,
)
from app.services.inventory_registry import InventoryRegistry, SystemNotConfiguredError


UNKNOWN_SYSTEM_ID = "retired-nas"
UNKNOWN_SYSTEM_DETAIL = f"System '{UNKNOWN_SYSTEM_ID}' is not configured."


def _registry_with_default_service(default_service: Mock) -> InventoryRegistry:
    registry = object.__new__(InventoryRegistry)
    registry.settings = Settings(
        systems=[SystemConfig(id="system-a", label="System A")],
        default_system_id="system-a",
    )
    registry._services = {"system-a": default_service}
    return registry


def _default_service() -> Mock:
    service = Mock()
    service.system = SimpleNamespace(id="system-a", truenas=SimpleNamespace(platform="core"))
    service.get_snapshot = AsyncMock(
        return_value=InventorySnapshot(
            slots=[],
            systems=[SystemOption(id="system-a", label="System A", platform="core")],
            selected_system_id="system-a",
            selected_system_label="System A",
            selected_system_platform="core",
            layout_slot_count=60,
            refresh_interval_seconds=30,
        )
    )
    service.get_storage_view_runtime = AsyncMock(
        return_value=StorageViewRuntimePayload(system_id="system-a", views=[])
    )
    service.get_storage_view_candidates = AsyncMock(return_value=[])
    service.set_system_locator = AsyncMock(
        return_value=SystemLocatorStatusView(supported=True, active=True, backend="synthetic")
    )
    return service


def _route(application, path: str, method: str = "GET") -> APIRoute:
    return next(
        route
        for route in application.routes
        if isinstance(route, APIRoute)
        and getattr(route, "path", None) == path
        and method in (getattr(route, "methods", None) or set())
    )


def _request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
            "app": app_main.app,
        }
    )


class InventoryRegistrySelectionTests(unittest.TestCase):
    def test_omitted_system_id_resolves_the_configured_default(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)

        self.assertEqual(registry.get_system(None).id, "system-a")
        self.assertIs(registry.get_service(None), default_service)

    def test_explicit_unknown_system_id_does_not_resolve_the_default_system(self) -> None:
        registry = _registry_with_default_service(_default_service())

        with self.assertRaisesRegex(
            SystemNotConfiguredError,
            "^System 'retired-nas' is not configured\\.$",
        ):
            registry.get_system(UNKNOWN_SYSTEM_ID)

    def test_explicit_unknown_system_id_does_not_resolve_the_default_service(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)

        with self.assertRaisesRegex(
            SystemNotConfiguredError,
            "^System 'retired-nas' is not configured\\.$",
        ):
            registry.get_service(UNKNOWN_SYSTEM_ID)

        self.assertEqual(registry._services, {"system-a": default_service})


class UnknownSystemRouteTests(unittest.TestCase):
    def assert_unknown_system(self, callback) -> None:
        with self.assertRaisesRegex(
            SystemNotConfiguredError,
            "^System 'retired-nas' is not configured\\.$",
        ):
            asyncio.run(callback())

    def test_registered_handlers_map_unknown_system_errors_to_404(self) -> None:
        error = SystemNotConfiguredError(UNKNOWN_SYSTEM_ID)
        for application, handler in (
            (app_main.app, app_main.system_not_configured_exception_handler),
            (admin_main.app, admin_main.system_not_configured_exception_handler),
        ):
            with self.subTest(application=application.title):
                self.assertIs(application.exception_handlers[SystemNotConfiguredError], handler)
                response = asyncio.run(handler(Mock(), error))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(
                    json.loads(bytes(response.body)),
                    {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL},
                )

    def test_explicit_unknown_inventory_read_returns_404_without_calling_default_service(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        route = _route(app_main.app, "/api/inventory")

        with patch.object(app_main, "get_inventory_registry", return_value=registry):
            self.assert_unknown_system(
                lambda: route.endpoint(
                    force=False,
                    system_id=UNKNOWN_SYSTEM_ID,
                    enclosure_id=None,
                )
            )

        default_service.get_snapshot.assert_not_awaited()

    def test_explicit_unknown_locator_mutation_returns_404_without_calling_default_service(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        route = _route(app_main.app, "/api/system-locator", "POST")

        with patch.object(app_main, "get_inventory_registry", return_value=registry):
            self.assert_unknown_system(
                lambda: route.endpoint(
                    payload=SystemLocatorRequest(active=True),
                    system_id=UNKNOWN_SYSTEM_ID,
                )
            )

        default_service.set_system_locator.assert_not_awaited()

    def test_explicit_unknown_slot_history_returns_404_without_calling_default_or_history_backend(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        history_backend = Mock()
        history_backend.get_slot_history = AsyncMock(return_value={"available": True})
        route = _route(app_main.app, "/api/slots/{slot}/history")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend) as backend_getter,
        ):
            self.assert_unknown_system(
                lambda: route.endpoint(
                    slot=5,
                    system_id=UNKNOWN_SYSTEM_ID,
                    enclosure_id="enc-a",
                    window_hours=None,
                )
            )

        default_service.get_snapshot.assert_not_awaited()
        backend_getter.assert_not_called()
        history_backend.get_slot_history.assert_not_awaited()

    def test_explicit_unknown_history_scope_returns_404_without_calling_history_backend(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        history_backend = Mock()
        history_backend.get_scope_history = AsyncMock(return_value={})
        route = _route(app_main.app, "/api/history/scope")

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend) as backend_getter,
        ):
            self.assert_unknown_system(
                lambda: route.endpoint(
                    system_id=UNKNOWN_SYSTEM_ID,
                    enclosure_id=None,
                    slots=None,
                    window_hours=None,
                    metrics=None,
                    event_limit=12,
                )
            )

        backend_getter.assert_not_called()
        history_backend.get_scope_history.assert_not_awaited()

    def test_admin_inventory_route_maps_explicit_unknown_system_to_404(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        route = _route(admin_main.app, "/api/admin/storage-views/candidates")

        with (
            patch.object(admin_main, "reload_app_settings", return_value=registry.settings),
            patch.object(admin_main, "InventoryRegistry", return_value=registry),
        ):
            self.assert_unknown_system(
                lambda: route.endpoint(
                    system_id=UNKNOWN_SYSTEM_ID,
                    target_system_id=None,
                    force=False,
                )
            )

        default_service.get_storage_view_candidates.assert_not_awaited()

    def test_index_with_unknown_system_explicitly_selects_and_renders_default(self) -> None:
        settings = Settings(
            systems=[SystemConfig(id="system-a", label="System A")],
            default_system_id="system-a",
        )
        service = _default_service()
        registry = Mock()
        registry.get_service.return_value = service
        release_service = Mock()
        release_service.snapshot.return_value = {}
        route = _route(app_main.app, "/")

        with (
            patch.object(app_main, "get_settings", return_value=settings),
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_release_status_service", return_value=release_service),
            patch.object(app_main, "resolve_admin_launch_url", return_value=None),
        ):
            response = asyncio.run(
                route.endpoint(
                    request=_request(),
                    system_id=UNKNOWN_SYSTEM_ID,
                    enclosure_id=None,
                )
            )

        self.assertEqual(response.status_code, 200)
        registry.get_service.assert_called_once_with("system-a")
        response_text = response.body.decode("utf-8")
        self.assertIn('value="system-a" selected', response_text)
        self.assertNotIn(UNKNOWN_SYSTEM_ID, response_text)


if __name__ == "__main__":
    unittest.main()
