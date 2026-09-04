from __future__ import annotations

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from admin_service import main as admin_main
from admin_service.config import AdminSettings
from app import main as app_main
from app.config import Settings, SystemConfig
from app.models.domain import InventorySnapshot, StorageViewRuntimePayload, SystemLocatorStatusView, SystemOption
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


def _authorized_mutation_headers() -> dict[str, str]:
    credentials = base64.b64encode(b"operator:test-password").decode("ascii")
    return {
        "Authorization": f"Basic {credentials}",
        "Origin": "http://testserver",
    }


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
    def test_explicit_unknown_inventory_read_returns_404_without_calling_default_service(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)

        with patch.object(app_main, "get_inventory_registry", return_value=registry):
            response = TestClient(app_main.app).get(
                "/api/inventory",
                params={"system_id": UNKNOWN_SYSTEM_ID},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL})
        default_service.get_snapshot.assert_not_awaited()

    def test_explicit_unknown_locator_mutation_returns_404_without_calling_default_service(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        original_auth_settings = app_main.app.state.operator_auth_settings
        original_public_origin = app_main.app.state.read_ui_public_origin
        app_main.app.state.operator_auth_settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr("test-password"),
        )
        app_main.app.state.read_ui_public_origin = "http://testserver"
        try:
            with patch.object(app_main, "get_inventory_registry", return_value=registry):
                response = TestClient(app_main.app).post(
                    "/api/system-locator",
                    params={"system_id": UNKNOWN_SYSTEM_ID},
                    json={"active": True},
                    headers=_authorized_mutation_headers(),
                )
        finally:
            app_main.app.state.operator_auth_settings = original_auth_settings
            app_main.app.state.read_ui_public_origin = original_public_origin

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL})
        default_service.set_system_locator.assert_not_awaited()

    def test_explicit_unknown_slot_history_returns_404_without_calling_default_or_history_backend(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        history_backend = Mock()
        history_backend.get_slot_history = AsyncMock(return_value={"available": True})

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend) as backend_getter,
        ):
            response = TestClient(app_main.app).get(
                "/api/slots/5/history",
                params={"system_id": UNKNOWN_SYSTEM_ID, "enclosure_id": "enc-a"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL})
        default_service.get_snapshot.assert_not_awaited()
        backend_getter.assert_not_called()
        history_backend.get_slot_history.assert_not_awaited()

    def test_explicit_unknown_history_scope_returns_404_without_calling_history_backend(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)
        history_backend = Mock()
        history_backend.get_scope_history = AsyncMock(return_value={})

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_history_backend", return_value=history_backend) as backend_getter,
        ):
            response = TestClient(app_main.app).get(
                "/api/history/scope",
                params={"system_id": UNKNOWN_SYSTEM_ID},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL})
        backend_getter.assert_not_called()
        history_backend.get_scope_history.assert_not_awaited()

    def test_admin_inventory_route_maps_explicit_unknown_system_to_404(self) -> None:
        default_service = _default_service()
        registry = _registry_with_default_service(default_service)

        with (
            patch.object(admin_main, "reload_app_settings", return_value=registry.settings),
            patch.object(admin_main, "InventoryRegistry", return_value=registry),
        ):
            response = TestClient(admin_main.app).get(
                "/api/admin/storage-views/candidates",
                params={"system_id": UNKNOWN_SYSTEM_ID},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "detail": UNKNOWN_SYSTEM_DETAIL})
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

        with (
            patch.object(app_main, "get_settings", return_value=settings),
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "get_release_status_service", return_value=release_service),
        ):
            response = TestClient(app_main.app).get(
                "/",
                params={"system_id": UNKNOWN_SYSTEM_ID},
            )

        self.assertEqual(response.status_code, 200)
        registry.get_service.assert_called_once_with("system-a")
        self.assertIn('value="system-a" selected', response.text)
        self.assertNotIn(UNKNOWN_SYSTEM_ID, response.text)


if __name__ == "__main__":
    unittest.main()
