from __future__ import annotations

import json
import unittest
from collections import deque
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, patch

from starlette.middleware.exceptions import ExceptionMiddleware

from app import main as app_main

MAX_HISTORY_SCOPES_REQUEST_BYTES = 64 * 1024


class MainHistoryScopesBodyBoundTests(unittest.IsolatedAsyncioTestCase):
    async def _post(
        self,
        chunks: list[bytes],
        *,
        content_length: int | None = None,
    ) -> tuple[int, dict[str, object], int]:
        messages = deque(
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        )
        receive_calls = 0
        response_messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            nonlocal receive_calls
            if not messages:
                return {"type": "http.disconnect"}
            receive_calls += 1
            return messages.popleft()

        async def send(message: dict[str, object]) -> None:
            response_messages.append(message)

        headers = [(b"content-type", b"application/json")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode("ascii")))
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/history/scopes/bundle",
            "raw_path": b"/api/history/scopes/bundle",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "app": app_main.app,
        }
        asgi_app = ExceptionMiddleware(
            app_main.app.router,
            handlers={
                exception_type: handler
                for exception_type, handler in app_main.app.exception_handlers.items()
                if exception_type is not Exception
            },
        )
        async with AsyncExitStack() as middleware_stack, AsyncExitStack() as inner_stack:
            scope["fastapi_middleware_astack"] = middleware_stack
            scope["fastapi_inner_astack"] = inner_stack
            await asgi_app(scope, receive, send)

        start = next(message for message in response_messages if message["type"] == "http.response.start")
        body = b"".join(
            message.get("body", b"")
            for message in response_messages
            if message["type"] == "http.response.body"
        )
        return int(start["status"]), json.loads(body), receive_calls

    async def test_declared_oversize_is_rejected_without_receive_or_backend_work(self) -> None:
        with patch.object(app_main, "get_history_backend") as get_history_backend:
            status, payload, receive_calls = await self._post(
                [b"ignored"],
                content_length=MAX_HISTORY_SCOPES_REQUEST_BYTES + 1,
            )

        self.assertEqual(status, 413)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "detail": "History request exceeds 65536 bytes.",
            },
        )
        self.assertEqual(receive_calls, 0)
        get_history_backend.assert_not_called()

    async def test_chunked_oversize_stops_immediately_after_crossing_bound(self) -> None:
        with patch.object(app_main, "get_history_backend") as get_history_backend:
            status, payload, receive_calls = await self._post(
                [b"x" * MAX_HISTORY_SCOPES_REQUEST_BYTES, b"y", b"unread"],
            )

        self.assertEqual(status, 413)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "detail": "History request exceeds 65536 bytes.",
            },
        )
        self.assertEqual(receive_calls, 2)
        get_history_backend.assert_not_called()

    async def test_malformed_json_returns_stable_bounded_error(self) -> None:
        with patch.object(app_main, "get_history_backend") as get_history_backend:
            status, payload, receive_calls = await self._post([b"{"])

        self.assertEqual(status, 422)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "detail": "History request must be valid JSON.",
            },
        )
        self.assertEqual(receive_calls, 1)
        get_history_backend.assert_not_called()

    async def test_in_limit_invalid_shape_returns_stable_bounded_error(self) -> None:
        with patch.object(app_main, "get_history_backend") as get_history_backend:
            status, payload, receive_calls = await self._post([b"{}"])

        self.assertEqual(status, 422)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "detail": "History request body is invalid.",
            },
        )
        self.assertEqual(receive_calls, 1)
        get_history_backend.assert_not_called()

    async def test_valid_in_limit_body_still_reaches_backend(self) -> None:
        request_document = {
            "scopes": [
                {
                    "system_id": "synthetic",
                    "enclosure_id": "front",
                    "slots": [0],
                }
            ],
            "metrics": ["temperature_c"],
            "since": "2026-01-01T00:00:00+00:00",
            "event_limit": 0,
            "metric_limit": 24,
        }
        body = json.dumps(request_document).encode("utf-8")
        backend = AsyncMock()
        backend.get_scopes_history.return_value = {"ok": True}

        with patch.object(app_main, "get_history_backend", return_value=backend):
            status, payload, receive_calls = await self._post(
                [body],
                content_length=len(body),
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(receive_calls, 1)
        backend.get_scopes_history.assert_awaited_once_with(
            scopes=request_document["scopes"],
            metrics=request_document["metrics"],
            since=request_document["since"],
            event_limit=request_document["event_limit"],
            metric_limit=request_document["metric_limit"],
        )

    async def test_openapi_retains_the_history_scopes_request_contract(self) -> None:
        request_schema = app_main.app.openapi()["paths"]["/api/history/scopes/bundle"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]

        self.assertEqual(request_schema["title"], "HistoryScopesProxyRequest")
        self.assertEqual(
            request_schema["properties"]["scopes"]["items"]["title"],
            "HistoryScopeProxyRequest",
        )
        self.assertNotIn("$defs", request_schema)


if __name__ == "__main__":
    unittest.main()
