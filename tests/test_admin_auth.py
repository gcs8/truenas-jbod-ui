from __future__ import annotations

import asyncio
import base64
import importlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pydantic import SecretStr, ValidationError
from fastapi import HTTPException
from fastapi.routing import APIRoute

# Must precede admin_service.main, which builds its app at import time.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN
from admin_service.config import AdminSettings, get_admin_settings
from admin_service.main import (
    _basic_auth_matches,
    create_app,
    validate_admin_export_policy,
)
from app.models.domain import DebugBundleExportRequest, SystemBackupExportRequest


MARKER_ALPHA = "correct horse"
MARKER_BRAVO = "wrong"
MARKER_CHARLIE = "sensitive-value"


async def invoke_asgi(
    app,
    path: str,
    *,
    authorization: str | None = None,
    method: str = "GET",
    origin: str | None = None,
    referer: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [(b"host", b"admin.example.test")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if referer is not None:
        headers.append((b"referer", referer.encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("admin.example.test", 8082),
    }
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    return int(start["status"]), response_headers, body


def basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class AdminAuthenticationTests(unittest.TestCase):
    def test_clean_backup_targets_rejects_admin_sidecar(self) -> None:
        with self.assertRaisesRegex(ValidationError, "clean_backup_targets"):
            AdminSettings(clean_backup_targets=["ui", "admin"])

    def test_basic_auth_mode_requires_both_credentials(self) -> None:
        with self.assertRaises(ValidationError):
            AdminSettings(auth_mode="basic")
        with self.assertRaises(ValidationError):
            AdminSettings(auth_mode="basic", auth_username="operator")

    def test_auth_validation_errors_do_not_echo_supplied_secret(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            AdminSettings.model_validate(
                {
                    "auth_mode": "basic",
                    "auth_username": "",
                    "auth_password": MARKER_CHARLIE,
                }
            )

        self.assertNotIn(MARKER_CHARLIE, str(raised.exception))

    def test_auth_credentials_remain_exact_strings_when_loaded_from_environment(self) -> None:
        get_admin_settings.cache_clear()
        try:
            with patch.dict(
                "os.environ",
                {
                    "ADMIN_AUTH_MODE": "basic",
                    "ADMIN_AUTH_USERNAME": "123",
                    "ADMIN_AUTH_PASSWORD": "true",
                },
                clear=True,
            ):
                settings = get_admin_settings()
            self.assertEqual(settings.auth_username, "123")
            assert settings.auth_password is not None
            self.assertEqual(settings.auth_password.get_secret_value(), "true")
        finally:
            get_admin_settings.cache_clear()

    def test_auth_password_file_overrides_direct_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            password_path = temp_path / "admin-password"
            password_path.write_bytes(b" file-pass \r\n")
            password_path.chmod(0o600)
            environment = {
                "ADMIN_AUTH_MODE": "basic",
                "ADMIN_AUTH_USERNAME": "direct-user",
                "ADMIN_AUTH_PASSWORD": "direct-password-must-not-win",
                "ADMIN_AUTH_PASSWORD_FILE": str(password_path),
            }
            get_admin_settings.cache_clear()
            try:
                with patch.dict("os.environ", environment, clear=True):
                    settings = get_admin_settings()
                self.assertEqual(settings.auth_username, "direct-user")
                assert settings.auth_password is not None
                self.assertEqual(settings.auth_password.get_secret_value(), " file-pass ")
            finally:
                get_admin_settings.cache_clear()

    def test_basic_auth_comparison_accepts_only_exact_credentials(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
        )

        self.assertTrue(
            _basic_auth_matches(
                basic_header("operator", MARKER_ALPHA),
                settings,
            )
        )
        self.assertFalse(_basic_auth_matches(None, settings))
        self.assertFalse(_basic_auth_matches("Bearer wrong", settings))
        self.assertFalse(
            _basic_auth_matches(
                basic_header("operator", MARKER_BRAVO),
                settings,
            )
        )

    def test_basic_auth_accepts_exact_utf8_credentials_without_server_error(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="opérator",
            auth_password=SecretStr("pässphrase"),
        )

        self.assertTrue(
            _basic_auth_matches(
                basic_header("opérator", "pässphrase"),
                settings,
            )
        )

    def test_basic_auth_protects_admin_routes_but_not_health_or_metrics(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        status, headers, _body = asyncio.run(invoke_asgi(app, "/missing"))
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("www-authenticate"), 'Basic realm="truenas-jbod-admin"')

        status, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/missing",
                authorization=basic_header("operator", MARKER_ALPHA),
            )
        )
        self.assertEqual(status, 404)

        health_status, _headers, _body = asyncio.run(invoke_asgi(app, "/livez"))
        metrics_status, _headers, _body = asyncio.run(invoke_asgi(app, "/metrics"))
        self.assertEqual(health_status, 200)
        self.assertEqual(metrics_status, 200)

    def test_basic_auth_wraps_every_privileged_admin_router_endpoint(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        public_paths = {"/healthz", "/livez", "/metrics"}
        privileged_routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute) and route.path not in public_paths
        ]
        self.assertGreater(len(privileged_routes), 30)
        for route in privileged_routes:
            method = sorted((route.methods or {"GET"}) - {"HEAD", "OPTIONS"})[0]
            path = re.sub(r"\{[^}]+\}", "synthetic", route.path)
            with self.subTest(method=method, path=route.path):
                status, headers, _body = asyncio.run(
                    invoke_asgi(app, path, method=method)
                )
                self.assertEqual(status, 401)
                self.assertEqual(
                    headers.get("www-authenticate"),
                    'Basic realm="truenas-jbod-admin"',
                )

        static_status, _headers, _body = asyncio.run(
            invoke_asgi(app, "/static/missing.css")
        )
        self.assertEqual(static_status, 401)

    def test_origin_gate_wraps_every_admin_router_mutation(self) -> None:
        settings = AdminSettings(
            auth_mode="network",
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        mutation_methods = {"POST", "PUT", "PATCH", "DELETE"}
        mutation_routes = [
            (route, method)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in sorted((route.methods or set()) & mutation_methods)
        ]
        self.assertGreater(len(mutation_routes), 20)
        for route, method in mutation_routes:
            path = re.sub(r"\{[^}]+\}", "synthetic", route.path)
            with self.subTest(method=method, path=route.path):
                status, _headers, body = asyncio.run(
                    invoke_asgi(
                        app,
                        path,
                        method=method,
                        origin="https://attacker.example",
                    )
                )
                self.assertEqual(status, 403)
                self.assertEqual(
                    body,
                    b'{"detail":"Cross-origin admin mutation rejected."}',
                )

    def test_network_boundary_mode_preserves_remote_unauthenticated_contract(self) -> None:
        settings = AdminSettings(
            auth_mode="network",
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        status, _headers, _body = asyncio.run(invoke_asgi(app, "/missing"))
        self.assertEqual(status, 404)

    def test_admin_test_env_replaces_a_blank_or_malformed_inherited_origin(self) -> None:
        # A shell that sourced .env inherits the shipped empty `ADMIN_PUBLIC_ORIGIN=` line as a
        # present-but-blank variable; the helper must still supply the synthetic origin.
        import tests.admin_test_env as admin_test_env

        try:
            for inherited in ("", "   ", "not-an-origin", "https://admin.example.test/path"):
                with self.subTest(inherited=inherited):
                    with patch.dict("os.environ", {"ADMIN_PUBLIC_ORIGIN": inherited}):
                        importlib.reload(admin_test_env)
                        self.assertEqual(os.environ["ADMIN_PUBLIC_ORIGIN"], ADMIN_TEST_PUBLIC_ORIGIN)
                        self.assertEqual(get_admin_settings().public_origin, ADMIN_TEST_PUBLIC_ORIGIN)
            with patch.dict("os.environ", {"ADMIN_PUBLIC_ORIGIN": "https://inherited.example.test"}):
                importlib.reload(admin_test_env)
                self.assertEqual(os.environ["ADMIN_PUBLIC_ORIGIN"], "https://inherited.example.test")
                self.assertEqual(get_admin_settings().public_origin, "https://inherited.example.test")
        finally:
            importlib.reload(admin_test_env)
            get_admin_settings.cache_clear()

    def test_create_app_refuses_to_start_without_a_valid_public_origin(self) -> None:
        for public_origin in (
            None,
            "",
            "   ",
            "not-an-origin",
            "ftp://admin.example.test",
            "https://user@admin.example.test",
            "https://admin.example.test/path",
            "https://admin.example.test?query=1",
            "https://admin.example.test#fragment",
        ):
            with self.subTest(public_origin=public_origin):
                settings = AdminSettings(
                    auth_mode="network",
                    public_origin=public_origin,
                    auto_stop_seconds=0,
                )
                with patch("admin_service.main.get_admin_settings", return_value=settings):
                    with self.assertRaisesRegex(ValueError, "ADMIN_PUBLIC_ORIGIN"):
                        create_app()

    def test_configured_public_origin_gates_browser_mutations_on_a_real_route(self) -> None:
        settings = AdminSettings(
            auth_mode="network",
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        same_origin_status, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/api/admin/system-setup/sudoers-preview",
                method="POST",
                origin="http://admin.example.test",
            )
        )
        foreign_origin_status, _headers, foreign_body = asyncio.run(
            invoke_asgi(
                app,
                "/api/admin/system-setup/sudoers-preview",
                method="POST",
                origin="http://admin.example.test:8082",
            )
        )

        # The empty request body reaches the handler and fails validation instead of the origin gate.
        self.assertEqual(same_origin_status, 422)
        self.assertEqual(foreign_origin_status, 403)
        self.assertEqual(foreign_body, b'{"detail":"Cross-origin admin mutation rejected."}')

    def test_browser_mutations_require_same_origin_in_both_auth_modes(self) -> None:
        for settings, authorization in (
            (
                AdminSettings(
                    auth_mode="network",
                    public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
                    auto_stop_seconds=0,
                ),
                None,
            ),
            (
                AdminSettings(
                    auth_mode="basic",
                    auth_username="operator",
                    auth_password=SecretStr(MARKER_ALPHA),
                    public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
                    auto_stop_seconds=0,
                ),
                basic_header("operator", MARKER_ALPHA),
            ),
        ):
            with self.subTest(auth_mode=settings.auth_mode):
                with patch("admin_service.main.get_admin_settings", return_value=settings):
                    app = create_app()
                cross_origin_status, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/missing",
                        method="POST",
                        authorization=authorization,
                        origin="https://attacker.example",
                    )
                )
                same_origin_status, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/missing",
                        method="POST",
                        authorization=authorization,
                        origin="http://admin.example.test",
                    )
                )
                cross_referer_status, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/missing",
                        method="POST",
                        authorization=authorization,
                        referer="https://attacker.example/form",
                    )
                )
                cli_status, _headers, _body = asyncio.run(
                    invoke_asgi(
                        app,
                        "/missing",
                        method="POST",
                        authorization=authorization,
                    )
                )
                self.assertEqual(cross_origin_status, 403)
                self.assertEqual(cross_referer_status, 403)
                self.assertEqual(same_origin_status, 404)
                self.assertEqual(cli_status, 404)

    def test_browser_mutations_do_not_trust_a_host_derived_origin(self) -> None:
        # The configured origin differs from the request's Host header; an Origin that
        # merely matches Host must still be rejected.
        settings = AdminSettings(
            auth_mode="network",
            public_origin="https://admin.example.test:9443",
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        status, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/missing",
                method="POST",
                origin="http://admin.example.test",
            )
        )

        self.assertEqual(status, 403)

    def test_configured_public_origin_is_accepted_behind_reverse_proxy(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
            public_origin="https://admin.example.test",
            auto_stop_seconds=0,
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        status, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/missing",
                method="POST",
                authorization=basic_header("operator", MARKER_ALPHA),
                origin="https://admin.example.test",
            )
        )
        self.assertEqual(status, 404)

    def test_metrics_path_cannot_overlap_privileged_admin_routes(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with (
            patch("admin_service.main.get_admin_settings", return_value=settings),
            patch.dict("os.environ", {"METRICS_PATH": "/api/admin/backup/export"}),
        ):
            with self.assertRaisesRegex(ValueError, "METRICS_PATH"):
                create_app()

    def test_custom_non_privileged_metrics_path_remains_anonymous(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr(MARKER_ALPHA),
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auto_stop_seconds=0,
        )
        with (
            patch("admin_service.main.get_admin_settings", return_value=settings),
            patch.dict("os.environ", {"METRICS_PATH": "/observability/metrics"}),
        ):
            app = create_app()

        status, _headers, _body = asyncio.run(
            invoke_asgi(app, "/observability/metrics")
        )
        self.assertEqual(status, 200)

    def test_plaintext_credential_export_requires_explicit_opt_in(self) -> None:
        settings = AdminSettings()

        with self.assertRaisesRegex(ValueError, "(?i)plaintext backup export"):
            validate_admin_export_policy(
                settings,
                encrypt=False,
                scrub_secrets=False,
            )

        validate_admin_export_policy(settings, encrypt=True, scrub_secrets=False)
        validate_admin_export_policy(settings, encrypt=False, scrub_secrets=True)
        validate_admin_export_policy(
            AdminSettings(allow_plaintext_backup_export=True),
            encrypt=False,
            scrub_secrets=False,
        )

    def test_export_routes_enforce_plaintext_policy_before_maintenance(self) -> None:
        settings = AdminSettings(auto_stop_seconds=0, public_origin=ADMIN_TEST_PUBLIC_ORIGIN)
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()
        backup_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/admin/backup/export"
        )
        debug_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/admin/debug/export"
        )

        with self.assertRaises(HTTPException) as backup_error:
            asyncio.run(
                getattr(backup_route, "endpoint")(
                    SystemBackupExportRequest(),
                    stop_services=False,
                    restart_services=True,
                )
            )
        self.assertEqual(backup_error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as debug_error:
            asyncio.run(
                getattr(debug_route, "endpoint")(
                    DebugBundleExportRequest(
                        scrub_secrets=False,
                        scrub_disk_identifiers=False,
                    ),
                    stop_services=False,
                    restart_services=True,
                )
            )
        self.assertEqual(debug_error.exception.status_code, 400)

    def test_public_configuration_documents_trust_and_auth_options(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        security_doc = (root / "docs" / "ADMIN_TRUST_BOUNDARY.md").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        admin_template = (root / "admin_service" / "templates" / "index.html").read_text(encoding="utf-8")

        for marker in (
            "ADMIN_AUTH_MODE",
            "ADMIN_AUTH_USERNAME",
            "ADMIN_AUTH_PASSWORD",
            "ADMIN_ALLOW_PLAINTEXT_BACKUP_EXPORT",
        ):
            self.assertIn(marker, env_example)
            self.assertIn(marker, compose)
        self.assertIn("ADMIN_TRUST_BOUNDARY.md", readme)
        self.assertIn("trusted operators", security_doc)
        self.assertIn("firewall", security_doc.lower())
        self.assertIn("VPN", security_doc)
        self.assertIn("Basic authentication", security_doc)
        self.assertIn('id="backup-encrypt-toggle" type="checkbox" checked', admin_template)


if __name__ == "__main__":
    unittest.main()
