from __future__ import annotations

import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

# admin_service.main builds the module-level app at import time and refuses to
# start without a browser origin; give the test process a synthetic one.
os.environ.setdefault("ADMIN_PUBLIC_ORIGIN", "http://admin.example.test")

from pydantic import SecretStr

from admin_service.config import AdminSettings
from admin_service.main import create_app


async def invoke_asgi(
    app,
    path: str,
    *,
    authorization: str | None = None,
    origin: str | None = None,
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
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
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
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


class AdminRuntimeRouteTests(unittest.TestCase):
    def _runtime_service(self) -> MagicMock:
        service = MagicMock()
        service.status_payload.return_value = {
            "available": True,
            "detail": "Docker socket /private/runtime/docker.sock is unavailable.",
            "private_runtime_field": "/private/runtime/config.yaml",
            "containers": [
                {
                    "key": "ui",
                    "name": "truenas-jbod-ui",
                    "label": "Read UI",
                    "description": "Primary read-mostly enclosure UI.",
                    "status": "running",
                    "status_text": "Up (healthy)",
                    "running": True,
                    "health": "healthy",
                    "restart_required": False,
                    "lifecycle_state": "normal",
                    "lifecycle_label": "Normal",
                    "can_stop": True,
                    "can_start": False,
                    "can_restart": True,
                    "running_version": "0.22.0",
                    "version_probe_error": "Version probe failed for http://private-runtime.internal/livez.",
                    "private_container_field": "/private/container/config",
                }
            ],
        }
        return service

    def test_runtime_get_route_returns_a_narrow_fresh_runtime_payload(self) -> None:
        settings = AdminSettings(
            auth_mode="network",
            public_origin="http://admin.example.test",
            auto_stop_seconds=0,
        )
        runtime_service = self._runtime_service()
        release_service = SimpleNamespace(
            snapshot=lambda: {"latest_tag": "v0.22.0", "latest_version": "0.22.0"}
        )
        with (
            patch("admin_service.main.get_admin_settings", return_value=settings),
            patch("admin_service.main.get_runtime_service", return_value=runtime_service),
            patch("admin_service.main.get_release_status_service", return_value=release_service),
        ):
            app = create_app()
            status, _headers, body = asyncio.run(invoke_asgi(app, "/api/admin/runtime"))

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"ok", "runtime"})
        self.assertTrue(payload["ok"])
        runtime = payload["runtime"]
        self.assertEqual(
            set(runtime),
            {"available", "detail", "version_state", "version_detail", "containers"},
        )
        self.assertTrue(runtime["available"])
        container = runtime["containers"][0]
        self.assertEqual(
            set(container),
            {
                "key",
                "name",
                "label",
                "description",
                "status",
                "status_text",
                "running",
                "health",
                "restart_required",
                "lifecycle_state",
                "lifecycle_label",
                "can_stop",
                "can_start",
                "can_restart",
                "running_version",
                "latest_version",
                "release_status",
                "version_sync_state",
                "version_sync_summary",
            },
        )
        self.assertTrue(container["running"])
        serialized = json.dumps(payload)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("private-runtime.internal", serialized)
        self.assertNotIn("version_probe_error", serialized)
        runtime_service.status_payload.assert_called_once_with()

    def test_runtime_get_route_keeps_basic_auth_and_read_only_origin_boundaries(self) -> None:
        settings = AdminSettings(
            auth_mode="basic",
            auth_username="operator",
            auth_password=SecretStr("runtime-passphrase"),
            public_origin="http://admin.example.test",
            auto_stop_seconds=0,
        )
        runtime_service = self._runtime_service()
        release_service = SimpleNamespace(snapshot=lambda: {})
        with (
            patch("admin_service.main.get_admin_settings", return_value=settings),
            patch("admin_service.main.get_runtime_service", return_value=runtime_service),
            patch("admin_service.main.get_release_status_service", return_value=release_service),
        ):
            app = create_app()
            unauthenticated, headers, _body = asyncio.run(
                invoke_asgi(app, "/api/admin/runtime")
            )
            authenticated, _headers, _body = asyncio.run(
                invoke_asgi(
                    app,
                    "/api/admin/runtime",
                    authorization=basic_header("operator", "runtime-passphrase"),
                    origin="https://status-reader.example",
                )
            )

        self.assertEqual(unauthenticated, 401)
        self.assertEqual(headers.get("www-authenticate"), 'Basic realm="truenas-jbod-admin"')
        self.assertEqual(authenticated, 200, "GET remains read-only and does not use mutation-origin rejection")
        runtime_service.status_payload.assert_called_once_with()

    def test_runtime_get_route_replaces_unavailable_runtime_detail(self) -> None:
        settings = AdminSettings(
            auth_mode="network",
            public_origin="http://admin.example.test",
            auto_stop_seconds=0,
        )
        runtime_service = MagicMock()
        runtime_service.status_payload.return_value = {
            "available": False,
            "detail": "Docker socket /private/runtime/docker.sock is not mounted.",
            "containers": [],
        }
        release_service = SimpleNamespace(snapshot=lambda: {})
        with (
            patch("admin_service.main.get_admin_settings", return_value=settings),
            patch("admin_service.main.get_runtime_service", return_value=runtime_service),
            patch("admin_service.main.get_release_status_service", return_value=release_service),
        ):
            app = create_app()
            status, _headers, body = asyncio.run(invoke_asgi(app, "/api/admin/runtime"))

        self.assertEqual(status, 200)
        runtime = json.loads(body)["runtime"]
        self.assertFalse(runtime["available"])
        self.assertEqual(runtime["detail"], "Docker runtime control is unavailable.")
        self.assertNotIn("/private/", json.dumps(runtime))


if __name__ == "__main__":
    unittest.main()
