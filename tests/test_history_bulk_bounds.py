from __future__ import annotations

import asyncio
import json
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from history_service import main as history_main
from history_service.operation_bounds import MAX_RESPONSE_BYTES
from history_service.store import HistoryStore


SINCE = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


class HistoryBulkRouteBoundsTests(unittest.TestCase):
    @staticmethod
    def _request(body: bytes, *, content_type: bytes = b"application/json") -> Request:
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
                "scheme": "http",
                "path": "/api/history/scopes/bundle",
                "raw_path": b"/api/history/scopes/bundle",
                "query_string": b"",
                "headers": [(b"content-type", content_type)],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
            },
            receive,
        )

    def test_single_scope_get_rejects_unbounded_or_unknown_requests_before_store(self) -> None:
        store = Mock()
        cases = (
            {"slots": [], "metrics": ["temperature_c"], "since": SINCE, "event_limit": 0, "metric_limit": 24},
            {"slots": [0], "metrics": ["temperature_c"], "since": None, "event_limit": 0, "metric_limit": 24},
            {"slots": [0], "metrics": ["unknown"], "since": SINCE, "event_limit": 0, "metric_limit": 24},
            {"slots": list(range(348)), "metrics": ["temperature_c"], "since": SINCE, "event_limit": 0, "metric_limit": 24},
        )
        with patch.object(history_main, "store", store):
            for arguments in cases:
                with self.subTest(arguments=arguments), self.assertRaises(HTTPException):
                    asyncio.run(
                        history_main.scope_slot_history(
                            system_id="synthetic",
                            enclosure_id="front",
                            **arguments,
                        )
                    )
        store.list_scope_history.assert_not_called()

    def test_single_scope_get_returns_budget_metadata_and_allowlisted_metrics_only(self) -> None:
        store = Mock(
            list_scope_history=Mock(
                return_value={
                    999999: {
                        "events": [],
                        "metrics": {"temperature_c": [{"observed_at": SINCE, "value": 30}]},
                        "sample_counts": {"temperature_c": 1},
                        "latest_values": {"temperature_c": 30},
                    }
                }
            )
        )
        with patch.object(history_main, "store", store):
            response = asyncio.run(
                history_main.scope_slot_history(
                    system_id="synthetic",
                    enclosure_id="front",
                    slots=[999999],
                    metrics=["temperature_c"],
                    since=SINCE,
                    event_limit=0,
                    metric_limit=24,
                )
            )
        payload = json.loads(response.body)
        self.assertEqual(payload["budget"]["target_count"], 1)
        self.assertEqual(payload["budget"]["returned_row_count"], 1)
        self.assertGreater(payload["budget"]["response_bytes"], 0)
        store.list_scope_history.assert_called_once()
        self.assertEqual(store.list_scope_history.call_args.kwargs["metric_limits"], {"temperature_c": 24})

    def test_multi_scope_body_is_strict_bounded_and_combines_all_scope_work(self) -> None:
        route = next(route for route in history_main.app.routes if route.path == "/api/history/scopes/bundle")
        valid = {
            "scopes": [
                {"system_id": "synthetic", "enclosure_id": "front", "slots": [0]},
                {"system_id": "synthetic", "enclosure_id": "rear", "slots": [5]},
            ],
            "metrics": ["bytes_written"],
            "since": SINCE,
            "event_limit": 0,
            "metric_limit": 24,
        }
        store = Mock(list_scope_history=Mock(return_value={}))
        with patch.object(history_main, "store", store):
            response = asyncio.run(route.endpoint(request=self._request(json.dumps(valid).encode())))
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["budget"]["scope_count"], 2)
        self.assertEqual(payload["budget"]["target_count"], 2)
        self.assertEqual(store.list_scope_history.call_count, 2)

        invalid_bodies = (
            b'{"scopes":[],"metrics":["temperature_c"],"since":"x","event_limit":0,"metric_limit":24}',
            b'{"scopes":[],"scopes":[],"metrics":[],"since":"x","event_limit":0,"metric_limit":24}',
            b'{"scopes":[],"metrics":[],"since":"x","event_limit":0,"metric_limit":24,"extra":true}',
            b"{" + b" " * 65536 + b"}",
        )
        for body in invalid_bodies:
            with self.subTest(body=body[:50]):
                response = asyncio.run(route.endpoint(request=self._request(body)))
                self.assertIn(response.status_code, {413, 422})
        self.assertEqual(store.list_scope_history.call_count, 2)

    def test_exact_serialized_response_overflow_returns_small_413(self) -> None:
        payload = {"histories": {"0": {"events": [], "metrics": {"temperature_c": ["x" * 1000]}}}}
        response = history_main.bounded_history_json_response(payload, max_bytes=100)
        self.assertEqual(response.status_code, 413)
        self.assertLess(len(response.body), 500)
        self.assertNotIn(b"xxxxxxxx", response.body)
        self.assertIn(str(MAX_RESPONSE_BYTES).encode(), response.body)

    def test_store_rejects_unbounded_scope_work_before_opening_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HistoryStore(str(Path(temp_dir) / "history.db"))
            with patch.object(store, "_connect", side_effect=AssertionError("SQLite opened")) as connect:
                for arguments in (
                    {"slots": [], "since": SINCE, "event_limit": 0, "metric_limits": {"temperature_c": 24}},
                    {"slots": [0], "since": None, "event_limit": 0, "metric_limits": {"temperature_c": 24}},
                    {"slots": list(range(347)), "since": SINCE, "event_limit": 0, "metric_limits": {"temperature_c": 96, "bytes_read": 96}},
                ):
                    with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                        store.list_scope_history("synthetic", "front", **arguments)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
