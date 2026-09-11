from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import HistoryConfig
from app.request_context import request_context
from app.services.history_backend import (
    HISTORY_BACKEND_DEGRADED_DETAIL,
    HISTORY_BACKEND_FAILURE_DETAIL,
    HistoryBackendClient,
    HistoryBackendResponseError,
    HistoryBackendUnavailableError,
)
from app.services.history_status import (
    PUBLIC_COLLECTOR_STATUS_FIELDS,
    project_public_collector_status,
)


EXPECTED_PUBLIC_COLLECTOR_STATUS_FIELDS = (
    "collector_running",
    "collection_running",
    "collection_kind",
    "collection_activity",
    "collection_elapsed_seconds",
    "last_collection_inventory_forced",
    "last_collection_duration_seconds",
    "last_background_overrun_seconds",
    "background_consecutive_failures",
    "background_backoff_until",
    "background_backoff_seconds_remaining",
    "next_collection_at",
    "last_inventory_at",
    "last_fast_metrics_at",
    "last_slow_metrics_at",
    "last_success_at",
    "last_completed_at",
    "last_backup_at",
    "last_retention_at",
    "last_retention_duration_seconds",
    "last_retention_rows_removed",
    "last_retention_has_more",
    "last_retention_error",
    "last_error",
)


class HistoryBackendClientTests(unittest.IsolatedAsyncioTestCase):
    LEAKING_EXCEPTION_TEXT = (
        "raw transport failure token=secret password=secret "
        "url=https://history.invalid/private payload={'credential': 'secret'} path=/srv/private/history.db"
    )

    def test_public_collector_status_uses_exact_allowlist(self) -> None:
        status: dict[str, Any] = {
            field: f"approved-{index}"
            for index, field in enumerate(EXPECTED_PUBLIC_COLLECTOR_STATUS_FIELDS)
        }
        status.update(
            {
                "last_error": None,
                "future_internal_metadata": "status-leak-ZXQ9",
                "source_base_url": "https://collector.status-leak-ZXQ9.example.test",
                "sqlite_path": "/synthetic/private/status-leak-ZXQ9/history.db",
                "collection_stage_timings": [
                    {"stage": "internal", "error": "status-leak-ZXQ9"}
                ],
                "poll_interval_seconds": "status-leak-ZXQ9",
                "failure_backoff_max_seconds": "status-leak-ZXQ9",
                "last_scope_count": "status-leak-ZXQ9",
                "last_smart_failure_evidence_disks": "status-leak-ZXQ9",
                "last_max_temperature_celsius": "status-leak-ZXQ9",
                "last_retention_metric_samples_removed": "status-leak-ZXQ9",
            }
        )

        projected = project_public_collector_status(
            status,
            last_error_detail="Stable public collector detail.",
        )

        self.assertEqual(PUBLIC_COLLECTOR_STATUS_FIELDS, EXPECTED_PUBLIC_COLLECTOR_STATUS_FIELDS)
        self.assertEqual(
            projected,
            {field: status[field] for field in EXPECTED_PUBLIC_COLLECTOR_STATUS_FIELDS},
        )
        self.assertNotIn("status-leak-ZXQ9", json.dumps(projected))

    def test_public_collector_status_replaces_raw_last_error(self) -> None:
        projected = project_public_collector_status(
            {"collector_running": True, "last_error": "status-leak-ZXQ9"},
            last_error_detail="Stable public collector detail.",
        )

        self.assertEqual(
            projected,
            {"collector_running": True, "last_error": "Stable public collector detail."},
        )
        self.assertNotIn("status-leak-ZXQ9", json.dumps(projected))

    def test_public_collector_status_rejects_non_mapping_input(self) -> None:
        for malformed in ([{"collector_running": True}], "status-leak-ZXQ9", None):
            with self.subTest(malformed=malformed):
                self.assertEqual(
                    project_public_collector_status(
                        malformed,
                        last_error_detail="Stable public collector detail.",
                    ),
                    {},
                )

    def assert_single_safe_warning(self, captured: Any, expected: str) -> None:
        self.assertEqual(
            captured.output,
            [f"WARNING:app.services.history_backend:{expected}"],
        )
        rendered = "\n".join(captured.output)
        self.assertNotIn("token=secret", rendered)
        self.assertNotIn("password=secret", rendered)
        self.assertNotIn(self.LEAKING_EXCEPTION_TEXT, rendered)

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

    async def test_get_status_uses_explicit_public_collector_allowlist(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="https://history.example.test", timeout_seconds=10)
        )
        collector: dict[str, Any] = {
            "collector_running": True,
            "last_success_at": "2026-09-06T10:00:00+00:00",
            "last_completed_at": "2026-09-06T09:59:00+00:00",
            "source_base_url": "https://collector.status-leak-ZXQ9.example.test",
            "sqlite_path": "/synthetic/private/status-leak-ZXQ9/history.db",
            "collection_stage_timings": [{"error": "status-leak-ZXQ9"}],
            "future_internal_metadata": "status-leak-ZXQ9",
        }

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(return_value={"status": "ok", "collector": collector}),
        ):
            payload = await client.get_status()

        self.assertEqual(
            payload["collector"],
            {
                "collector_running": True,
                "last_success_at": "2026-09-06T10:00:00+00:00",
                "last_completed_at": "2026-09-06T09:59:00+00:00",
            },
        )
        self.assertNotIn("status-leak-ZXQ9", json.dumps(payload))

    async def test_get_status_degraded_without_collector_error_adds_only_stable_error(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="https://history.example.test", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_fetch_json",
            AsyncMock(
                return_value={
                    "status": "degraded",
                    "collector": {
                        "last_success_at": "2026-09-06T10:00:00+00:00",
                        "future_internal_metadata": "status-leak-ZXQ9",
                    },
                }
            ),
        ):
            payload = await client.get_status()

        self.assertEqual(payload["detail"], HISTORY_BACKEND_DEGRADED_DETAIL)
        self.assertEqual(
            payload["collector"],
            {
                "last_success_at": "2026-09-06T10:00:00+00:00",
                "last_error": HISTORY_BACKEND_DEGRADED_DETAIL,
            },
        )
        self.assertNotIn("status-leak-ZXQ9", json.dumps(payload))

    async def test_get_status_malformed_collector_fails_closed(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="https://history.example.test", timeout_seconds=10)
        )

        for malformed in ([{"collector_running": True}], "status-leak-ZXQ9", None):
            with (
                self.subTest(malformed=malformed),
                patch.object(
                    client,
                    "_fetch_json",
                    AsyncMock(return_value={"status": "ok", "collector": malformed}),
                ),
            ):
                payload = await client.get_status()
                self.assertEqual(payload["collector"], {})
                self.assertNotIn("status-leak-ZXQ9", json.dumps(payload))

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

    async def test_get_status_warning_omits_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        error = RuntimeError(self.LEAKING_EXCEPTION_TEXT)

        with (
            patch.object(client, "_fetch_json", AsyncMock(side_effect=error)),
            self.assertLogs("app.services.history_backend", level="WARNING") as captured,
        ):
            payload = await client.get_status()

        self.assertFalse(payload["available"])
        self.assertEqual(payload["detail"], HISTORY_BACKEND_FAILURE_DETAIL)
        self.assert_single_safe_warning(captured, "History backend status request failed.")

    async def test_get_slot_history_warning_omits_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        error = RuntimeError(self.LEAKING_EXCEPTION_TEXT)

        with (
            patch.object(client, "_fetch_json", AsyncMock(side_effect=error)),
            self.assertLogs("app.services.history_backend", level="WARNING") as captured,
        ):
            payload = await client.get_slot_history(5, "archive-core", "front", window_hours=24)

        self.assertFalse(payload["available"])
        self.assertEqual(payload["detail"], HISTORY_BACKEND_FAILURE_DETAIL)
        self.assert_single_safe_warning(captured, "History backend slot history request failed.")

    async def test_multi_scope_warning_omits_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        error = HistoryBackendUnavailableError(self.LEAKING_EXCEPTION_TEXT)

        with (
            patch.object(client, "_send_json", AsyncMock(side_effect=error)),
            self.assertLogs("app.services.history_backend", level="WARNING") as captured,
        ):
            payload = await client.get_scopes_history(
                scopes=[{"system_id": "archive-core", "enclosure_id": "front", "slots": [5]}],
                since="2026-04-15T23:10:00+00:00",
                metrics=["temperature_c"],
                event_limit=12,
                metric_limit=60,
            )

        self.assertFalse(payload["available"])
        self.assertEqual(payload["detail"], HISTORY_BACKEND_FAILURE_DETAIL)
        self.assert_single_safe_warning(captured, "History backend multi-scope request failed.")

    async def test_get_scope_history_warning_omits_backend_exception_details(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )
        error = HistoryBackendUnavailableError(self.LEAKING_EXCEPTION_TEXT)

        with (
            patch.object(client, "get_scopes_history", AsyncMock(side_effect=error)),
            patch.object(client, "_build_since_isoformat", return_value="2026-04-15T23:10:00+00:00"),
            self.assertLogs("app.services.history_backend", level="WARNING") as captured,
        ):
            payload = await client.get_scope_history(
                system_id="archive-core",
                enclosure_id="front",
                slots=[5],
                window_hours=24,
            )

        self.assertFalse(payload[5]["available"])
        self.assertEqual(payload[5]["detail"], HISTORY_BACKEND_FAILURE_DETAIL)
        self.assert_single_safe_warning(captured, "History backend scope history request failed.")

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
            "_send_json",
            AsyncMock(
                return_value={
                    "scopes": [{
                        "system_id": "archive-core",
                        "enclosure_id": "front",
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
                        },
                    }]
                }
            ),
        ) as send_json:
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
        send_json.assert_awaited_once()

    async def test_get_scope_history_can_request_only_needed_metrics(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        with patch.object(
            client,
            "_send_json",
            AsyncMock(return_value={"scopes": [{"histories": {"5": {"slot": 5, "metrics": {"bytes_written": []}}}}]}),
        ) as send_json:
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

        send_json.assert_awaited_once()
        document = send_json.await_args.args[1]
        self.assertEqual(document["metrics"], ["bytes_written"])
        self.assertEqual(document["event_limit"], 0)

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

