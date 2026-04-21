from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.config import HistoryConfig
from app.services.history_backend import HistoryBackendClient


class HistoryBackendClientTests(unittest.IsolatedAsyncioTestCase):
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
        fetch_json.assert_awaited_once_with("/api/history/overview")

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
                            "annualized_bytes_written": 0,
                            "power_on_hours": 1,
                        },
                        "latest_values": {
                            "temperature_c": 31,
                            "bytes_read": 549755813888,
                            "bytes_written": 1099511627776,
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
        self.assertEqual(payload["sample_counts"]["annualized_bytes_written"], 0)
        self.assertEqual(payload["latest_values"]["temperature_c"], 31)
        self.assertEqual(payload["latest_values"]["bytes_read"], 549755813888)
        self.assertEqual(payload["latest_values"]["bytes_written"], 1099511627776)
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
            payload = await client.get_scope_history(system_id="archive-core", enclosure_id="front", slots=[5, 6])

        self.assertEqual(payload[5]["latest_values"]["temperature_c"], 31)
        self.assertEqual(payload[6]["sample_counts"]["temperature_c"], 0)
        fetch_json.assert_awaited_once()

    async def test_get_scope_history_falls_back_to_per_slot_fetch_on_scope_error(self) -> None:
        client = HistoryBackendClient(
            HistoryConfig(service_url="http://history-backend:8001", timeout_seconds=10)
        )

        per_slot_payload = {
            "configured": True,
            "available": True,
            "detail": None,
            "slot": 5,
            "system_id": "archive-core",
            "enclosure_id": "front",
            "metrics": {},
            "events": [],
            "sample_counts": {},
            "latest_values": {},
        }

        with patch.object(client, "_fetch_json", AsyncMock(side_effect=RuntimeError("boom"))):
            with patch.object(client, "get_slot_history", AsyncMock(return_value=per_slot_payload)) as get_slot_history:
                payload = await client.get_scope_history(system_id="archive-core", enclosure_id="front", slots=[5])

        self.assertEqual(payload[5]["slot"], 5)
        get_slot_history.assert_awaited_once_with(5, "archive-core", "front")
