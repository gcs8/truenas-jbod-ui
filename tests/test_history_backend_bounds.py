from __future__ import annotations

import asyncio
import io
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from app.config import HistoryConfig
from app.services.history_backend import (
    HistoryBackendClient,
    HistoryBackendBusyError,
    HistoryBackendPolicyError,
    HistoryBackendResponseError,
)


class HistoryBackendBoundsTests(unittest.IsolatedAsyncioTestCase):
    def test_http_503_maps_to_dedicated_busy_error(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="http://history-backend:8001"))
        error = urllib.error.HTTPError(
            "http://history-backend:8001/api/history/scopes/bundle",
            503,
            "busy",
            {},
            io.BytesIO(b"busy"),
        )
        with patch("app.services.history_backend.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HistoryBackendBusyError):
                client._request_bytes_sync("/api/history/scopes/bundle")

    async def test_multi_scope_uses_one_strict_json_request_with_internal_bearer(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(
                service_url="http://history-backend:8001",
                timeout_seconds=10,
                refresh_token="synthetic-token",
            )
        )
        scopes = [
            {"system_id": "synthetic", "enclosure_id": "front", "slots": [0]},
            {"system_id": "synthetic", "enclosure_id": "rear", "slots": [5]},
        ]
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with patch.object(
            client,
            "_send_json",
            AsyncMock(return_value={"scopes": [], "budget": {"target_count": 2}}),
        ) as send:
            payload = await client.get_scopes_history(
                scopes=scopes,
                since=since,
                metrics=["temperature_c"],
                event_limit=0,
                metric_limit=24,
            )
        self.assertEqual(payload["budget"]["target_count"], 2)
        send.assert_awaited_once_with(
            "/api/history/scopes/bundle",
            {
                "scopes": scopes,
                "metrics": ["temperature_c"],
                "since": since,
                "event_limit": 0,
                "metric_limit": 24,
            },
        )

    def test_send_json_places_token_only_in_authorization_header(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", refresh_token="synthetic-token")
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"ok":true}'
        response.headers.items.return_value = []
        with patch("app.services.history_backend.urllib.request.urlopen", return_value=response) as urlopen:
            payload = asyncio.run(client._send_json("/api/history/refresh", {"mode": "fast"}))
        request = urlopen.call_args.args[0]
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
        self.assertNotIn("synthetic-token", request.full_url)
        self.assertNotIn(b"synthetic-token", request.data)

    async def test_multi_scope_transport_failure_returns_bounded_unavailable_scopes(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="http://history-backend:8001"))
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with patch.object(
            client,
            "_send_json",
            AsyncMock(side_effect=OSError("synthetic unavailable")),
        ):
            payload = await client.get_scopes_history(
                scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0, 1]}],
                since=since,
                metrics=["temperature_c"],
                event_limit=0,
                metric_limit=24,
            )
        self.assertFalse(payload["available"])
        self.assertEqual(len(payload["scopes"]), 1)
        self.assertFalse(payload["scopes"][0]["histories"]["0"]["available"])
        self.assertNotIn("synthetic unavailable", str(payload))

    async def test_policy_rejection_never_falls_back_per_slot(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="http://history-backend:8001"))
        with (
            patch.object(client, "_send_json", AsyncMock(side_effect=HistoryBackendPolicyError(413))) as send,
            patch.object(client, "_fetch_slot_history", AsyncMock()) as fallback,
        ):
            with self.assertRaises(HistoryBackendPolicyError):
                await client.get_scope_history(
                    system_id="synthetic",
                    enclosure_id="front",
                    slots=[0],
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
        send.assert_awaited_once()
        fallback.assert_not_awaited()

    async def test_only_404_uses_legacy_scope_compatibility_and_preserves_limits(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="http://history-backend:8001"))
        with (
            patch.object(client, "_send_json", AsyncMock(side_effect=HistoryBackendResponseError(404))) as send,
            patch.object(client, "_fetch_json", AsyncMock(return_value={"histories": {}})) as fetch,
        ):
            payload = await client.get_scope_history(
                system_id="synthetic",
                enclosure_id="front",
                slots=[999999],
                window_hours=24,
                metrics=["bytes_written"],
                event_limit=0,
                metric_limit=24,
            )
        self.assertIn(999999, payload)
        send.assert_awaited_once()
        params = fetch.await_args.kwargs["params"]
        self.assertEqual(params["metrics"], ["bytes_written"])
        self.assertEqual(params["event_limit"], 0)
        self.assertEqual(params["metric_limit"], 24)

    async def test_client_rejects_unbounded_request_before_network(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="http://history-backend:8001"))
        with patch.object(client, "_send_json", AsyncMock()) as send:
            with self.assertRaises(ValueError):
                await client.get_scope_history(
                    system_id="synthetic",
                    enclosure_id="front",
                    slots=[0],
                    window_hours=None,
                )
        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
