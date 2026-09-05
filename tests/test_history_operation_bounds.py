from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from history_service.operation_bounds import (
    MAX_EVENT_ROWS,
    MAX_HISTORY_HOURS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_RETURNED_ROWS,
    MAX_SCOPES,
    MAX_TARGETS,
    HistoryBudgetExceeded,
    HistoryRequestShapeError,
    build_history_read_plan,
)


NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)
SINCE = (NOW - timedelta(hours=24)).isoformat()


class HistoryOperationBoundsTests(unittest.TestCase):
    def _plan(self, **overrides):
        arguments = {
            "scopes": [{"system_id": "synthetic", "enclosure_id": "front", "slots": [0]}],
            "metrics": ["temperature_c"],
            "since": SINCE,
            "event_limit": 0,
            "metric_limit": 24,
            "now": NOW,
        }
        arguments.update(overrides)
        return build_history_read_plan(**arguments)

    def test_contract_constants_are_server_owned(self) -> None:
        self.assertEqual(MAX_SCOPES, 32)
        self.assertEqual(MAX_TARGETS, 347)
        self.assertEqual(MAX_EVENT_ROWS, 4096)
        self.assertEqual(MAX_RETURNED_ROWS, 36000)
        self.assertEqual(MAX_HISTORY_HOURS, 8760)
        self.assertEqual(MAX_REQUEST_BYTES, 65536)
        self.assertEqual(MAX_RESPONSE_BYTES, 25165824)

    def test_sparse_high_slot_identifiers_are_valid_but_target_count_is_bounded(self) -> None:
        plan = self._plan(
            scopes=[{"system_id": "synthetic", "enclosure_id": None, "slots": [0, 999_999]}]
        )
        self.assertEqual(plan.target_count, 2)
        with self.assertRaises(HistoryBudgetExceeded) as raised:
            self._plan(scopes=[{"system_id": "synthetic", "slots": list(range(MAX_TARGETS + 1))}])
        self.assertEqual(raised.exception.limit_name, "target_count")

    def test_scope_count_and_duplicate_scope_identity_are_rejected(self) -> None:
        scopes = [
            {"system_id": f"synthetic-{index}", "enclosure_id": "front", "slots": [index]}
            for index in range(MAX_SCOPES + 1)
        ]
        with self.assertRaises(HistoryBudgetExceeded) as raised:
            self._plan(scopes=scopes)
        self.assertEqual(raised.exception.limit_name, "scope_count")

        with self.assertRaises(HistoryRequestShapeError):
            self._plan(
                scopes=[
                    {"system_id": "synthetic", "enclosure_id": "front", "slots": [0]},
                    {"system_id": "synthetic", "enclosure_id": "front", "slots": [1]},
                ]
            )

    def test_raw_slots_and_metrics_are_bounded_before_deduplication(self) -> None:
        with self.assertRaises(HistoryBudgetExceeded):
            self._plan(scopes=[{"system_id": "synthetic", "slots": [0] * (MAX_TARGETS + 1)}])
        with self.assertRaises(HistoryRequestShapeError):
            self._plan(metrics=["temperature_c"] * 7)

    def test_empty_scopes_slots_unknown_metrics_and_negative_slots_are_rejected(self) -> None:
        invalid = (
            {"scopes": []},
            {"scopes": [{"system_id": "synthetic", "slots": []}]},
            {"scopes": [{"system_id": "synthetic", "slots": [-1]}]},
            {"metrics": ["not_a_metric"]},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(HistoryRequestShapeError):
                self._plan(**overrides)

    def test_window_is_timezone_aware_not_future_and_between_one_and_8760_hours(self) -> None:
        for since in (
            None,
            "2030-01-02T11:00:00",
            (NOW + timedelta(seconds=1)).isoformat(),
            (NOW - timedelta(minutes=59)).isoformat(),
            (NOW - timedelta(hours=MAX_HISTORY_HOURS, seconds=1)).isoformat(),
        ):
            with self.subTest(since=since), self.assertRaises(HistoryRequestShapeError):
                self._plan(since=since)
        self.assertEqual(self._plan(since=(NOW - timedelta(hours=1)).isoformat()).window_hours, 1)
        self.assertEqual(
            self._plan(since=(NOW - timedelta(hours=MAX_HISTORY_HOURS)).isoformat()).window_hours,
            MAX_HISTORY_HOURS,
        )

    def test_metric_and_event_ranges_are_strict(self) -> None:
        for metric_limit in (0, 97, True):
            with self.subTest(metric_limit=metric_limit), self.assertRaises(HistoryRequestShapeError):
                self._plan(metric_limit=metric_limit)
        for event_limit in (-1, 4097, True):
            with self.subTest(event_limit=event_limit), self.assertRaises(HistoryRequestShapeError):
                self._plan(event_limit=event_limit)

    def test_aggregate_event_and_projected_row_limits_apply_across_scopes(self) -> None:
        scopes = [
            {"system_id": "synthetic", "enclosure_id": "front", "slots": list(range(174))},
            {"system_id": "synthetic", "enclosure_id": "rear", "slots": list(range(173))},
        ]
        plan = self._plan(scopes=scopes, event_limit=0, metrics=["bytes_read", "bytes_written"], metric_limit=24)
        self.assertEqual(plan.target_count, 347)
        self.assertEqual(plan.projected_rows, 16656)

        with self.assertRaises(HistoryBudgetExceeded) as raised:
            self._plan(scopes=scopes, event_limit=12, metrics=["temperature_c"], metric_limit=1)
        self.assertEqual(raised.exception.limit_name, "event_rows")

        with self.assertRaises(HistoryBudgetExceeded) as raised:
            self._plan(scopes=scopes, event_limit=0, metrics=["temperature_c", "bytes_read"], metric_limit=96)
        self.assertEqual(raised.exception.limit_name, "projected_rows")

    def test_plan_is_immutable_and_exposes_numeric_budget_metadata(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.budget_metadata(), {
            "scope_count": 1,
            "target_count": 1,
            "projected_event_rows": 0,
            "projected_returned_rows": 24,
            "returned_row_count": 0,
            "response_bytes": 0,
        })
        with self.assertRaises((AttributeError, TypeError)):
            plan.target_count = 2  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
