"""History reads admit real bay and enclosure counts (Refs #1310).

One SAS expander can carry up to 1,024 disks and a system can have many
enclosures, so the bay-count and scope-count limits only stop runaway requests.
The row, event, request-byte and response-byte budgets keep protecting the work
and the reply size. A refusal names the limit and the requested count.
Synthetic planner and route inputs only; no history database or server.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app import main as app_main
from app import routes as app_routes
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


if __name__ == "__main__":
    unittest.main()
