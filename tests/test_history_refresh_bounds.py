from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from history_service import main as history_main
from history_service.config import HistorySettings, get_history_settings
from history_service.refresh_auth import ManualRefreshAdmission, authorize_refresh_request, read_refresh_document


class HistoryRefreshConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_history_settings.cache_clear()

    def test_loopback_network_mode_is_the_default(self) -> None:
        settings = HistorySettings()
        self.assertEqual(settings.published_bind_address, "127.0.0.1")
        self.assertEqual(settings.refresh_auth_mode, "network")
        self.assertEqual(settings.full_refresh_cooldown_seconds, 900)
        self.assertIsNone(settings.refresh_token)

    def test_non_loopback_network_mode_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "HISTORY_REFRESH_AUTH_MODE=token"):
            HistorySettings(published_bind_address="0.0.0.0", refresh_auth_mode="network")

    def test_exposed_token_mode_requires_token_and_strict_public_origin(self) -> None:
        for overrides in (
            {"refresh_auth_mode": "token", "refresh_token": ""},
            {"refresh_auth_mode": "token", "refresh_token": "synthetic-token", "public_origin": None},
            {"refresh_auth_mode": "token", "refresh_token": "synthetic-token", "public_origin": "https://example.test/path"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                HistorySettings(published_bind_address="192.0.2.20", **overrides)
        settings = HistorySettings(
            published_bind_address="192.0.2.20",
            refresh_auth_mode="token",
            refresh_token="synthetic-token",
            public_origin="https://history.example.test",
        )
        self.assertEqual(settings.public_origin, "https://history.example.test")

    def test_refresh_token_file_takes_precedence_without_leaking_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "refresh-token"
            token_path.write_text("file-token\n", encoding="utf-8")
            token_path.chmod(0o600)
            with patch.dict(
                os.environ,
                {
                    "HISTORY_SQLITE_PATH": str(Path(temp_dir) / "history.db"),
                    "HISTORY_PUBLISHED_BIND_ADDRESS": "192.0.2.20",
                    "HISTORY_PUBLIC_ORIGIN": "https://history.example.test",
                    "HISTORY_REFRESH_AUTH_MODE": "token",
                    "HISTORY_REFRESH_TOKEN": "environment-token",
                    "HISTORY_REFRESH_TOKEN_FILE": str(token_path),
                },
                clear=False,
            ):
                get_history_settings.cache_clear()
                settings = get_history_settings()
        self.assertEqual(settings.refresh_token.get_secret_value(), "file-token")
        self.assertNotIn("file-token", repr(settings))


class HistoryRefreshRequestTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request(body: bytes, *, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/api/history/refresh",
                "raw_path": b"/api/history/refresh",
                "query_string": b"",
                "headers": headers or [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 12345),
                "server": ("history.example.test", 443),
            },
            receive,
        )

    async def test_refresh_document_is_strict_and_limited_to_256_bytes(self) -> None:
        self.assertEqual(await read_refresh_document(self._request(b'{"mode":"fast"}')), "fast")
        invalid = (
            b"{}",
            b'{"mode":"slow"}',
            b'{"mode":"fast","force":true}',
            b'{"mode":"fast","mode":"full"}',
            b"not-json",
            b'{"mode":"fast"}' + b" " * 300,
        )
        for body in invalid:
            with self.subTest(body=body[:40]), self.assertRaises(HTTPException) as raised:
                await read_refresh_document(self._request(body))
            self.assertEqual(raised.exception.status_code, 422 if len(body) <= 256 else 413)

    async def test_token_auth_and_origin_are_both_required_for_browser_calls(self) -> None:
        settings = HistorySettings(
            refresh_auth_mode="token",
            refresh_token="synthetic-token",
            public_origin="https://history.example.test",
        )
        cases = (
            ([], 401),
            ([(b"authorization", b"Basic abc")], 401),
            ([(b"authorization", b"Bearer wrong")], 401),
            (
                [(b"authorization", b"Bearer synthetic-token"), (b"origin", b"https://foreign.example.test")],
                403,
            ),
        )
        for headers, status_code in cases:
            with self.subTest(headers=headers), self.assertRaises(HTTPException) as raised:
                authorize_refresh_request(self._request(b'{"mode":"fast"}', headers=headers), settings)
            self.assertEqual(raised.exception.status_code, status_code)
        authorize_refresh_request(
            self._request(
                b'{"mode":"fast"}',
                headers=[
                    (b"authorization", b"Bearer synthetic-token"),
                    (b"origin", b"https://history.example.test"),
                ],
            ),
            settings,
        )
        authorize_refresh_request(
            self._request(b'{"mode":"fast"}', headers=[(b"authorization", b"Bearer synthetic-token")]),
            settings,
        )

    async def test_admission_is_atomic_and_full_cooldown_is_recorded_at_start(self) -> None:
        clock = Mock(return_value=1000.0)
        admission = ManualRefreshAdmission(cooldown_seconds=900, monotonic=clock)
        first = await admission.try_acquire("full")
        self.assertTrue(first.accepted)
        second = await admission.try_acquire("fast")
        self.assertFalse(second.accepted)
        self.assertEqual(second.status_code, 409)
        await admission.release()

        clock.return_value = 1000.1
        cooled = await admission.try_acquire("full")
        self.assertFalse(cooled.accepted)
        self.assertEqual(cooled.status_code, 429)
        self.assertEqual(cooled.retry_after, 900)

        clock.return_value = 1900.0
        boundary = await admission.try_acquire("full")
        self.assertTrue(boundary.accepted)
        await admission.release()

    async def test_concurrent_refresh_calls_run_collector_once_and_skip_overview_on_rejection(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/refresh")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_run_once(**_kwargs):
            entered.set()
            await release.wait()

        settings = HistorySettings()
        admission = ManualRefreshAdmission(cooldown_seconds=900, monotonic=Mock(return_value=1000.0))
        request = self._request(b'{"mode":"fast"}')
        with (
            patch.object(history_main, "settings", settings),
            patch.object(history_main, "refresh_admission", admission),
            patch.object(history_main.collector, "run_once", AsyncMock(side_effect=blocked_run_once)) as run_once,
            patch.object(type(history_main.collector), "collection_running", new_callable=PropertyMock, return_value=False),
            patch.object(history_main.store, "estimated_counts", return_value={"tracked_slots": 0}) as counts,
            patch.object(history_main.store, "list_scopes", return_value=[]),
        ):
            winner = asyncio.create_task(route.endpoint(request=request))
            await entered.wait()
            loser = await route.endpoint(request=self._request(b'{"mode":"fast"}'))
            self.assertEqual(loser.status_code, 409)
            self.assertEqual(counts.call_count, 0)
            release.set()
            successful = await winner

        self.assertTrue(successful["ok"])
        run_once.assert_awaited_once()
        self.assertEqual(counts.call_count, 1)


if __name__ == "__main__":
    unittest.main()
