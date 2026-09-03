from __future__ import annotations

import asyncio
import io
import unittest
import urllib.error
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import ENV_OVERRIDES, HistoryConfig
from app.request_context import request_context
from app.services.history_backend import (
    HISTORY_BACKEND_FAILURE_DETAIL,
    HistoryBackendClient,
    HistoryBackendResponseError,
    HistoryBackendUnavailableError,
)


class HistoryBackendClientTests(unittest.IsolatedAsyncioTestCase):
    def test_request_bytes_sync_propagates_current_server_request_id(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        response.__enter__.return_value.headers.items.return_value = []

        with (
            request_context("c" * 32),
            patch("app.services.history_backend.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            client._request_bytes_sync("/healthz")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-request-id"), "c" * 32)

    def test_request_bytes_sync_preserves_list_query_params(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        class FakeResponse:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> bool | None:
                return None

            def read(self) -> bytes:
                return b"{}"

        with patch("app.services.history_backend.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            payload, headers = client._request_bytes_sync(
                "/api/history/scopes/slots",
                params={
                    "system_id": "archive-core",
                    "slots": [5, 6],
                    "enclosure_id": "",
                },
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(payload, b"{}")
        self.assertEqual(headers, {})
        self.assertIn("system_id=archive-core", request.full_url)
        self.assertIn("slots=5&slots=6", request.full_url)
        self.assertNotIn("enclosure_id=", request.full_url)

    async def test_get_status_returns_unconfigured_shape_when_url_missing(self) -> None:
        client = HistoryBackendClient(HistoryConfig(service_url="", timeout_seconds=10))

        payload = await client.get_status()

        self.assertFalse(payload["configured"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["counts"], {})
        self.assertEqual(payload["collector"], {})
        self.assertEqual(payload["scopes"], [])

    async def test_get_status_returns_available_payload_when_backend_responds(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(
                return_value={
                    "status": "ok",
                    "counts": {"tracked_slots": 12, "metric_sample_count": 48},
                    "collector": {"last_completed_at": "2026-04-16T23:10:00+00:00"},
                    "scopes": [{"system_id": "archive-core", "enclosure_id": "front"}],
                }
            ),
        ) as fetch_json:
            payload = await client.get_status()

        self.assertTrue(payload["configured"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["counts"]["tracked_slots"], 12)
        self.assertEqual(payload["collector"]["last_completed_at"], "2026-04-16T23:10:00+00:00")
        self.assertEqual(len(payload["scopes"]), 1)
        fetch_json.assert_awaited_once_with("/healthz")

    async def test_get_status_uses_lightweight_health_shape_when_counts_are_absent(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(
                return_value={
                    "status": "degraded",
                    "last_error": "collector timed out",
                    "collector": {"last_success_at": "2026-05-14T23:10:00+00:00"},
                }
            ),
        ) as fetch_json:
            payload = await client.get_status()

        self.assertTrue(payload["configured"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["detail"], "History backend is degraded; see history service logs.")
        self.assertEqual(payload["counts"], {})
        self.assertEqual(payload["collector"]["last_success_at"], "2026-05-14T23:10:00+00:00")
        self.assertEqual(payload["scopes"], [])
        fetch_json.assert_awaited_once_with("/healthz")

    async def test_get_status_redacts_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(side_effect=RuntimeError("Traceback: password=secret backend timed out")),
        ):
            payload = await client.get_status()

        self.assertFalse(payload["available"])
        self.assertEqual(payload["detail"], "History backend request failed; see application logs.")
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("Traceback", str(payload))

    async def test_get_slot_history_redacts_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(side_effect=RuntimeError("Traceback: token=secret backend timed out")),
        ):
            payload = await client.get_slot_history(5, "archive-core", "front", window_hours=24)

        self.assertFalse(payload["available"])
        self.assertEqual(payload["detail"], "History backend request failed; see application logs.")
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("Traceback", str(payload))

    async def test_get_slot_history_shapes_metric_and_event_payloads(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with (
            patch.object(
                client,
                "_build_since_isoformat",
                return_value="2026-04-15T23:10:00+00:00",
            ),
            patch.object(
                client,
                "_fetch_json",
                AsyncMock(
                    return_value={
                        "events": [
                            {
                                "observed_at": "2026-04-16T23:15:00+00:00",
                                "event_type": "slot_identity_changed",
                                "previous_value": "SERIAL-OLD",
                                "current_value": "SERIAL-NEW",
                            }
                        ],
                        "metrics": {
                            "temperature_c": [
                                {
                                    "observed_at": "2026-04-16T23:10:00+00:00",
                                    "value": 31,
                                }
                            ],
                            "bytes_read": [
                                {
                                    "observed_at": "2026-04-16T23:10:00+00:00",
                                    "value": 549755813888,
                                }
                            ],
                            "bytes_written": [
                                {
                                    "observed_at": "2026-04-16T23:10:00+00:00",
                                    "value": 1099511627776,
                                }
                            ],
                            "annualized_bytes_read": [],
                            "annualized_bytes_written": [],
                            "power_on_hours": [
                                {
                                    "observed_at": "2026-04-16T23:10:00+00:00",
                                    "value": 10101,
                                }
                            ],
                        },
                        "sample_counts": {
                            "temperature_c": 1,
                            "bytes_read": 1,
                            "bytes_written": 1,
                            "annualized_bytes_read": 0,
                            "annualized_bytes_written": 0,
                            "power_on_hours": 1,
                        },
                        "latest_values": {
                            "temperature_c": 31,
                            "bytes_read": 549755813888,
                            "bytes_written": 1099511627776,
                            "annualized_bytes_read": None,
                            "annualized_bytes_written": None,
                            "power_on_hours": 10101,
                        },
                        "disk_history": {
                            "followed": True,
                            "prior_home_count": 1,
                        },
                    }
                ),
            ) as fetch_json,
        ):
            payload = await client.get_slot_history(5, "archive-core", "front", window_hours=24)

        self.assertTrue(payload["configured"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["slot"], 5)
        self.assertEqual(payload["system_id"], "archive-core")
        self.assertEqual(payload["enclosure_id"], "front")
        self.assertEqual(payload["sample_counts"]["temperature_c"], 1)
        self.assertEqual(payload["sample_counts"]["bytes_read"], 1)
        self.assertEqual(payload["sample_counts"]["annualized_bytes_read"], 0)
        self.assertEqual(payload["sample_counts"]["annualized_bytes_written"], 0)
        self.assertEqual(payload["latest_values"]["temperature_c"], 31)
        self.assertEqual(payload["latest_values"]["bytes_read"], 549755813888)
        self.assertEqual(payload["latest_values"]["bytes_written"], 1099511627776)
        self.assertIsNone(payload["latest_values"]["annualized_bytes_read"])
        self.assertIsNone(payload["latest_values"]["annualized_bytes_written"])
        self.assertEqual(payload["latest_values"]["power_on_hours"], 10101)
        self.assertEqual(len(payload["events"]), 1)
        self.assertTrue(payload["disk_history"]["followed"])
        fetch_json.assert_awaited_once_with(
            "/api/history/slots/5/bundle",
            params={
                "system_id": "archive-core",
                "enclosure_id": "front",
                "since": "2026-04-15T23:10:00+00:00",
                "event_limit": 12,
            },
        )

    async def test_get_scope_history_uses_scope_endpoint_when_available(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(
                return_value={
                    "histories": {
                        "5": {
                            "slot": 5,
                            "events": [{"observed_at": "2026-04-16T23:15:00+00:00"}],
                            "metrics": {"temperature_c": [{"observed_at": "2026-04-16T23:10:00+00:00", "value": 31}]},
                            "sample_counts": {"temperature_c": 1},
                            "latest_values": {"temperature_c": 31},
                        },
                        "6": {
                            "slot": 6,
                            "events": [],
                            "metrics": {"temperature_c": []},
                            "sample_counts": {"temperature_c": 0},
                            "latest_values": {"temperature_c": None},
                        },
                    }
                }
            ),
        ) as fetch_json:
            with patch.object(
                client,
                "_build_since_isoformat",
                return_value="2026-04-15T23:10:00+00:00",
            ):
                payload = await client.get_scope_history(
                    system_id="archive-core",
                    enclosure_id="front",
                    slots=[5, 6],
                    window_hours=24,
                )

        self.assertEqual(payload[5]["latest_values"]["temperature_c"], 31)
        self.assertEqual(payload[6]["sample_counts"]["temperature_c"], 0)
        fetch_json.assert_awaited_once_with(
            "/api/history/scopes/slots",
            params={
                "system_id": "archive-core",
                "enclosure_id": "front",
                "slots": [5, 6],
                "since": "2026-04-15T23:10:00+00:00",
                "event_limit": 12,
            },
        )

    async def test_get_scope_history_can_request_only_needed_metrics(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(return_value={"histories": {"5": {"slot": 5, "metrics": {"bytes_written": []}}}}),
        ) as fetch_json:
            with patch.object(
                client,
                "_build_since_isoformat",
                return_value="2026-04-15T23:10:00+00:00",
            ):
                await client.get_scope_history(
                    system_id="archive-core",
                    enclosure_id="front",
                    slots=[5],
                    window_hours=24,
                    metrics=["bytes_written"],
                    event_limit=0,
                )

        fetch_json.assert_awaited_once_with(
            "/api/history/scopes/slots",
            params={
                "system_id": "archive-core",
                "enclosure_id": "front",
                "slots": [5],
                "since": "2026-04-15T23:10:00+00:00",
                "event_limit": 0,
                "metrics": ["bytes_written"],
            },
        )

    async def test_get_scope_history_falls_back_to_per_slot_fetch_on_scope_error(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        calls: list[tuple[str, dict[str, Any]]] = []

        async def fake_fetch_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append((path, dict(params or {})))
            if path == "/api/history/scopes/slots":
                raise HistoryBackendResponseError("History backend returned HTTP 500: boom")
            return {"metrics": {"temperature_c": [[1, 2]]}, "events": [], "sample_counts": {}, "latest_values": {}}

        with patch.object(client, "_fetch_json", fake_fetch_json):
            payload = await client.get_scope_history(
                system_id="archive-core",
                enclosure_id="front",
                slots=[5],
                window_hours=24,
            )

        self.assertEqual(payload[5]["slot"], 5)
        self.assertTrue(payload[5]["available"])
        self.assertEqual(payload[5]["metrics"], {"temperature_c": [[1, 2]]})
        self.assertEqual([path for path, _ in calls], ["/api/history/scopes/slots", "/api/history/slots/5/bundle"])
        self.assertEqual(calls[1][1]["system_id"], "archive-core")
        self.assertEqual(calls[1][1]["enclosure_id"], "front")
        self.assertIn("since", calls[1][1])

    async def test_get_scope_history_fallback_dedupes_slots_and_bounds_concurrency(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10, fallback_max_concurrency=2)
        )
        in_flight = 0
        max_in_flight = 0
        per_slot_paths: list[str] = []

        async def fake_fetch_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal in_flight, max_in_flight
            if path == "/api/history/scopes/slots":
                raise HistoryBackendResponseError("History backend returned HTTP 503: busy")
            per_slot_paths.append(path)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return {"metrics": {}, "events": [], "sample_counts": {}, "latest_values": {}}

        with patch.object(client, "_fetch_json", fake_fetch_json):
            payload = await client.get_scope_history(
                system_id="archive-core",
                enclosure_id="front",
                slots=[0, 1, 2, 1, 3, 0, 4, 5],
                window_hours=24,
            )

        self.assertEqual(sorted(payload), [0, 1, 2, 3, 4, 5])
        self.assertEqual(len(per_slot_paths), 6, "duplicate slot ids must not issue duplicate requests")
        self.assertEqual(sorted(per_slot_paths), sorted(f"/api/history/slots/{slot}/bundle" for slot in range(6)))
        self.assertLessEqual(max_in_flight, 2)
        self.assertTrue(all(entry["available"] for entry in payload.values()))

    async def test_get_scope_history_fallback_stops_fanning_out_once_backend_is_unreachable(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10, fallback_max_concurrency=3)
        )
        per_slot_calls = 0

        async def fake_fetch_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal per_slot_calls
            if path == "/api/history/scopes/slots":
                raise HistoryBackendUnavailableError("History backend request failed: [Errno 111] Connection refused")
            per_slot_calls += 1
            raise HistoryBackendUnavailableError("History backend request failed: [Errno 111] Connection refused")

        slots = list(range(60))
        with patch.object(client, "_fetch_json", fake_fetch_json):
            payload = await client.get_scope_history(
                system_id="archive-core",
                enclosure_id="front",
                slots=slots,
                window_hours=24,
            )

        self.assertEqual(sorted(payload), slots)
        self.assertLessEqual(
            per_slot_calls,
            3,
            "an unreachable backend must cost at most one timeout per concurrency slot, not one per bay",
        )
        for slot in slots:
            self.assertFalse(payload[slot]["available"])
            self.assertEqual(payload[slot]["detail"], HISTORY_BACKEND_FAILURE_DETAIL)
            self.assertEqual(payload[slot]["slot"], slot)

    async def test_get_scope_history_fallback_keeps_going_after_http_errors(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10, fallback_max_concurrency=2)
        )

        async def fake_fetch_json(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if path == "/api/history/scopes/slots":
                return {"histories": "not-a-dict"}
            if path.endswith("/1/bundle"):
                raise HistoryBackendResponseError("History backend returned HTTP 404: unknown slot")
            return {"metrics": {}, "events": [], "sample_counts": {}, "latest_values": {}}

        with patch.object(client, "_fetch_json", fake_fetch_json):
            payload = await client.get_scope_history(
                system_id="archive-core",
                enclosure_id="front",
                slots=[0, 1, 2],
            )

        self.assertTrue(payload[0]["available"])
        self.assertFalse(payload[1]["available"])
        self.assertTrue(payload[2]["available"])

    def test_request_bytes_sync_maps_transport_failures_to_typed_errors(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch(
            "app.services.history_backend.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(HistoryBackendUnavailableError):
                client._request_bytes_sync("/healthz")

        with patch("app.services.history_backend.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(HistoryBackendUnavailableError):
                client._request_bytes_sync("/healthz")

        http_error = urllib.error.HTTPError("http://history-backend:8001/healthz", 503, "busy", {}, io.BytesIO(b"busy"))
        with patch("app.services.history_backend.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(HistoryBackendResponseError) as raised:
                client._request_bytes_sync("/healthz")
        self.assertIn("HTTP 503", str(raised.exception))
        self.assertNotIsInstance(raised.exception, HistoryBackendUnavailableError)

    def test_history_config_exposes_fallback_concurrency_env(self) -> None:
        self.assertEqual(HistoryConfig().fallback_max_concurrency, 4)
        self.assertEqual(
            ENV_OVERRIDES["HISTORY_BACKEND_FALLBACK_CONCURRENCY"],
            ("history", "fallback_max_concurrency"),
        )
