"""History reads admit real bay and enclosure counts (Refs #1310).

One SAS expander can carry up to 1,024 disks and a system can have many
enclosures, so the bay-count and scope-count limits only stop runaway requests.
The row, event, request-byte and response-byte budgets keep protecting the work
and the reply size. A refusal names the limit and the requested count.
Synthetic planner and route inputs only; no history database or server.
"""

from __future__ import annotations

import asyncio
import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

from fastapi import HTTPException
from starlette.requests import Request as StarletteRequest

from app import main as app_main
from app import routes as app_routes
from app.config import HistoryConfig
from app.services.history_backend import HistoryBackendClient, HistoryBackendPolicyError
from history_service import main as history_main
from history_service import operation_bounds
from history_service.operation_bounds import (
    HistoryBudgetExceeded,
    build_history_read_plan,
    validate_store_scope_request,
)

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
SINCE = (NOW - timedelta(hours=1)).isoformat()
FULL_EXPANDER = 1024


def _plan(scopes, *, metrics=("temperature_c",), metric_limit=1, event_limit=0):
    return build_history_read_plan(
        scopes=scopes,
        metrics=list(metrics),
        since=SINCE,
        event_limit=event_limit,
        metric_limit=metric_limit,
        now=NOW,
    )


class HistoryHardwareScalePlannerTests(unittest.TestCase):
    def test_one_full_expander_scope_is_admitted(self) -> None:
        plan = _plan([{"system_id": "synthetic", "enclosure_id": "shelf", "slots": list(range(FULL_EXPANDER))}])

        self.assertEqual(plan.target_count, FULL_EXPANDER)
        self.assertEqual(plan.projected_rows, FULL_EXPANDER)

    def test_heatmap_shaped_full_expander_read_is_admitted(self) -> None:
        # The heat map asks for one metric, no events and 24 samples per bay.
        plan = _plan(
            [{"system_id": "synthetic", "enclosure_id": "shelf", "slots": list(range(FULL_EXPANDER))}],
            metric_limit=24,
        )

        self.assertEqual(plan.projected_rows, 24 * FULL_EXPANDER)

    def test_many_enclosure_scopes_are_admitted(self) -> None:
        scopes = [
            {"system_id": "synthetic", "enclosure_id": f"shelf-{index}", "slots": [0]}
            for index in range(128)
        ]

        plan = _plan(scopes)

        self.assertEqual(plan.scope_count, 128)

    def test_count_limits_are_far_above_real_hardware(self) -> None:
        self.assertGreaterEqual(operation_bounds.MAX_TARGETS, 16 * FULL_EXPANDER)
        self.assertGreaterEqual(operation_bounds.MAX_SCOPES, 256)

    def test_row_budget_still_refuses_an_oversized_read(self) -> None:
        with self.assertRaises(HistoryBudgetExceeded) as raised:
            _plan(
                [{"system_id": "synthetic", "enclosure_id": "shelf", "slots": list(range(FULL_EXPANDER))}],
                metrics=("temperature_c", "bytes_read"),
                metric_limit=24,
            )

        self.assertEqual(raised.exception.limit_name, "projected_rows")
        self.assertEqual(raised.exception.value, 2 * 24 * FULL_EXPANDER)

    def test_refusals_name_the_limit_and_the_requested_count(self) -> None:
        target_limit = operation_bounds.MAX_TARGETS
        with self.assertRaises(HistoryBudgetExceeded) as raised:
            _plan([{"system_id": "synthetic", "enclosure_id": "shelf", "slots": list(range(target_limit + 1))}])
        self.assertEqual(raised.exception.limit_name, "target_count")
        self.assertEqual(
            str(raised.exception),
            f"History request exceeds target_count limit ({target_limit}); {target_limit + 1} requested.",
        )

        scope_limit = operation_bounds.MAX_SCOPES
        scopes = [
            {"system_id": "synthetic", "enclosure_id": f"shelf-{index}", "slots": [0]}
            for index in range(scope_limit + 1)
        ]
        with self.assertRaises(HistoryBudgetExceeded) as raised:
            _plan(scopes)
        self.assertEqual(raised.exception.limit_name, "scope_count")
        self.assertIn(f"({scope_limit}); {scope_limit + 1} requested.", str(raised.exception))

    def test_store_admits_a_full_expander_scope(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        normalized = validate_store_scope_request(
            slots=list(range(FULL_EXPANDER)),
            event_limit=0,
            metric_limits={"temperature_c": 24},
            since=since,
        )

        self.assertTrue(normalized)


class HistoryHardwareScaleRouteTests(unittest.TestCase):
    def _scope_route(self):
        return next(route for route in app_main.app.routes if getattr(route, "path", None) == "/api/history/scope")

    def _call(self, slots: list[int], backend: Mock):
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="scale"))
        registry = Mock()
        registry.get_service.return_value = service
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "resolve_read_layout_slots", AsyncMock(return_value=(None, "unavailable"))),
            patch.object(app_routes, "get_history_backend", return_value=backend),
        ):
            return asyncio.run(
                self._scope_route().endpoint(
                    system_id=None,
                    enclosure_id="shelf",
                    slots=slots,
                    window_hours=24,
                    metrics=["temperature_c"],
                    event_limit=0,
                    metric_limit=24,
                )
            )

    def test_heatmap_scope_route_reads_a_full_expander(self) -> None:
        backend = Mock(configured=True)
        backend.get_scope_history = AsyncMock(return_value={0: {"available": True}})

        response = self._call(list(range(FULL_EXPANDER)), backend)

        self.assertEqual(response.status_code, 200)
        backend.get_scope_history.assert_awaited_once()
        self.assertEqual(backend.get_scope_history.await_args.kwargs["slots"], list(range(FULL_EXPANDER)))

    def test_scope_route_refusal_names_the_limit_and_the_requested_count(self) -> None:
        backend = Mock(configured=True)
        backend.get_scope_history = AsyncMock()
        limit = operation_bounds.MAX_TARGETS

        with self.assertRaises(HTTPException) as raised:
            self._call(list(range(limit + 1)), backend)

        self.assertEqual(raised.exception.status_code, 413)
        self.assertIn(str(limit), raised.exception.detail)
        self.assertIn(f"{limit + 1} requested", raised.exception.detail)
        backend.get_scope_history.assert_not_awaited()


# A read that passes the count and row budgets can still be larger than the
# history service's request-byte budget: 14,000 bays with one metric, one
# sample and no events serialize to more than 64 KiB.
OVER_BYTE_BUDGET_SLOTS = 14000


def _asgi_get(app, path: str, params: list[tuple[str, str]]):
    """Drive one GET through the real ASGI application, with no socket or lifespan."""

    async def invoke():
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                await asyncio.Event().wait()
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": urlencode(params).encode(), "root_path": "",
            "headers": [(b"host", b"app.example.test")],
            "client": ("127.0.0.1", 12345), "server": ("app.example.test", 80),
        }
        try:
            await app(scope, receive, send)
        except Exception:
            # The server-error middleware sends its 500 and then re-raises.
            if not any(message["type"] == "http.response.start" for message in messages):
                raise
        return messages

    messages = asyncio.run(invoke())
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body.decode("utf-8", "replace")


class HistoryRequestByteBudgetTests(unittest.TestCase):
    """An over-budget read is a counted 413, never a generic server error (Refs #1310)."""

    def setUp(self) -> None:
        self.client = HistoryBackendClient(HistoryConfig(service_url="http://history.example.test"))
        self.transport_bodies: list[int] = []

    def _sidecar_dispatch(self, request, timeout):
        """Hand the client's request to the real history-service handler, in process."""
        body = request.data
        self.transport_bodies.append(len(body))
        sent = False

        async def receive():
            nonlocal sent
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/api/history/scopes/bundle",
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            "query_string": b"",
        }
        response = asyncio.run(history_main.scopes_history_bundle(StarletteRequest(scope, receive)))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "synthetic", {}, io.BytesIO(response.body)
            )
        reply = Mock()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=None)
        reply.read.return_value = response.body
        reply.headers = {}
        return reply

    @staticmethod
    def _store():
        store = Mock()

        def list_scope_history(_system_id, _enclosure_id, *, slots, **_kwargs):
            return {slot: {"events": [], "metrics": {}, "latest_values": {}, "sample_counts": {}} for slot in slots}

        store.list_scope_history.side_effect = list_scope_history
        return store

    def _scope_request(self, slot_count: int, *, urlopen):
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="scale"))
        registry = Mock()
        registry.get_service.return_value = service
        params = [
            ("enclosure_id", "shelf"), ("window_hours", "24"), ("metrics", "temperature_c"),
            ("event_limit", "0"), ("metric_limit", "1"),
        ] + [("slots", str(slot)) for slot in range(slot_count)]
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "resolve_read_layout_slots", AsyncMock(return_value=(None, "unavailable"))),
            patch.object(app_routes, "get_history_backend", return_value=self.client),
            patch.object(history_main, "store", self._store()),
            patch("app.services.history_backend.urllib.request.urlopen", side_effect=urlopen) as sent,
        ):
            status, text = _asgi_get(app_main.app, "/api/history/scope", params)
        return status, text, sent

    def test_client_refuses_an_over_budget_request_before_sending(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with patch("app.services.history_backend.urllib.request.urlopen") as sent:
            with self.assertRaises(HistoryBudgetExceeded) as raised:
                asyncio.run(
                    self.client.get_scopes_history(
                        scopes=[{"system_id": "synthetic", "enclosure_id": "shelf", "slots": list(range(OVER_BYTE_BUDGET_SLOTS))}],
                        since=since,
                        metrics=["temperature_c"],
                        event_limit=0,
                        metric_limit=1,
                    )
                )

        sent.assert_not_called()
        self.assertEqual(raised.exception.limit_name, "request_bytes")
        self.assertEqual(raised.exception.limit, operation_bounds.MAX_REQUEST_BYTES)
        self.assertGreater(raised.exception.value, operation_bounds.MAX_REQUEST_BYTES)
        self.assertEqual(
            str(raised.exception),
            f"History request exceeds request_bytes limit ({operation_bounds.MAX_REQUEST_BYTES}); "
            f"{raised.exception.value} requested.",
        )

    def test_scope_route_over_byte_budget_is_a_counted_413_without_transport(self) -> None:
        status, text, sent = self._scope_request(OVER_BYTE_BUDGET_SLOTS, urlopen=self._sidecar_dispatch)

        self.assertEqual(status, 413, text)
        detail = json.loads(text)["detail"]
        self.assertIn("request_bytes", detail)
        self.assertIn(f"({operation_bounds.MAX_REQUEST_BYTES})", detail)
        self.assertRegex(detail, r"; \d+ requested\.$")
        self.assertGreater(int(detail.rsplit("; ", 1)[1].split()[0]), operation_bounds.MAX_REQUEST_BYTES)
        sent.assert_not_called()
        self.assertEqual(self.transport_bodies, [])

    def test_scope_route_full_expander_control_reaches_the_service(self) -> None:
        status, text, sent = self._scope_request(FULL_EXPANDER, urlopen=self._sidecar_dispatch)

        self.assertEqual(status, 200, text)
        self.assertEqual(len(json.loads(text)["histories"]), FULL_EXPANDER)
        sent.assert_called_once()
        self.assertEqual(len(self.transport_bodies), 1)
        self.assertLessEqual(self.transport_bodies[0], operation_bounds.MAX_REQUEST_BYTES)

    def test_scope_route_service_policy_refusal_is_a_counted_413(self) -> None:
        def refuse(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 413, "synthetic", {}, io.BytesIO(b"{}"))

        status, text, sent = self._scope_request(FULL_EXPANDER, urlopen=refuse)

        self.assertEqual(status, 413, text)
        detail = json.loads(text)["detail"]
        self.assertIn("history service refused", detail)
        self.assertIn(f"{FULL_EXPANDER} slots", detail)
        sent.assert_called_once()

    def test_storage_view_route_service_policy_refusal_is_a_counted_413(self) -> None:
        route = next(
            route for route in app_main.app.routes
            if getattr(route, "path", None) == "/api/storage-views/{view_id}/history"
        )
        runtime_view = SimpleNamespace(
            id="view",
            source="selected_enclosure_snapshot",
            backing_enclosure_id="shelf",
            slots=[
                SimpleNamespace(slot_index=index, snapshot_slot=index, snapshot_enclosure_id="shelf")
                for index in range(3)
            ],
        )
        service = Mock()
        service.system = SimpleNamespace(id="synthetic", truenas=SimpleNamespace(platform="scale"))
        service.get_storage_view_runtime = AsyncMock(return_value=SimpleNamespace(views=[runtime_view]))
        registry = Mock()
        registry.get_service.return_value = service
        backend = Mock(configured=True)
        backend.get_scopes_history = AsyncMock(side_effect=HistoryBackendPolicyError(413))
        with (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "get_history_backend", return_value=backend),
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    route.endpoint(
                        view_id="view",
                        system_id=None,
                        enclosure_id=None,
                        window_hours=24,
                        metrics=["temperature_c"],
                        event_limit=0,
                        metric_limit=1,
                    )
                )

        self.assertEqual(raised.exception.status_code, 413)
        self.assertIn("history service refused", raised.exception.detail)
        self.assertIn("3 slots", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
