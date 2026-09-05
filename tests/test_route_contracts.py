from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from admin_service import main as admin_main
from app import main as app_main
from app.models.domain import SystemBackupExportRequest


APP_ROUTE_MATRIX = [
    ("/openapi.json", ("GET", "HEAD"), "openapi", None, None),
    ("/static", (), "static", None, None),
    ("/metrics", ("GET",), "prometheus_metrics_endpoint", "starlette.responses.JSONResponse", None),
    ("/", ("GET",), "index", "starlette.responses.HTMLResponse", None),
    ("/sas-fabric", ("GET",), "sas_fabric_view", "starlette.responses.HTMLResponse", None),
    ("/api/inventory", ("GET",), "get_inventory", "starlette.responses.JSONResponse", "app.models.domain.InventorySnapshot"),
    ("/api/sas-fabric", ("GET",), "get_sas_fabric", "starlette.responses.JSONResponse", "app.models.domain.SasFabricSnapshot"),
    ("/api/sas-fabric/aliases", ("POST",), "save_sas_fabric_alias", "starlette.responses.JSONResponse", None),
    ("/api/storage-views", ("GET",), "get_storage_views", "starlette.responses.JSONResponse", "app.models.domain.StorageViewRuntimePayload"),
    ("/api/slots/{slot}/led", ("POST",), "set_slot_led", "starlette.responses.JSONResponse", None),
    ("/api/system-locator", ("GET",), "get_system_locator", "starlette.responses.JSONResponse", "app.models.domain.SystemLocatorStatusView"),
    ("/api/system-locator", ("POST",), "set_system_locator", "starlette.responses.JSONResponse", "app.models.domain.SystemLocatorStatusView"),
    ("/api/slots/{slot}/mapping", ("POST",), "save_mapping", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/mapping", ("DELETE",), "clear_mapping", "starlette.responses.JSONResponse", None),
    ("/api/mappings/export", ("GET",), "export_mappings", "starlette.responses.JSONResponse", "app.models.domain.MappingBundle"),
    ("/api/mappings/import/preview", ("POST",), "preview_mapping_import", "starlette.responses.JSONResponse", None),
    ("/api/mappings/import", ("POST",), "import_mappings", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/smart", ("GET",), "get_slot_smart_summary", "starlette.responses.JSONResponse", "app.models.domain.SmartSummaryView"),
    ("/api/storage-views/{view_id}/slots/{slot_index}/smart", ("GET",), "get_storage_view_slot_smart_summary", "starlette.responses.JSONResponse", "app.models.domain.SmartSummaryView"),
    ("/api/storage-views/{view_id}/slots/{slot_index}/history", ("GET",), "get_storage_view_slot_history", "starlette.responses.JSONResponse", None),
    ("/api/slots/smart-batch", ("POST",), "get_slot_smart_summaries", "starlette.responses.JSONResponse", "app.models.domain.SmartBatchResponse"),
    ("/api/history/status", ("GET",), "get_history_status", "starlette.responses.JSONResponse", None),
    ("/api/slots/{slot}/history", ("GET",), "get_slot_history", "starlette.responses.JSONResponse", None),
    ("/api/history/scope", ("GET",), "get_history_scope", "starlette.responses.JSONResponse", None),
    ("/api/storage-views/{view_id}/history", ("GET",), "get_storage_view_history", "starlette.responses.JSONResponse", None),
    ("/api/export/enclosure-snapshot", ("POST",), "export_enclosure_snapshot", "starlette.responses.JSONResponse", None),
    ("/api/export/enclosure-snapshot/estimate", ("POST",), "estimate_enclosure_snapshot", "starlette.responses.JSONResponse", None),
    ("/livez", ("GET",), "livez", "starlette.responses.JSONResponse", None),
    ("/healthz", ("GET",), "healthz", "starlette.responses.JSONResponse", None),
]

ADMIN_ROUTE_MATRIX = [
    ("/openapi.json", ("GET", "HEAD"), "openapi", None, None),
    ("/static", (), "static", None, None),
    ("/metrics", ("GET",), "prometheus_metrics_endpoint", "starlette.responses.JSONResponse", None),
    ("/", ("GET",), "index", "starlette.responses.HTMLResponse", None),
    ("/api/admin/state", ("GET",), "get_admin_state", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime", ("GET",), "get_admin_runtime", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime-behavior", ("POST",), "update_runtime_behavior", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/stop", ("POST",), "stop_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/start", ("POST",), "start_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/runtime/containers/{container_key}/restart", ("POST",), "restart_container", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/export", ("POST",), "export_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/debug/export", ("POST",), "export_debug_bundle", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/inspect", ("POST",), "inspect_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/backup/import", ("POST",), "import_backup", "starlette.responses.JSONResponse", None),
    ("/api/admin/esxi-host-prep/upload", ("POST",), "upload_esxi_host_prep_package", "starlette.responses.JSONResponse", None),
    ("/api/admin/esxi-host-prep/install", ("POST",), "install_esxi_host_prep_package", "starlette.responses.JSONResponse", None),
    ("/api/admin/ssh-keys", ("GET",), "list_ssh_keys", "starlette.responses.JSONResponse", None),
    ("/api/admin/ssh-keys/generate", ("POST",), "generate_ssh_key", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/inspect", ("POST",), "inspect_tls_certificate", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/import", ("POST",), "import_tls_bundle", "starlette.responses.JSONResponse", None),
    ("/api/admin/tls/trust-remote", ("POST",), "trust_remote_tls_certificate", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/quantastor-nodes", ("POST",), "discover_quantastor_nodes", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup", ("POST",), "create_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/demo", ("POST",), "create_demo_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/{system_id}", ("DELETE",), "delete_system", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/purge-orphaned", ("POST",), "purge_orphaned_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/orphaned", ("GET",), "list_orphaned_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/history/adopt-removed-system", ("POST",), "adopt_removed_system_history", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/bootstrap", ("POST",), "bootstrap_service_account", "starlette.responses.JSONResponse", None),
    ("/api/admin/system-setup/sudoers-preview", ("POST",), "preview_sudoers_file", "starlette.responses.JSONResponse", None),
    ("/api/admin/storage-views/candidates", ("GET",), "list_storage_view_candidates", "starlette.responses.JSONResponse", None),
    ("/api/admin/storage-views/live-enclosures", ("GET",), "list_storage_view_live_enclosures", "starlette.responses.JSONResponse", None),
    ("/api/admin/profiles", ("POST",), "save_profile", "starlette.responses.JSONResponse", None),
    ("/api/admin/profiles/{profile_id}", ("DELETE",), "delete_profile", "starlette.responses.JSONResponse", None),
    ("/healthz", ("GET",), "healthz", "starlette.responses.JSONResponse", None),
    ("/livez", ("GET",), "livez", "starlette.responses.JSONResponse", None),
]


def _symbol(value: object) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    placeholder = getattr(value, "value", None)
    if placeholder is not None and placeholder is not value:
        return _symbol(placeholder)
    return type(value).__name__


def _route_matrix(application) -> list[tuple[object, ...]]:
    return [
        (
            getattr(route, "path", None),
            tuple(sorted(getattr(route, "methods", None) or [])),
            getattr(route, "name", None),
            _symbol(route.response_class) if isinstance(route, APIRoute) else None,
            _symbol(route.response_model) if isinstance(route, APIRoute) else None,
        )
        for route in application.routes
    ]


def _create_app_node(module) -> ast.FunctionDef:
    source_path = Path(inspect.getsourcefile(module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )


class RouteContractTests(unittest.TestCase):
    def test_main_route_matrix_is_frozen(self) -> None:
        self.assertEqual(_route_matrix(app_main.create_app()), APP_ROUTE_MATRIX)

    def test_admin_route_matrix_is_frozen(self) -> None:
        self.assertEqual(_route_matrix(admin_main.create_app()), ADMIN_ROUTE_MATRIX)

    def test_critical_openapi_request_and_response_contracts_are_frozen(self) -> None:
        app_schema = app_main.create_app().openapi()
        admin_schema = admin_main.create_app().openapi()

        mapping_path = app_schema["paths"]["/api/slots/{slot}/mapping"]
        self.assertEqual(set(mapping_path), {"post", "delete"})
        self.assertEqual(mapping_path["post"]["operationId"], "save_mapping_api_slots__slot__mapping_post")
        self.assertEqual(mapping_path["delete"]["operationId"], "clear_mapping_api_slots__slot__mapping_delete")
        self.assertEqual(set(mapping_path["post"]["responses"]), {"200", "422"})
        self.assertEqual(set(mapping_path["delete"]["responses"]), {"200", "422"})

        import_operation = app_schema["paths"]["/api/mappings/import"]["post"]
        request_schema = import_operation["requestBody"]["content"]["application/json"]["schema"]
        component = app_schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[-1]]
        self.assertEqual(
            set(component["required"]),
            {"bundle", "expected_revision", "import_digest", "confirmed"},
        )

        export_operation = admin_schema["paths"]["/api/admin/backup/export"]["post"]
        self.assertEqual(export_operation["operationId"], "export_backup_api_admin_backup_export_post")
        self.assertEqual(set(export_operation["responses"]), {"200", "422"})
        import_admin = admin_schema["paths"]["/api/admin/backup/import"]["post"]
        self.assertEqual(import_admin["operationId"], "import_backup_api_admin_backup_import_post")
        self.assertEqual(
            [parameter["name"] for parameter in import_admin["parameters"]],
            ["stop_services", "restart_services"],
        )

    def test_sas_fabric_endpoint_name_supports_real_url_lookup(self) -> None:
        application = app_main.create_app()
        self.assertEqual(str(application.url_path_for("sas_fabric_view")), "/sas-fabric")

    def test_main_create_app_delegates_routes_to_apirouter(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("app.routes"))
        create_app = _create_app_node(app_main)
        nested = [node for node in create_app.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        route_decorators = [
            decorator
            for node in ast.walk(create_app)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
        ]
        self.assertEqual(route_decorators, [])
        end_lineno = create_app.end_lineno
        if end_lineno is None:
            self.fail("create_app has no end line")
        self.assertLessEqual(end_lineno - create_app.lineno + 1, 110)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "include_router_preserving_route_objects"
                for node in ast.walk(create_app)
            )
        )
        self.assertEqual(
            {node.name for node in nested},
            {"lifespan", "http_exception_handler", "unhandled_exception_handler"},
        )

    def test_admin_create_app_delegates_routes_to_apirouter(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("admin_service.routes"))
        create_app = _create_app_node(admin_main)
        route_decorators = [
            decorator
            for node in ast.walk(create_app)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"get", "post", "put", "patch", "delete"}
        ]
        self.assertEqual(route_decorators, [])
        end_lineno = create_app.end_lineno
        if end_lineno is None:
            self.fail("create_app has no end line")
        self.assertLessEqual(end_lineno - create_app.lineno + 1, 120)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "include_router"
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "include_router_preserving_route_objects"
                    )
                )
                for node in ast.walk(create_app)
            )
        )

    def test_route_modules_do_not_import_their_main_compatibility_facades(self) -> None:
        for relative_path, forbidden_module in (
            ("app/routes.py", "app.main"),
            ("admin_service/routes.py", "admin_service.main"),
        ):
            with self.subTest(route_module=relative_path):
                tree = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
                imported_modules = {
                    node.module
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                }
                self.assertNotIn(forbidden_module, imported_modules)

    def test_main_routes_deduplicate_contiguous_service_perf_preambles(self) -> None:
        route_path = Path(__file__).resolve().parents[1] / "app" / "routes.py"
        tree = ast.parse(route_path.read_text(encoding="utf-8"))
        build_router = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_router"
        )
        duplicate_endpoints: list[str] = []
        for endpoint in build_router.body:
            if (
                not isinstance(endpoint, (ast.FunctionDef, ast.AsyncFunctionDef))
                or endpoint.name == "route_service"
            ):
                continue
            statements = endpoint.body
            for index in range(len(statements) - 2):
                first, second, third = statements[index : index + 3]
                first_call = first.value if isinstance(first, ast.Assign) else None
                second_call = second.value if isinstance(second, ast.Assign) else None
                third_call = third.value if isinstance(third, ast.Expr) else None
                if not (
                    isinstance(first_call, ast.Call)
                    and isinstance(first_call.func, ast.Name)
                    and first_call.func.id == "get_inventory_registry"
                    and isinstance(second_call, ast.Call)
                    and isinstance(second_call.func, ast.Attribute)
                    and second_call.func.attr == "get_service"
                    and isinstance(third_call, ast.Call)
                    and isinstance(third_call.func, ast.Name)
                    and third_call.func.id == "add_perf_metadata"
                ):
                    continue
                duplicate_endpoints.append(endpoint.name)
        self.assertEqual(duplicate_endpoints, [])

    def test_main_handler_resolves_main_module_patch_after_app_creation(self) -> None:
        application = app_main.create_app()
        route = cast(
            APIRoute,
            next(route for route in application.routes if getattr(route, "path", None) == "/healthz"),
        )
        service = MagicMock()
        service.peek_cached_snapshot.return_value = None
        registry = MagicMock()
        registry.get_service.return_value = service

        with patch.object(app_main, "get_inventory_registry", return_value=registry) as getter:
            response = asyncio.run(route.endpoint())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["cache_state"], "empty")
        getter.assert_called_once_with()

    def test_admin_handler_resolves_temporary_file_response_patch_after_app_creation(self) -> None:
        application = admin_main.create_app()
        route = cast(
            APIRoute,
            next(
                route
                for route in application.routes
                if getattr(route, "path", None) == "/api/admin/backup/export"
            ),
        )
        artifact = SimpleNamespace(
            path=Path("/tmp/synthetic-backup.zip"),
            filename="synthetic-backup.zip",
            media_type="application/zip",
            manifest={"packaging": "zip"},
            cleanup=MagicMock(),
        )
        maintenance = SimpleNamespace(stopped_containers=[], restarted_containers=[], restart_failures={})
        service = MagicMock()
        service.export_bundle.return_value = (artifact, maintenance)
        sentinel = object()

        with (
            patch.object(admin_main, "get_maintenance_service", return_value=service),
            patch.object(admin_main, "TemporaryFileResponse", return_value=sentinel) as response_class,
        ):
            response = asyncio.run(
                route.endpoint(
                    SystemBackupExportRequest(
                        packaging="zip",
                        encrypt=True,
                        passphrase="test-passphrase",
                    ),
                    stop_services=False,
                    restart_services=True,
                )
            )

        self.assertIs(response, sentinel)
        response_class.assert_called_once()
        artifact.cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
