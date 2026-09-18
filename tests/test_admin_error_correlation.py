"""Admin failures carry a correlation id, and the stopped state says what to do (#418).

An operator who hits "see admin logs" has no way to find their request in the
log, and the auto-stop countdown claims a shutdown from the browser clock
alone. These tests pin a short random id that appears in both the error body
and the server log line, and a server-authoritative offline state that names
the next step in plain words.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unittest
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException

# Must precede admin_service.main, which builds its app at import time.
from tests.admin_test_env import ADMIN_TEST_PUBLIC_ORIGIN
from admin_service.config import AdminSettings
from admin_service.main import (
    admin_request_id,
    build_offline_recovery_state,
    create_app,
)
from app.request_context import REQUEST_ID_HEADER

REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


async def invoke_asgi(
    app,
    path: str,
    *,
    method: str = "GET",
    origin: str | None = None,
    authorization: str | None = None,
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
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
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


class AdminRequestIdTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch(
            "admin_service.main.get_admin_settings",
            return_value=AdminSettings(public_origin=ADMIN_TEST_PUBLIC_ORIGIN),
        ):
            self.app = create_app()

    def test_request_id_outside_a_request_context_is_still_random(self) -> None:
        first = admin_request_id()
        second = admin_request_id()

        self.assertRegex(first, REQUEST_ID_PATTERN)
        self.assertRegex(second, REQUEST_ID_PATTERN)
        self.assertNotEqual(first, second)

    def test_http_error_body_and_header_carry_the_same_request_id(self) -> None:
        status, headers, body = asyncio.run(invoke_asgi(self.app, "/api/admin/does-not-exist"))
        payload = json.loads(body)

        self.assertEqual(status, 404)
        self.assertRegex(payload["request_id"], REQUEST_ID_PATTERN)
        self.assertEqual(headers[REQUEST_ID_HEADER.lower()], payload["request_id"])
        self.assertIs(payload["ok"], False)

    def test_cross_origin_rejection_carries_a_request_id(self) -> None:
        status, headers, body = asyncio.run(
            invoke_asgi(
                self.app,
                "/api/admin/does-not-exist",
                method="POST",
                origin="https://unrelated.example",
            )
        )
        payload = json.loads(body)

        self.assertEqual(status, 403)
        self.assertRegex(payload["request_id"], REQUEST_ID_PATTERN)
        self.assertEqual(headers[REQUEST_ID_HEADER.lower()], payload["request_id"])

    def test_authentication_rejection_carries_a_request_id(self) -> None:
        settings = AdminSettings(
            public_origin=ADMIN_TEST_PUBLIC_ORIGIN,
            auth_mode="basic",
            auth_username="operator",
            auth_password="not-a-real-password",
        )
        with patch("admin_service.main.get_admin_settings", return_value=settings):
            app = create_app()

        status, headers, body = asyncio.run(invoke_asgi(app, "/api/admin/does-not-exist"))
        payload = json.loads(body)

        self.assertEqual(status, 401)
        self.assertRegex(payload["request_id"], REQUEST_ID_PATTERN)
        self.assertEqual(headers[REQUEST_ID_HEADER.lower()], payload["request_id"])

    def test_error_log_line_names_the_same_request_id_and_leaks_nothing(self) -> None:
        with self.assertLogs("admin_service.main", level=logging.WARNING) as captured:
            _status, _headers, body = asyncio.run(
                invoke_asgi(self.app, "/api/admin/does-not-exist")
            )
        payload = json.loads(body)

        matching = [line for line in captured.output if payload["request_id"] in line]
        self.assertTrue(matching, captured.output)
        for line in matching:
            self.assertNotIn("/api/admin/does-not-exist", line)
            self.assertNotIn("admin.example.test", line)

    def test_unhandled_error_response_correlates_with_the_logged_request_id(self) -> None:
        # The metrics middleware sits outside the app exception handlers and
        # already answers a crash with a correlated 500; pin that the id an
        # operator reads is the one the log line carries, and that the
        # exception text never reaches the browser.
        app = create_app()

        @app.get("/api/admin/_test-boom")
        async def _boom() -> None:
            raise RuntimeError("secret-bearing failure")

        with self.assertLogs("app.observability", level=logging.ERROR) as captured:
            status, headers, body = asyncio.run(invoke_asgi(app, "/api/admin/_test-boom"))
        payload = json.loads(body)

        self.assertEqual(status, 500)
        self.assertRegex(payload["request_id"], REQUEST_ID_PATTERN)
        self.assertEqual(headers[REQUEST_ID_HEADER.lower()], payload["request_id"])
        self.assertNotIn("secret-bearing failure", payload["detail"])
        logged_ids = {getattr(record, "request_id", None) for record in captured.records}
        self.assertIn(payload["request_id"], logged_ids)

    def test_request_id_never_reuses_a_client_supplied_value(self) -> None:
        # A client-controlled header must not become the logged correlation id.
        async def run() -> tuple[str, str]:
            messages: list[dict[str, Any]] = []

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                messages.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/admin/does-not-exist",
                "raw_path": b"/api/admin/does-not-exist",
                "query_string": b"",
                "headers": [
                    (b"host", b"admin.example.test"),
                    (REQUEST_ID_HEADER.lower().encode("ascii"), b"<script>alert(1)</script>"),
                ],
                "client": ("127.0.0.1", 12345),
                "server": ("admin.example.test", 8082),
            }
            await self.app(scope, receive, send)
            start = next(m for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            response_headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in start.get("headers", [])
            }
            return response_headers[REQUEST_ID_HEADER.lower()], body.decode("utf-8")

        header_value, raw_body = asyncio.run(run())
        self.assertRegex(header_value, REQUEST_ID_PATTERN)
        self.assertNotIn("<script>", raw_body)
        self.assertNotIn("alert(1)", raw_body)


class AdminOfflineRecoveryStateTests(unittest.TestCase):
    def test_no_auto_stop_reports_a_running_state_with_no_recovery_step(self) -> None:
        state = build_offline_recovery_state(expires_at=None, now=None)

        self.assertIs(state["expired"], False)
        self.assertEqual(state["summary"], "")
        self.assertEqual(state["next_step"], "")

    def test_expired_auto_stop_states_the_next_step_in_plain_words(self) -> None:
        from history_service.domain import utcnow

        now = utcnow()
        state = build_offline_recovery_state(expires_at=now - timedelta(seconds=1), now=now)

        self.assertIs(state["expired"], True)
        self.assertTrue(state["summary"])
        self.assertTrue(state["next_step"])
        # Plain words an operator can act on, and no promise the page can renew.
        self.assertIn("docker compose", state["next_step"].lower())
        self.assertNotIn("extend", state["next_step"].lower())

    def test_pending_auto_stop_is_not_reported_as_stopped(self) -> None:
        from history_service.domain import utcnow

        now = utcnow()
        state = build_offline_recovery_state(expires_at=now + timedelta(minutes=5), now=now)

        self.assertIs(state["expired"], False)
        self.assertEqual(state["summary"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
