from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

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
        with patch.object(history_main, "JSONResponse", wraps=history_main.JSONResponse) as response_type:
            response = history_main.bounded_history_json_response(payload, max_bytes=100)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response_type.call_count, 1)
        self.assertLess(len(response.body), 500)
        self.assertNotIn(b"xxxxxxxx", response.body)
        self.assertIn(str(MAX_RESPONSE_BYTES).encode(), response.body)

    def test_response_budget_is_exact_with_one_serialization_across_digit_boundaries(self) -> None:
        cases = ((57, 99), (58, 101), (956, 999), (957, 1001))

        for value_length, expected_bytes in cases:
            with self.subTest(expected_bytes=expected_bytes):
                payload = {
                    "data": "x" * value_length,
                    "budget": {"response_bytes": 0},
                }
                with patch.object(
                    history_main,
                    "JSONResponse",
                    wraps=history_main.JSONResponse,
                ) as response_type:
                    response = history_main.bounded_history_json_response(payload)

                document = json.loads(response.body)
                self.assertEqual(response_type.call_count, 1)
                self.assertEqual(len(response.body), expected_bytes)
                self.assertEqual(document["budget"]["response_bytes"], expected_bytes)

    def test_response_budget_counts_real_history_keys_and_escaped_text_exactly(self) -> None:
        payload = {
            "scopes": [{"histories": {5: {"label": "temp\néra"}}}],
            "budget": {"response_bytes": 0},
        }

        response = history_main.bounded_history_json_response(payload)

        document = json.loads(response.body)
        self.assertEqual(document["budget"]["response_bytes"], len(response.body))
        self.assertEqual(document["scopes"][0]["histories"]["5"]["label"], "temp\néra")

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

    def test_hot_scope_history_excludes_events_before_since(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "history.db"
            store = HistoryStore(str(database_path))
            now = datetime.now(timezone.utc)
            since = (now - timedelta(hours=24)).isoformat()
            with sqlite3.connect(database_path) as connection:
                connection.executemany(
                    """
                    INSERT INTO slot_events (
                        observed_at, system_id, enclosure_key, slot, slot_label,
                        event_type, details_json
                    ) VALUES (?, 'synthetic', 'front', 0, '0', 'state', '{}')
                    """,
                    [
                        ((now - timedelta(hours=25)).isoformat(),),
                        ((now - timedelta(hours=1)).isoformat(),),
                    ],
                )

            histories = store.list_scope_history(
                "synthetic",
                "front",
                slots=[0],
                event_limit=2,
                metric_limits={"temperature_c": 1},
                since=since,
            )

            self.assertEqual(
                [event["observed_at"] for event in histories[0]["events"]],
                [(now - timedelta(hours=1)).isoformat()],
            )


class HistoryReadAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def _plan_arguments(self) -> dict[str, Any]:
        return {
            "system_id": "synthetic",
            "enclosure_id": "front",
            "slots": [0],
            "metrics": ["temperature_c"],
            "since": SINCE,
            "event_limit": 0,
            "metric_limit": 1,
        }

    async def test_get_and_post_share_one_at_limit_admission_boundary(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=2)
        entered_count = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_execute(plan):
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            await release.wait()
            return [
                {
                    "system_id": scope.system_id,
                    "enclosure_id": scope.enclosure_id,
                    "histories": {},
                }
                for scope in plan.scopes
            ], 0

        post_route = next(
            route for route in history_main.app.routes
            if route.path == "/api/history/scopes/bundle"
        )
        post_document = {
            "scopes": [{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            "metrics": ["temperature_c"],
            "since": SINCE,
            "event_limit": 0,
            "metric_limit": 1,
        }
        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "_execute_history_plan", AsyncMock(side_effect=blocked_execute)) as execute,
        ):
            get_task = asyncio.create_task(history_main.scope_slot_history(**self._plan_arguments()))
            post_task = asyncio.create_task(
                post_route.endpoint(
                    request=HistoryBulkRouteBoundsTests._request(json.dumps(post_document).encode())
                )
            )
            await both_entered.wait()

            rejected = await history_main.scope_slot_history(**self._plan_arguments())

            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(rejected.headers["retry-after"], "1")
            self.assertEqual(
                json.loads(rejected.body),
                {"detail": "History read capacity is temporarily busy; retry later."},
            )
            self.assertEqual(execute.await_count, 2)
            release.set()
            await asyncio.gather(get_task, post_task)

    async def test_admission_releases_after_success(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=1)
        plan = history_main.build_history_read_plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            metrics=["temperature_c"],
            since=SINCE,
            event_limit=0,
            metric_limit=1,
        )
        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "_execute_history_plan", AsyncMock(return_value=([], 0))) as execute,
        ):
            await history_main._execute_admitted_history_plan(plan)
            await history_main._execute_admitted_history_plan(plan)
        self.assertEqual(execute.await_count, 2)

    async def test_admission_releases_after_exception(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=1)
        plan = history_main.build_history_read_plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            metrics=["temperature_c"],
            since=SINCE,
            event_limit=0,
            metric_limit=1,
        )
        execute = AsyncMock(side_effect=[RuntimeError("synthetic failure"), ([], 0)])
        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "_execute_history_plan", execute),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                await history_main._execute_admitted_history_plan(plan)
            await history_main._execute_admitted_history_plan(plan)
        self.assertEqual(execute.await_count, 2)

    async def test_admission_releases_after_cancellation(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=1)
        plan = history_main.build_history_read_plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            metrics=["temperature_c"],
            since=SINCE,
            event_limit=0,
            metric_limit=1,
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocked_store(*_args, **_kwargs):
            entered.set()
            release.wait()
            finished.set()
            return {}

        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "store", Mock(list_scope_history=blocked_store)),
        ):
            task = asyncio.create_task(history_main._execute_admitted_history_plan(plan))
            await asyncio.to_thread(entered.wait)
            task.cancel()
            event_loop_turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(event_loop_turn.set)
            await event_loop_turn.wait()
            acquired_while_thread_running = admission.try_acquire()
            if acquired_while_thread_running:
                admission.release()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.to_thread(finished.wait)
            self.assertFalse(acquired_while_thread_running)

            for _ in range(100):
                acquired_after_finish = admission.try_acquire()
                if acquired_after_finish:
                    admission.release()
                    break
                await asyncio.sleep(0)
            else:
                self.fail("Admission was not released after the SQLite worker finished")

        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "_execute_history_plan", AsyncMock(return_value=([], 0))),
        ):
            self.assertEqual(await history_main._execute_admitted_history_plan(plan), ([], 0))

    async def test_repeated_cancellation_retains_admission_until_worker_finishes(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=1)
        plan = history_main.build_history_read_plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            metrics=["temperature_c"],
            since=SINCE,
            event_limit=0,
            metric_limit=1,
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocked_store(*_args, **_kwargs):
            entered.set()
            release.wait()
            finished.set()
            return {}

        with (
            patch.object(history_main, "bulk_history_read_admission", admission),
            patch.object(history_main, "store", Mock(list_scope_history=blocked_store)),
        ):
            task = asyncio.create_task(history_main._execute_admitted_history_plan(plan))
            await asyncio.to_thread(entered.wait)
            try:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                acquired_while_thread_running = admission.try_acquire()
                if acquired_while_thread_running:
                    admission.release()
                self.assertFalse(acquired_while_thread_running)
            finally:
                release.set()
                await asyncio.to_thread(finished.wait)

            for _ in range(100):
                acquired_after_finish = admission.try_acquire()
                if acquired_after_finish:
                    admission.release()
                    break
                await asyncio.sleep(0)
            else:
                self.fail("Admission was not released after the SQLite worker finished")


    async def test_canceled_caller_consumes_late_worker_exception(self) -> None:
        admission = history_main.BulkHistoryReadAdmission(max_concurrency=1)
        plan = history_main.build_history_read_plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            metrics=["temperature_c"],
            since=SINCE,
            event_limit=0,
            metric_limit=1,
        )
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        loop_errors: list[dict[str, object]] = []

        def failing_store(*_args, **_kwargs):
            entered.set()
            release.wait()
            finished.set()
            raise RuntimeError("synthetic late worker failure")

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        try:
            with (
                patch.object(history_main, "bulk_history_read_admission", admission),
                patch.object(history_main, "store", Mock(list_scope_history=failing_store)),
            ):
                task = asyncio.create_task(history_main._execute_admitted_history_plan(plan))
                await asyncio.to_thread(entered.wait)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await asyncio.to_thread(finished.wait)
                for _ in range(100):
                    if not history_main.bulk_history_read_operations:
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("Failed bulk-history operation remained retained")
                await asyncio.sleep(0)
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

        self.assertEqual(loop_errors, [])


if __name__ == "__main__":
    unittest.main()
