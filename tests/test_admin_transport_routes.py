"""The admin SSH key and TLS trust routes live in ``admin_service.transport_routes``.

``admin_service.routes.build_router`` adds them to its router at the place they
have always had. These tests pin that seam: the new module imports on its own,
every name stays reachable on ``admin_service.routes``, and a replacement made
on ``admin_service.routes`` (the way the existing tests make it) still reaches
the moved endpoints when a request runs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
from admin_service import main as admin_main
from admin_service import routes as admin_routes
from admin_service.config import get_admin_settings
from app.models.domain import SSHKeyGenerateRequest, TLSCertificateInspectRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NEW_MODULE = "admin_service.transport_routes"
MOVED_ENDPOINTS = (
    "list_ssh_keys",
    "generate_ssh_key",
    "inspect_tls_certificate",
    "import_tls_bundle",
    "trust_remote_tls_certificate",
    "saved_tls_bundle_status",
)


def _router_endpoints() -> list:
    return [
        route.endpoint
        for route in admin_routes.build_router(get_admin_settings()).routes
        if isinstance(route, APIRoute)
    ]


def _app_endpoint(name: str):
    application = admin_main.create_app()
    return next(
        route.endpoint
        for route in application.routes
        if isinstance(route, APIRoute) and route.endpoint.__name__ == name
    )


def _saved_settings(*, verify_ssl: bool = True, bundle_path: str = "/synthetic/tls/core.pem"):
    truenas = SimpleNamespace(
        host="https://truenas.example.test",
        verify_ssl=verify_ssl,
        tls_ca_bundle_path=bundle_path,
        tls_server_name=None,
        timeout_seconds=10,
    )
    return SimpleNamespace(
        config_file="/synthetic/config.yaml",
        systems=[SimpleNamespace(id="synthetic-core", truenas=truenas)],
    )


class AdminTransportRouteSeamTests(unittest.TestCase):
    def test_transport_routes_module_imports_without_the_routes_module(self) -> None:
        probe = (
            "import sys\n"
            f"import {NEW_MODULE} as module\n"
            "assert callable(module.add_transport_routes)\n"
            "print(sorted(name for name in ('admin_service.routes', 'admin_service.main') if name in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_moved_endpoints_are_registered_from_their_module_in_place(self) -> None:
        endpoints = _router_endpoints()
        names = [endpoint.__name__ for endpoint in endpoints]
        start = names.index("install_esxi_host_prep_package") + 1
        self.assertEqual(tuple(names[start : start + len(MOVED_ENDPOINTS)]), MOVED_ENDPOINTS)
        self.assertEqual(names[start + len(MOVED_ENDPOINTS)], "discover_quantastor_nodes")
        moved = [endpoint for endpoint in endpoints if endpoint.__name__ in MOVED_ENDPOINTS]
        self.assertEqual([endpoint.__module__ for endpoint in moved], [NEW_MODULE] * len(MOVED_ENDPOINTS))
        others = {endpoint.__module__ for endpoint in endpoints if endpoint.__name__ not in MOVED_ENDPOINTS}
        self.assertEqual(others, {"admin_service.routes"})

    def test_admin_endpoint_files_are_the_files_the_route_sweeps_list(self) -> None:
        # tests/test_route_contracts.py names the admin route files by path in its
        # no-main-import and blanket-suppression sweeps. An endpoint defined in any
        # other file would silently leave them, so pin the set here.
        files = {
            Path(inspect.getsourcefile(inspect.unwrap(endpoint)) or "").resolve().relative_to(REPOSITORY_ROOT).as_posix()
            for endpoint in _router_endpoints()
        }
        self.assertEqual(files, {"admin_service/routes.py", "admin_service/transport_routes.py"})

    def test_routes_module_keeps_the_names_the_moved_endpoints_read(self) -> None:
        import importlib

        transport_routes = importlib.import_module(NEW_MODULE)
        from admin_service import route_support
        from admin_service.services.tls_trust import TLSTrustStoreService
        from app.models import domain
        from app.services.ssh_key_manager import SSHKeyManager
        from app.services.tls_context import host_uses_tls

        self.assertIs(admin_routes.add_transport_routes, transport_routes.add_transport_routes)
        self.assertIs(admin_routes.reload_app_settings, route_support.reload_app_settings)
        self.assertIs(admin_routes.SSHKeyManager, SSHKeyManager)
        self.assertIs(admin_routes.TLSTrustStoreService, TLSTrustStoreService)
        self.assertIs(admin_routes.host_uses_tls, host_uses_tls)
        for name in (
            "SSHKeyGenerateRequest",
            "TLSCertificateImportRequest",
            "TLSCertificateInspectRequest",
            "TLSRemoteCertificateTrustRequest",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(admin_routes, name), getattr(domain, name))
                self.assertIs(getattr(transport_routes, name), getattr(domain, name))
        self.assertTrue(callable(admin_routes.build_router))


class AdminTransportRouteReplacementTests(unittest.TestCase):
    """A replacement made on ``admin_service.routes`` after app creation reaches the moved code."""

    def _endpoint(self, name: str):
        endpoint = _app_endpoint(name)
        self.assertEqual(endpoint.__module__, NEW_MODULE)
        return endpoint

    def test_replaced_reload_app_settings_reaches_the_moved_endpoint(self) -> None:
        endpoint = self._endpoint("saved_tls_bundle_status")
        settings = SimpleNamespace(config_file="/synthetic/config.yaml", systems=[])
        with patch.object(admin_routes, "reload_app_settings", return_value=settings) as reload_settings:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(endpoint(system_id="missing"))
        reload_settings.assert_called_once_with()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, "That system is not in the saved config.")

    def test_replaced_ssh_key_manager_reaches_the_moved_endpoints(self) -> None:
        list_keys = self._endpoint("list_ssh_keys")
        generate = self._endpoint("generate_ssh_key")
        settings = SimpleNamespace(config_file="/synthetic/config.yaml")
        manager = MagicMock()
        manager.list_keys.return_value = [{"name": "id_synthetic"}]
        manager.generate_keypair.side_effect = ValueError("synthetic refusal")
        with (
            patch.object(admin_routes, "reload_app_settings", return_value=settings),
            patch.object(admin_routes, "SSHKeyManager", return_value=manager) as manager_class,
        ):
            response = asyncio.run(list_keys())
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(generate(SSHKeyGenerateRequest(name="id_synthetic")))
        self.assertEqual(json.loads(response.body), {"ok": True, "keys": [{"name": "id_synthetic"}]})
        self.assertEqual(manager_class.call_args_list[0].args, ("/synthetic/config.yaml",))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "synthetic refusal")

    def test_replaced_trust_store_service_reaches_the_moved_endpoint(self) -> None:
        endpoint = self._endpoint("inspect_tls_certificate")
        settings = SimpleNamespace(config_file="/synthetic/config.yaml")
        service = MagicMock()
        service.inspect_remote_certificate.return_value = {"host": "https://truenas.example.test"}
        with (
            patch.object(admin_routes, "reload_app_settings", return_value=settings),
            patch.object(admin_routes, "TLSTrustStoreService", return_value=service) as service_class,
        ):
            response = asyncio.run(endpoint(TLSCertificateInspectRequest(host="https://truenas.example.test")))
        service_class.assert_called_once_with("/synthetic/config.yaml")
        service.inspect_remote_certificate.assert_called_once_with(
            "https://truenas.example.test", 10, tls_server_name=None
        )
        self.assertEqual(
            json.loads(response.body),
            {"ok": True, "inspection": {"host": "https://truenas.example.test"}},
        )

    def test_replaced_host_uses_tls_reaches_the_moved_endpoint(self) -> None:
        endpoint = self._endpoint("saved_tls_bundle_status")
        service_class = MagicMock()
        with (
            patch.object(admin_routes, "reload_app_settings", return_value=_saved_settings()),
            patch.object(admin_routes, "host_uses_tls", return_value=False) as uses_tls,
            patch.object(admin_routes, "TLSTrustStoreService", service_class),
        ):
            response = asyncio.run(endpoint(system_id="synthetic-core"))
        uses_tls.assert_called_once_with("https://truenas.example.test")
        service_class.assert_not_called()
        self.assertEqual(
            json.loads(response.body),
            {"ok": True, "system_id": "synthetic-core", "validation": None},
        )

    def test_saved_status_keeps_its_fixed_failure_text(self) -> None:
        endpoint = self._endpoint("saved_tls_bundle_status")
        service = MagicMock()
        service.validate_bundle_for_host.side_effect = ValueError("not echoed")
        with (
            patch.object(admin_routes, "reload_app_settings", return_value=_saved_settings()),
            patch.object(admin_routes, "host_uses_tls", return_value=True),
            patch.object(admin_routes, "TLSTrustStoreService", return_value=service),
        ):
            response = asyncio.run(endpoint(system_id="synthetic-core"))
        self.assertEqual(
            json.loads(response.body)["validation"],
            {
                "validated": False,
                "host": "https://truenas.example.test",
                "bundle_path": "/synthetic/tls/core.pem",
                "detail": "The saved bundle could not be checked: the bundle file could not be found.",
            },
        )

    def test_saved_status_query_bound_is_unchanged(self) -> None:
        operation = admin_main.create_app().openapi()["paths"]["/api/admin/tls/saved-status"]["get"]
        self.assertEqual(
            operation["parameters"],
            [
                {
                    "name": "system_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 128, "title": "System Id"},
                }
            ],
        )
        self.assertEqual(operation["operationId"], "saved_tls_bundle_status_api_admin_tls_saved_status_get")


if __name__ == "__main__":
    unittest.main()
