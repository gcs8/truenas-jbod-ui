from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import main as app_main
from app.models.domain import (
    InventorySnapshot,
    LedAction,
    LedRequest,
    ManualMapping,
    MappingBundle,
    MappingImportConfirmation,
    MappingRequest,
)
from app.services.mapping_store import (
    MappingImportDigestMismatch,
    MappingRevisionConflict,
)


PRIVATE_EXCEPTION_DETAIL = "Traceback: /srv/private/mappings.json contained operator data"


def with_private_exception_detail(error: Exception) -> Exception:
    error.args = (PRIVATE_EXCEPTION_DETAIL,)
    return error


class MappingImportRouteTests(unittest.TestCase):
    def test_mapping_import_preview_route_exists(self) -> None:
        routes = {
            (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", None) or [])))
            for route in app_main.app.routes
        }
        self.assertIn(("/api/mappings/import/preview", ("POST",)), routes)

    def test_mapping_import_preview_returns_service_diff_for_selected_scope(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", None) == "/api/mappings/import/preview"
        )
        bundle = MappingBundle(mappings=[ManualMapping(slot=2, serial="SANITIZED-2")])
        preview = {
            "revision": "a" * 64,
            "import_digest": "b" * 64,
            "system_id": "system-a",
            "enclosure_id": "enc-a",
            "additions": [{"enclosure_id": "enc-a", "slot": 2}],
            "updates": [],
            "removals": [],
            "unchanged": [],
        }
        service = Mock()
        service.preview_mapping_bundle = AsyncMock(return_value=preview)
        registry = Mock()
        registry.get_service.return_value = service

        with patch.object(app_main, "get_inventory_registry", return_value=registry):
            response = asyncio.run(
                route.endpoint(payload=bundle, system_id="system-a", enclosure_id="enc-a")
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), preview)
        registry.get_service.assert_called_once_with("system-a")
        service.preview_mapping_bundle.assert_awaited_once_with(
            bundle,
            selected_enclosure_id="enc-a",
        )

    def test_mapping_import_requires_exact_preview_confirmation_fields(self) -> None:
        app_main.app.openapi_schema = None
        schema = app_main.app.openapi()
        request_schema = schema["paths"]["/api/mappings/import"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        component_name = request_schema["$ref"].rsplit("/", 1)[-1]
        component = schema["components"]["schemas"][component_name]

        self.assertEqual(
            set(component.get("required", [])),
            {"bundle", "expected_revision", "import_digest", "confirmed"},
        )

    def test_single_mapping_mutations_require_scope_revision(self) -> None:
        app_main.app.openapi_schema = None
        schema = app_main.app.openapi()
        path = schema["paths"]["/api/slots/{slot}/mapping"]
        save_schema = path["post"]["requestBody"]["content"]["application/json"]["schema"]
        save_component = schema["components"]["schemas"][save_schema["$ref"].rsplit("/", 1)[-1]]
        clear_revision = next((
            parameter
            for parameter in path["delete"]["parameters"]
            if parameter["name"] == "expected_revision"
        ), None)

        self.assertIn("expected_revision", save_component.get("required", []))
        self.assertIsNotNone(clear_revision)
        self.assertTrue(clear_revision["required"])

    def test_single_mapping_mutations_forward_exact_scope_revision(self) -> None:
        save_route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/mapping"
            and "POST" in (getattr(route, "methods", None) or set())
        )
        clear_route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/mapping"
            and "DELETE" in (getattr(route, "methods", None) or set())
        )
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = "core"
        service.save_mapping = AsyncMock(
            return_value=ManualMapping(
                system_id="system-a",
                enclosure_id="enc-a",
                slot=2,
                serial="SANITIZED",
            )
        )
        service.clear_mapping = AsyncMock(return_value=True)
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(slots=[], refresh_interval_seconds=30)
        )
        registry = Mock()
        registry.get_service.return_value = service
        payload = MappingRequest(
            expected_revision="a" * 64,
            serial="SANITIZED",
            clear_identify_after_save=False,
        )

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "ensure_slot_bounds"),
            patch.object(app_main, "add_perf_metadata"),
        ):
            asyncio.run(
                save_route.endpoint(
                    slot=2,
                    payload=payload,
                    system_id="system-a",
                    enclosure_id="enc-a",
                )
            )
            asyncio.run(
                clear_route.endpoint(
                    slot=2,
                    system_id="system-a",
                    enclosure_id="enc-a",
                    expected_revision="b" * 64,
                )
            )

        service.save_mapping.assert_awaited_once_with(
            2,
            {
                "serial": "SANITIZED",
                "device_name": None,
                "gptid": None,
                "notes": None,
            },
            selected_enclosure_id="enc-a",
            expected_revision="a" * 64,
            invalidate_snapshot=False,
        )
        service.clear_mapping.assert_awaited_once_with(
            2,
            selected_enclosure_id="enc-a",
            expected_revision="b" * 64,
            invalidate_snapshot=False,
        )
        self.assertEqual(service.invalidate_physical_enclosure_snapshot_cache.call_count, 2)
        service.invalidate_physical_enclosure_snapshot_cache.assert_any_call(
            reason="route.save_mapping",
            enclosure_id="enc-a",
            invalidate_source_bundle=False,
        )
        service.invalidate_physical_enclosure_snapshot_cache.assert_any_call(
            reason="route.clear_mapping",
            enclosure_id="enc-a",
        )

    def test_led_mutation_invalidates_every_view_of_the_physical_enclosure(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/led"
            and "POST" in (getattr(route, "methods", None) or set())
        )
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = "scale"
        service.set_slot_led = AsyncMock()
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(slots=[], refresh_interval_seconds=30)
        )
        registry = Mock()
        registry.get_service.return_value = service

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "ensure_slot_bounds"),
            patch.object(app_main, "add_perf_metadata"),
        ):
            asyncio.run(
                route.endpoint(
                    slot=2,
                    payload=LedRequest(action=LedAction.identify),
                    system_id="system-a",
                    enclosure_id="enc-a::dell-md1280-top-drawer",
                )
            )

        service.invalidate_physical_enclosure_snapshot_cache.assert_called_once_with(
            reason="route.set_slot_led",
            enclosure_id="enc-a::dell-md1280-top-drawer",
            invalidate_source_bundle=True,
        )

    def test_mapping_save_that_clears_identify_invalidates_the_source_bundle(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/mapping"
            and "POST" in (getattr(route, "methods", None) or set())
        )
        service = Mock()
        service.system.id = "system-a"
        service.system.truenas.platform = "scale"
        service.save_mapping = AsyncMock(
            return_value=ManualMapping(
                system_id="system-a",
                enclosure_id="enc-a",
                slot=2,
                serial="SANITIZED",
            )
        )
        service.set_slot_led = AsyncMock()
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(slots=[], refresh_interval_seconds=30)
        )
        registry = Mock()
        registry.get_service.return_value = service

        with (
            patch.object(app_main, "get_inventory_registry", return_value=registry),
            patch.object(app_main, "ensure_slot_bounds"),
            patch.object(app_main, "add_perf_metadata"),
        ):
            asyncio.run(
                route.endpoint(
                    slot=2,
                    payload=MappingRequest(
                        expected_revision="a" * 64,
                        serial="SANITIZED",
                        clear_identify_after_save=True,
                    ),
                    system_id="system-a",
                    enclosure_id="enc-a::dell-md1280-top-drawer",
                )
            )

        service.invalidate_physical_enclosure_snapshot_cache.assert_called_once_with(
            reason="route.save_mapping",
            enclosure_id="enc-a",
            invalidate_source_bundle=True,
        )

    def test_stale_single_mapping_mutations_redact_exception_details(self) -> None:
        save_route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/mapping"
            and "POST" in (getattr(route, "methods", None) or set())
        )
        clear_route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", "") == "/api/slots/{slot}/mapping"
            and "DELETE" in (getattr(route, "methods", None) or set())
        )
        payload = MappingRequest(
            expected_revision="a" * 64,
            serial="STALE",
            clear_identify_after_save=True,
        )
        cases = [
            (save_route, {"payload": payload}),
            (clear_route, {"expected_revision": "a" * 64}),
        ]

        for route, arguments in cases:
            with self.subTest(method=next(iter(route.methods))):
                service = Mock()
                service.system.id = "system-a"
                service.system.truenas.platform = "core"
                service.save_mapping = AsyncMock(
                    side_effect=with_private_exception_detail(
                        MappingRevisionConflict("c" * 64)
                    )
                )
                service.clear_mapping = AsyncMock(
                    side_effect=with_private_exception_detail(
                        MappingRevisionConflict("c" * 64)
                    )
                )
                service.set_slot_led = AsyncMock()
                service.get_snapshot = AsyncMock()
                registry = Mock()
                registry.get_service.return_value = service
                with (
                    patch.object(app_main, "get_inventory_registry", return_value=registry),
                    patch.object(app_main, "ensure_slot_bounds"),
                    patch.object(app_main, "add_perf_metadata"),
                ):
                    response = asyncio.run(
                        route.endpoint(
                            slot=2,
                            **arguments,
                            system_id="system-a",
                            enclosure_id="enc-a",
                        )
                    )

                body = json.loads(response.body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(body["error"], "mapping_revision_conflict")
                self.assertEqual(body["current_revision"], "c" * 64)
                self.assertEqual(
                    body["detail"],
                    "Mapping scope revision changed before this write.",
                )
                self.assertNotIn(PRIVATE_EXCEPTION_DETAIL, response.body.decode("utf-8"))
                service.set_slot_led.assert_not_awaited()
                service.invalidate_snapshot_cache.assert_not_called()
                service.get_snapshot.assert_not_awaited()

    def test_confirmed_mapping_import_applies_exact_preview_then_returns_snapshot(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", None) == "/api/mappings/import"
        )
        bundle = MappingBundle(mappings=[ManualMapping(slot=2, serial="SANITIZED-2")])
        payload = MappingImportConfirmation(
            bundle=bundle,
            expected_revision="a" * 64,
            import_digest="b" * 64,
            confirmed=True,
        )
        service = Mock()
        service.import_mapping_bundle = AsyncMock(
            return_value={
                "imported": 1,
                "previous_revision": "a" * 64,
                "revision": "c" * 64,
                "preview": {"additions": [{"enclosure_id": "enc-a", "slot": 2}]},
            }
        )
        service.get_snapshot = AsyncMock(
            return_value=InventorySnapshot(slots=[], refresh_interval_seconds=30)
        )
        registry = Mock()
        registry.get_service.return_value = service

        with patch.object(app_main, "get_inventory_registry", return_value=registry):
            response = asyncio.run(
                route.endpoint(payload=payload, system_id="system-a", enclosure_id="enc-a")
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["imported"], 1)
        self.assertEqual(body["revision"], "c" * 64)
        service.import_mapping_bundle.assert_awaited_once_with(
            bundle,
            selected_enclosure_id="enc-a",
            expected_revision="a" * 64,
            import_digest="b" * 64,
            invalidate_snapshot=False,
        )
        service.invalidate_snapshot_cache.assert_called_once_with(reason="route.import_mappings")
        service.get_snapshot.assert_awaited_once_with(selected_enclosure_id="enc-a")

    def test_mapping_import_conflicts_return_current_version_without_mutation(self) -> None:
        route = next(
            route
            for route in app_main.app.routes
            if getattr(route, "path", None) == "/api/mappings/import"
        )
        payload = MappingImportConfirmation(
            bundle=MappingBundle(mappings=[]),
            expected_revision="a" * 64,
            import_digest="b" * 64,
            confirmed=True,
        )
        revision_error = with_private_exception_detail(
            MappingRevisionConflict("c" * 64)
        )
        digest_error = with_private_exception_detail(
            MappingImportDigestMismatch("c" * 64, "d" * 64)
        )
        cases = [
            (
                revision_error,
                {
                    "error": "mapping_revision_conflict",
                    "detail": "Mapping scope revision changed before this write.",
                    "current_revision": "c" * 64,
                },
            ),
            (
                digest_error,
                {
                    "error": "mapping_import_digest_mismatch",
                    "detail": "Mapping import digest does not match the confirmed preview.",
                    "current_revision": "c" * 64,
                    "current_import_digest": "d" * 64,
                },
            ),
        ]

        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                service = Mock()
                service.import_mapping_bundle = AsyncMock(side_effect=error)
                service.get_snapshot = AsyncMock()
                registry = Mock()
                registry.get_service.return_value = service
                with patch.object(app_main, "get_inventory_registry", return_value=registry):
                    response = asyncio.run(
                        route.endpoint(payload=payload, system_id="system-a", enclosure_id="enc-a")
                    )

                body = json.loads(response.body)
                self.assertEqual(response.status_code, 409)
                for key, value in expected.items():
                    self.assertEqual(body[key], value)
                self.assertNotIn(PRIVATE_EXCEPTION_DETAIL, response.body.decode("utf-8"))
                service.invalidate_snapshot_cache.assert_not_called()
                service.get_snapshot.assert_not_awaited()

    def test_invalid_mapping_bundles_return_generic_422_without_mutation(self) -> None:
        routes = {
            getattr(route, "path", ""): route
            for route in app_main.app.routes
        }
        bundle = MappingBundle(
            mappings=[
                ManualMapping(slot=2, enclosure_id="source-a", serial="FIRST"),
                ManualMapping(slot=2, enclosure_id="source-b", serial="SECOND"),
            ]
        )
        confirmation = MappingImportConfirmation(
            bundle=bundle,
            expected_revision="a" * 64,
            import_digest="b" * 64,
            confirmed=True,
        )
        cases = [
            (routes["/api/mappings/import/preview"], {"payload": bundle}),
            (routes["/api/mappings/import"], {"payload": confirmation}),
        ]

        for route, arguments in cases:
            with self.subTest(path=route.path):
                service = Mock()
                service.preview_mapping_bundle = AsyncMock(
                    side_effect=ValueError(PRIVATE_EXCEPTION_DETAIL)
                )
                service.import_mapping_bundle = AsyncMock(
                    side_effect=ValueError(PRIVATE_EXCEPTION_DETAIL)
                )
                service.get_snapshot = AsyncMock()
                registry = Mock()
                registry.get_service.return_value = service
                with patch.object(app_main, "get_inventory_registry", return_value=registry):
                    response = asyncio.run(
                        route.endpoint(
                            **arguments,
                            system_id="system-a",
                            enclosure_id="enc-a",
                        )
                    )

                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    json.loads(response.body)["detail"],
                    "Mapping bundle is invalid.",
                )
                self.assertNotIn(PRIVATE_EXCEPTION_DETAIL, response.body.decode("utf-8"))
                service.invalidate_snapshot_cache.assert_not_called()
                service.get_snapshot.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
