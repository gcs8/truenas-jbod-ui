from __future__ import annotations

import asyncio
import base64
import unittest
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import yaml
from pydantic import SecretStr

from admin_service.config import AdminSettings
from app import main as app_main
from app.config import AppConfig, Settings


MUTATION_ROUTES = (
    ("POST", "/api/sas-fabric/aliases"),
    ("POST", "/api/slots/0/led"),
    ("POST", "/api/system-locator"),
    ("POST", "/api/slots/0/mapping"),
    ("DELETE", "/api/slots/0/mapping"),
    ("POST", "/api/mappings/import"),
)
MUTATION_ROUTE_TEMPLATES = {
    ("POST", "/api/sas-fabric/aliases"),
    ("POST", "/api/slots/{slot}/led"),
    ("POST", "/api/system-locator"),
    ("POST", "/api/slots/{slot}/mapping"),
    ("DELETE", "/api/slots/{slot}/mapping"),
    ("POST", "/api/mappings/import"),
}
READ_ONLY_NON_GET_ROUTES = {
    ("POST", "/api/mappings/import/preview"),
    ("POST", "/api/slots/smart-batch"),
    ("POST", "/api/export/enclosure-snapshot"),
    ("POST", "/api/export/enclosure-snapshot/estimate"),
}


def basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


async def invoke_asgi(
    app,
    target: str,
    *,
    method: str = "GET",
    authorization: str | None = None,
    origin: str | None = None,
    additional_origins: tuple[str, ...] = (),
    referer: str | None = None,
    body: bytes = b"{}",
) -> tuple[int, dict[str, str], bytes]:
    messages: list[dict[str, Any]] = []
    request_sent = False
    path, separator, query = target.partition("?")

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [
        (b"host", b"ui.example.test"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    for additional_origin in additional_origins:
        headers.append((b"origin", additional_origin.encode("ascii")))
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
        "query_string": query.encode("ascii") if separator else b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("ui.example.test", 8080),
    }
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start.get("headers", [])
    }
    return int(start["status"]), response_headers, response_body


class ReadUIAuthorizationTests(unittest.TestCase):
    def make_app(
        self,
        *,
        auth_mode: Literal["network", "basic"],
        public_origin: str | None = None,
    ):
        settings = Settings(app=AppConfig(public_origin=public_origin))
        auth_settings = AdminSettings(
            auth_mode=auth_mode,
            auth_username="operator" if auth_mode == "basic" else None,
            auth_password=SecretStr("synthetic-passphrase") if auth_mode == "basic" else None,
            auto_stop_seconds=0,
        )
        with patch.object(app_main, "get_settings", return_value=settings):
            with patch.object(
                app_main,
                "get_admin_settings",
                return_value=auth_settings,
                create=True,
            ):
                return app_main.create_app()

    def test_network_mode_keeps_reads_available_but_denies_every_mutation(self) -> None:
        app = self.make_app(auth_mode="network")

        read_status, _headers, _body = asyncio.run(invoke_asgi(app, "/missing"))
        read_only_post, _headers, _body = asyncio.run(
            invoke_asgi(app, "/api/mappings/import/preview", method="POST")
        )
        mutation_statuses = [
            asyncio.run(invoke_asgi(app, path, method=method))[0]
            for method, path in MUTATION_ROUTES
        ]

        self.assertEqual(read_status, 404)
        self.assertEqual(read_only_post, 200)
        self.assertEqual(mutation_statuses, [403] * len(MUTATION_ROUTES))

    def test_basic_mode_keeps_reads_anonymous_and_requires_same_origin_for_mutations(self) -> None:
        app = self.make_app(
            auth_mode="basic",
            public_origin="http://ui.example.test:8080",
        )
        authorization = basic_header("operator", "synthetic-passphrase")

        anonymous_read, read_headers, _body = asyncio.run(invoke_asgi(app, "/missing"))
        anonymous_read_only_post, _headers, _body = asyncio.run(
            invoke_asgi(app, "/api/mappings/import/preview", method="POST")
        )
        health, _headers, _body = asyncio.run(invoke_asgi(app, "/healthz"))
        metrics, _headers, _body = asyncio.run(invoke_asgi(app, "/metrics"))
        unauthenticated_mutation, mutation_headers, _body = asyncio.run(
            invoke_asgi(app, "/api/system-locator", method="POST")
        )
        cross_origin_mutation, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/api/system-locator",
                method="POST",
                authorization=authorization,
                origin="https://attacker.example",
            )
        )
        same_origin_mutation, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/api/system-locator",
                method="POST",
                authorization=authorization,
                origin="http://ui.example.test:8080",
            )
        )
        conflicting_referer_mutation, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/api/system-locator",
                method="POST",
                authorization=authorization,
                origin="http://ui.example.test:8080",
                referer="https://attacker.example/form",
            )
        )
        duplicate_origin_mutation, _headers, _body = asyncio.run(
            invoke_asgi(
                app,
                "/api/system-locator",
                method="POST",
                authorization=authorization,
                origin="http://ui.example.test:8080",
                additional_origins=("https://attacker.example",),
            )
        )

        self.assertEqual(anonymous_read, 404)
        self.assertNotIn("www-authenticate", read_headers)
        self.assertEqual(anonymous_read_only_post, 200)
        self.assertEqual(health, 200)
        self.assertEqual(metrics, 200)
        self.assertEqual(unauthenticated_mutation, 401)
        self.assertEqual(
            mutation_headers.get("www-authenticate"),
            'Basic realm="truenas-jbod-ui"',
        )
        self.assertEqual(cross_origin_mutation, 403)
        self.assertEqual(same_origin_mutation, 422)
        self.assertEqual(conflicting_referer_mutation, 403)
        self.assertEqual(duplicate_origin_mutation, 403)

    def test_basic_mode_requires_explicit_main_ui_public_origin(self) -> None:
        for public_origin in (
            None,
            "not-an-origin",
            "ftp://ui.example.test",
            "https://user@ui.example.test",
            "https://ui.example.test/path",
            "https://ui.example.test?query=1",
            "https://ui.example.test#fragment",
            "https://ui.example.test?",
            "https://ui.example.test#",
        ):
            with self.subTest(public_origin=public_origin):
                with self.assertRaisesRegex(ValueError, "APP_PUBLIC_ORIGIN"):
                    self.make_app(auth_mode="basic", public_origin=public_origin)

    def test_every_non_get_route_is_protected_or_explicitly_read_only(self) -> None:
        app = self.make_app(auth_mode="network")
        seen_mutations: set[tuple[str, str]] = set()
        seen_read_only: set[tuple[str, str]] = set()

        for route in app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            dependency_names = {
                getattr(dependency.dependency, "__name__", "")
                for dependency in getattr(route, "dependencies", [])
            }
            for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
                key = (method, path)
                if key in READ_ONLY_NON_GET_ROUTES:
                    seen_read_only.add(key)
                    self.assertNotIn("require_read_ui_mutation_authorization", dependency_names)
                    continue
                seen_mutations.add(key)
                self.assertIn("require_read_ui_mutation_authorization", dependency_names, key)

        self.assertEqual(seen_mutations, MUTATION_ROUTE_TEMPLATES)
        self.assertEqual(seen_read_only, READ_ONLY_NON_GET_ROUTES)

    def test_public_origin_and_shared_operator_auth_reach_main_container(self) -> None:
        app_config = AppConfig(public_origin="https://ui.example.test")
        self.assertEqual(getattr(app_config, "public_origin", None), "https://ui.example.test")

        root = Path(__file__).resolve().parents[1]
        for compose_name in ("docker-compose.yml", "docker-compose.dev.yml"):
            compose = yaml.safe_load((root / compose_name).read_text(encoding="utf-8"))
            environment = compose["services"]["enclosure-ui"]["environment"]
            for key in (
                "APP_PUBLIC_ORIGIN",
                "ADMIN_AUTH_MODE",
                "ADMIN_AUTH_USERNAME",
                "ADMIN_AUTH_PASSWORD",
            ):
                self.assertIn(key, environment, (compose_name, key))

        secrets_compose = yaml.safe_load(
            (root / "docker-compose.secrets.yml").read_text(encoding="utf-8")
        )
        ui_service = secrets_compose["services"]["enclosure-ui"]
        self.assertEqual(
            ui_service["environment"]["ADMIN_AUTH_PASSWORD_FILE"],
            "/run/secrets/admin_auth_password",
        )
        self.assertIn("admin_auth_password", ui_service["secrets"])
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        self.assertIn("APP_PUBLIC_ORIGIN=", env_example)
        self.assertIn("main UI mutations are disabled", env_example)
        self.assertIn("main UI reads remain anonymous", env_example)
        self.assertNotIn("protects the full main UI", env_example)


if __name__ == "__main__":
    unittest.main()
