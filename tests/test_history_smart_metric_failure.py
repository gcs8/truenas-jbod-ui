"""SMART metric failures stay visible and retryable (#1205).

A history pass that cannot read SMART metrics must not report that work as
collected. Healthy scopes keep their own samples, but failed scopes stay
degraded and are retried on the next pass instead of waiting a whole cadence.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
from contextlib import closing
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from history_service.collector import HistoryCollector, ScopeSnapshot
from history_service.config import HistorySettings
from history_service.diagnostics import HistorySourceError
from history_service.store import HistoryStore
from history_service.domain import isoformat_utc, utcnow

TOTAL_FAILURE_REASON = "No history SMART metrics could be collected; every SMART request failed."
PARTIAL_FAILURE_REASON = "Some history SMART metric requests failed; metric collection was partial."


def _scope(system_id: str, enclosure_id: str, device: str) -> ScopeSnapshot:
    return ScopeSnapshot(
        system_id=system_id,
        system_label=f"{system_id} label",
        enclosure_id=enclosure_id,
        enclosure_label="Front Shelf",
        snapshot={
            "selected_system_id": system_id,
            "selected_system_label": f"{system_id} label",
            "selected_enclosure_id": enclosure_id,
            "selected_enclosure_label": "Front Shelf",
            "slots": [
                {
                    "slot": 0,
                    "present": True,
                    "serial": f"SERIAL-{device}",
                    "device_name": device,
                    "state": "OK",
                }
            ],
        },
    )


def _summary(temperature: int = 31) -> dict[int, dict[str, Any]]:
    return {
        0: {
            "available": True,
            "temperature_c": temperature,
            "power_on_hours": 1200,
            "bytes_read": 10,
            "bytes_written": 20,
        }
    }


class SmartMetricFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.store = HistoryStore(str(self.temp_dir / "history.db"))
        self.store.create_backup = MagicMock(return_value=None)  # type: ignore[method-assign]
        self.collector = HistoryCollector(
            HistorySettings(
                sqlite_path=str(self.temp_dir / "history.db"),
                backup_dir=str(self.temp_dir / "backups"),
                startup_grace_seconds=0,
                release_check_enabled=False,
            ),
            self.store,
        )
        self.collector._run_retention_if_due = MagicMock()  # type: ignore[method-assign]
        self.healthy = _scope("healthy-system", "enc-a", "da0")
        self.failing = _scope("failing-system", "enc-b", "da1")

    def _metric_rows(self, system_id: str | None = None) -> int:
        if system_id is None:
            return self.store.counts()["metric_sample_count"]
        with closing(sqlite3.connect(self.temp_dir / "history.db")) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM metric_samples WHERE system_id = ?", (system_id,)
                ).fetchone()[0]
            )

    def _smart_side_effect(self, failing_ids: set[str]):
        async def fetch(scope: ScopeSnapshot, present_slots: list[int], *, force_fresh: bool):
            if scope.system_id in failing_ids:
                raise RuntimeError("synthetic SMART batch timed out")
            return _summary()

        return fetch

    def test_total_smart_failure_is_not_recorded_as_success(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.failing])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )

        with self.assertRaises(HistorySourceError) as raised:
            asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))

        self.assertEqual(str(raised.exception), TOTAL_FAILURE_REASON)
        status = self.collector.status()
        self.assertIsNone(status["last_success_at"])
        self.assertIsNone(status["last_fast_metrics_at"])
        self.assertIsNone(status["last_slow_metrics_at"])
        self.assertEqual(self._metric_rows(), 0)
        self.assertEqual(self.collector.degraded_reason(), TOTAL_FAILURE_REASON)
        failed_stage = next(
            entry for entry in self.collector.last_collection_stage_timings if entry["stage"] == "smart.failed"
        )
        self.assertEqual(failed_stage["system_id"], "failing-system")
        self.assertTrue(failed_stage["force_fresh"])
        self.assertIn("timed out", failed_stage["error"])

    def test_total_smart_failure_is_retried_and_recovers_on_the_next_due_pass(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.failing])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )
        try:
            asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
        except HistorySourceError:
            pass  # The failed pass itself is covered above; this test is about the retry.

        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.collector._fetch_smart_summaries.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertTrue(self.collector._fetch_smart_summaries.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertGreater(self._metric_rows("failing-system"), 0)
        status = self.collector.status()
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNotNone(status["last_fast_metrics_at"])
        self.assertIsNotNone(status["last_slow_metrics_at"])
        self.assertIsNone(self.collector.degraded_reason())

    def test_failing_scope_does_not_block_healthy_sibling_and_stays_degraded(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )

        asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))

        status = self.collector.status()
        self.assertIsNotNone(status["last_success_at"])
        self.assertIsNotNone(status["last_fast_metrics_at"])
        self.assertIsNotNone(status["last_slow_metrics_at"])
        self.assertGreater(self._metric_rows("healthy-system"), 0)
        self.assertEqual(self._metric_rows("failing-system"), 0)
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

    def test_failed_scope_is_retried_before_its_cadence_is_due_again(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )
        asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
        fast_cadence = self.collector.last_fast_metrics_at
        slow_cadence = self.collector.last_slow_metrics_at

        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        asyncio.run(self.collector.run_once(include_due_intervals=True))

        fetch = self.collector._fetch_smart_summaries
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[0].system_id, "failing-system")  # type: ignore[attr-defined]
        self.assertTrue(fetch.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertGreater(self._metric_rows("failing-system"), 0)
        # The retry covers the failed scope only; it is not a new cadence pass.
        self.assertEqual(self.collector.last_fast_metrics_at, fast_cadence)
        self.assertEqual(self.collector.last_slow_metrics_at, slow_cadence)
        self.assertIsNone(self.collector.degraded_reason())

    def test_failed_scope_that_leaves_a_complete_census_stops_degrading(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )
        asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertIsNone(self.collector.degraded_reason())

    def test_quick_refresh_does_not_spend_a_pending_slow_retry(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )
        asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))

        # A Quick refresh is fast-only and must never request a fresh SMART read.
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        asyncio.run(
            self.collector.run_once(
                force_fast=True, force_slow=False, include_due_intervals=False, cached_root_only=True
            )
        )

        fetch = self.collector._fetch_smart_summaries
        self.assertTrue(fetch.await_args_list)  # type: ignore[attr-defined]
        self.assertTrue(all(not call.kwargs["force_fresh"] for call in fetch.await_args_list))  # type: ignore[attr-defined]
        # The slow debt is still owed, so the collector stays degraded.
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        # The next scheduled pass pays the slow debt with a fresh read.
        fetch.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(self.collector.run_once(include_due_intervals=True))

        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[0].system_id, "failing-system")  # type: ignore[attr-defined]
        self.assertTrue(fetch.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertIsNone(self.collector.degraded_reason())

    def _leave_failing_scope_owing_a_retry(self, started: datetime) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._smart_side_effect({"failing-system"})
        )
        with patch("history_service.collector.utcnow", return_value=started):
            asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

    def _run_one_scheduled_pass(self, now: datetime) -> list[str]:
        """Drive one real scheduler pass and return the run results it reported."""

        results: list[str] = []

        def observe(**kwargs: Any) -> None:
            results.append(kwargs["result"])

        async def stop_after_first_pass() -> None:
            self.collector._stopping.set()
            raise asyncio.TimeoutError

        with (
            patch("history_service.collector.utcnow", return_value=now),
            patch("history_service.collector.observe_history_collection_run", side_effect=observe),
            patch.object(self.collector._stopping, "wait", side_effect=stop_after_first_pass),
            patch("history_service.collector.logger"),
        ):
            asyncio.run(asyncio.wait_for(self.collector._run_loop(), timeout=30))
        return results

    def test_scheduled_retry_that_fails_again_is_not_recorded_as_success(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_failing_scope_owing_a_retry(started)
        success_at = self.collector.last_success_at
        fast_cadence = self.collector.last_fast_metrics_at
        slow_cadence = self.collector.last_slow_metrics_at
        rows = self._metric_rows()

        # Neither shared cadence is due one second later: only the debt is retried.
        self.collector._fetch_smart_summaries.reset_mock()  # type: ignore[attr-defined]
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            with self.assertRaises(HistorySourceError) as raised:
                asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.assertEqual(str(raised.exception), TOTAL_FAILURE_REASON)
        fetch = self.collector._fetch_smart_summaries
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[0].system_id, "failing-system")  # type: ignore[attr-defined]
        self.assertEqual(self.collector.last_success_at, success_at)
        self.assertEqual(self.collector.last_fast_metrics_at, fast_cadence)
        self.assertEqual(self.collector.last_slow_metrics_at, slow_cadence)
        self.assertEqual(self._metric_rows(), rows)
        self.assertEqual(self.collector.degraded_reason(), TOTAL_FAILURE_REASON)
        # The debt survives the failed retry and is paid by the next good one.
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=2)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))
        self.collector._fetch_smart_summaries.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertTrue(self.collector._fetch_smart_summaries.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertGreater(self._metric_rows("failing-system"), 0)
        self.assertIsNone(self.collector.degraded_reason())

    def test_scheduler_counts_a_failed_retry_only_pass_as_a_failure(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_failing_scope_owing_a_retry(started)
        success_at = self.collector.last_success_at

        results = self._run_one_scheduled_pass(started + timedelta(seconds=1))

        self.assertEqual(results, ["error"])
        self.assertEqual(self.collector.background_consecutive_failures, 1)
        self.assertEqual(self.collector.last_success_at, success_at)
        self.assertEqual(self.collector.last_error, TOTAL_FAILURE_REASON)
        self.assertEqual(self.collector.degraded_reason(), "The last background collection failed.")

    def test_control_scheduler_records_a_successful_retry_only_pass(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_failing_scope_owing_a_retry(started)
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]

        results = self._run_one_scheduled_pass(started + timedelta(seconds=1))

        self.assertEqual(results, ["success"])
        self.collector._fetch_smart_summaries.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(self.collector.background_consecutive_failures, 0)
        self.assertEqual(self.collector.last_success_at, isoformat_utc(started + timedelta(seconds=1)))
        self.assertGreater(self._metric_rows("failing-system"), 0)
        self.assertIsNone(self.collector.degraded_reason())

    def _failing_scope_with_bay(self, *, present: bool, presence_known: bool) -> ScopeSnapshot:
        scope = _scope("failing-system", "enc-b", "da1")
        slot = scope.snapshot["slots"][0]
        slot["present"] = present
        slot["presence_known"] = presence_known
        if not presence_known:
            slot["state"] = "unknown"
        return scope

    def test_unread_bay_occupancy_keeps_the_retry_debt_until_a_real_read(self) -> None:
        # Refs #1295: a pass that could not read the bay's occupancy read no
        # SMART either, so it must not pay or forget the scope's retry debt.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_failing_scope_owing_a_retry(started)
        self.assertEqual(
            self.collector._pending_smart_metric_scopes.get(("failing-system", "enc-b")),
            frozenset({"fast", "slow"}),
        )

        unread = self._failing_scope_with_bay(present=False, presence_known=False)
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy, unread])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertEqual(
            self.collector._pending_smart_metric_scopes.get(("failing-system", "enc-b")),
            frozenset({"fast", "slow"}),
        )
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        # Occupancy is readable again before either cadence is due: the debt
        # is paid by a fresh read, and only then does degradation clear.
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=2)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))

        fetch = self.collector._fetch_smart_summaries
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[0].system_id, "failing-system")  # type: ignore[attr-defined]
        self.assertTrue(fetch.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertGreater(self._metric_rows("failing-system"), 0)
        self.assertNotIn(("failing-system", "enc-b"), self.collector._pending_smart_metric_scopes)
        self.assertIsNone(self.collector.degraded_reason())

    def test_control_confirmed_empty_bay_settles_the_retry_debt(self) -> None:
        # A bay read as empty is authoritative absence: nothing is left to read.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_failing_scope_owing_a_retry(started)

        empty = self._failing_scope_with_bay(present=False, presence_known=True)
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy, empty])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertNotIn(("failing-system", "enc-b"), self.collector._pending_smart_metric_scopes)
        self.assertIsNone(self.collector.degraded_reason())

    def _two_bay_failing_scope(self, *, second_bay: str) -> ScopeSnapshot:
        """Failing scope with bay 0 present and bay 1 present, empty or unread."""

        scope = _scope("failing-system", "enc-b", "da1")
        second = {
            "slot": 1,
            "present": second_bay != "empty",
            "serial": "SERIAL-da2",
            "device_name": "da2",
            "state": "OK",
        }
        if second_bay == "unread":
            second["present"] = False
            second["presence_known"] = False
            second["state"] = "unknown"
        elif second_bay == "empty":
            second["presence_known"] = True
            second["serial"] = None
            second["device_name"] = None
        scope.snapshot["slots"].append(second)
        return scope

    def _two_bay_smart_side_effect(self, failing_ids: set[str]):
        async def fetch(scope: ScopeSnapshot, present_slots: list[int], *, force_fresh: bool):
            if scope.system_id in failing_ids:
                raise RuntimeError("synthetic SMART batch timed out")
            return {slot: _summary()[0] for slot in present_slots}

        return fetch

    def _leave_both_bays_owing_a_retry(self, started: datetime) -> None:
        both = self._two_bay_failing_scope(second_bay="present")
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy, both])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._two_bay_smart_side_effect({"failing-system"})
        )
        with patch("history_service.collector.utcnow", return_value=started):
            asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)
        self.assertEqual(
            self.collector._pending_smart_metric_scopes.get(("failing-system", "enc-b")),
            frozenset({"fast", "slow"}),
        )

    def test_read_of_one_bay_keeps_the_retry_debt_of_an_unread_sibling(self) -> None:
        # Refs #1401: both bays owe a failed SMART read. The next pass reads
        # bay 0 but cannot read bay 1's occupancy, so bay 1 was not read and
        # its debt and the degradation it causes must stay.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        sibling_unread = self._two_bay_failing_scope(second_bay="unread")
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, sibling_unread]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._two_bay_smart_side_effect(set())
        )
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))

        fetch = self.collector._fetch_smart_summaries
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[1], [0])  # type: ignore[attr-defined]
        self.assertTrue(fetch.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertEqual(
            self.collector._pending_smart_metric_scopes.get(("failing-system", "enc-b")),
            frozenset({"fast", "slow"}),
        )
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        # Bay 0's debt is paid: while bay 1 stays unread there is nothing this
        # scope can pay, so no further SMART read is forced for bay 0.
        fetch.reset_mock()  # type: ignore[attr-defined]
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=2)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))
        fetch.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        # Bay 1 is readable again: its own read pays its debt.
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self._two_bay_failing_scope(second_bay="present")]
        )
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=3)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[1], [0, 1])  # type: ignore[attr-defined]
        self.assertTrue(fetch.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertNotIn(("failing-system", "enc-b"), self.collector._pending_smart_metric_scopes)
        self.assertIsNone(self.collector.degraded_reason())

    def test_control_read_of_one_bay_with_an_empty_sibling_clears_the_debt(self) -> None:
        # The sibling was read as empty, so nothing in the scope is left to read.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        sibling_empty = self._two_bay_failing_scope(second_bay="empty")
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, sibling_empty]
        )
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._two_bay_smart_side_effect(set())
        )
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))

        fetch = self.collector._fetch_smart_summaries
        fetch.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(fetch.await_args.args[1], [0])  # type: ignore[attr-defined]
        self.assertNotIn(("failing-system", "enc-b"), self.collector._pending_smart_metric_scopes)
        self.assertIsNone(self.collector.degraded_reason())

    # Per-bay debt transitions (#1401). Each test checks the debt of every bay,
    # not only the scope's union, and ends with the read that pays it.

    BOTH_KINDS = frozenset({"fast", "slow"})
    FAILING_KEY = ("failing-system", "enc-b")

    def _bay_debts(self) -> dict[int, frozenset[str]] | None:
        return self.collector._pending_smart_metric_bays.get(self.FAILING_KEY)

    def _pass(self, scopes: list[ScopeSnapshot], seconds: int, *, incomplete: bool = False, **kwargs: Any) -> None:
        async def enumerate_scopes(**_kwargs: Any) -> list[ScopeSnapshot]:
            if incomplete:
                self.collector._scope_enumeration_complete = False
            return scopes

        self.collector._enumerate_scopes = AsyncMock(side_effect=enumerate_scopes)  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._two_bay_smart_side_effect(set())
        )
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=seconds)):
            asyncio.run(self.collector.run_once(**kwargs))

    def _batch_pass(self, summaries: list[dict[str, Any]], seconds: int) -> AsyncMock:
        """One scheduled pass through the real batch reader; only the HTTP JSON is synthetic."""

        both = self._two_bay_failing_scope(second_bay="present")
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy, both])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = HistoryCollector._fetch_smart_summaries.__get__(  # type: ignore[method-assign]
            self.collector
        )
        fetch_json = AsyncMock(return_value={"summaries": summaries})
        self.collector._fetch_json = fetch_json  # type: ignore[method-assign]
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=seconds)):
            asyncio.run(self.collector.run_once(include_due_intervals=True))
        return fetch_json

    def _assert_batch_retry_pays_both_bays(self, seconds: int) -> None:
        fetch_json = self._batch_pass(
            [{"slot": slot, "summary": _summary()[0]} for slot in (0, 1)], seconds
        )
        fetch_json.assert_awaited_once()
        self.assertEqual(fetch_json.await_args.kwargs["body"], b'{"slots": [0, 1]}')  # type: ignore[union-attr]
        self.assertEqual(fetch_json.await_args.kwargs["params"].get("fresh"), "true")  # type: ignore[union-attr]
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_successful_batch_that_omits_a_bays_summary_keeps_that_bays_debt(self) -> None:
        # A present bay with no summary in a successful batch was not read.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        fetch_json = self._batch_pass([{"slot": 0, "summary": _summary()[0]}], seconds=1)

        fetch_json.assert_awaited_once()
        self.assertEqual(fetch_json.await_args.kwargs["body"], b'{"slots": [0, 1]}')  # type: ignore[union-attr]
        self.assertGreater(self._metric_rows("failing-system"), 0)
        self.assertEqual(self._bay_debts(), {1: self.BOTH_KINDS})
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)
        self._assert_batch_retry_pays_both_bays(seconds=2)

    def test_successful_batch_with_an_unavailable_summary_keeps_that_bays_debt(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        fetch_json = self._batch_pass(
            [
                {"slot": 0, "summary": _summary()[0]},
                {"slot": 1, "summary": {"available": False, "message": "Synthetic SMART read unavailable"}},
            ],
            seconds=1,
        )

        fetch_json.assert_awaited_once()
        self.assertGreater(self._metric_rows("failing-system"), 0)
        self.assertEqual(self._bay_debts(), {1: self.BOTH_KINDS})
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)
        self._assert_batch_retry_pays_both_bays(seconds=2)

    def test_control_successful_batch_with_every_summary_pays_every_bay(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)
        self._assert_batch_retry_pays_both_bays(seconds=1)

    def test_failed_retry_keeps_the_debt_of_an_unread_sibling(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)
        unread = self._two_bay_failing_scope(second_bay="unread")
        self.collector._enumerate_scopes = AsyncMock(return_value=[self.healthy, unread])  # type: ignore[method-assign]
        self.collector._fetch_smart_summaries = AsyncMock(  # type: ignore[method-assign]
            side_effect=self._two_bay_smart_side_effect({"failing-system"})
        )
        with patch("history_service.collector.utcnow", return_value=started + timedelta(seconds=1)):
            asyncio.run(self.collector.run_once(force_fast=True, include_due_intervals=False))

        self.assertEqual(self.collector._fetch_smart_summaries.await_args.args[1], [0])  # type: ignore[attr-defined]
        self.assertEqual(self._bay_debts(), {0: self.BOTH_KINDS, 1: self.BOTH_KINDS})
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        self._pass([self.healthy, self._two_bay_failing_scope(second_bay="present")], seconds=2)
        self.assertTrue(self.collector._fetch_smart_summaries.await_args.kwargs["force_fresh"])  # type: ignore[attr-defined]
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_fast_only_read_pays_only_fast_and_only_read_bays(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        unread = self._two_bay_failing_scope(second_bay="unread")
        self._pass([self.healthy, unread], seconds=1, force_fast=True, include_due_intervals=False)

        self.assertEqual(self._bay_debts(), {0: frozenset({"slow"}), 1: self.BOTH_KINDS})
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

    def test_zero_slot_snapshot_keeps_every_bays_debt(self) -> None:
        # An empty slot list reads no bay as empty; it is not a confirmed-empty scope.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        no_slots = _scope("failing-system", "enc-b", "da1")
        no_slots.snapshot["slots"] = []
        self._pass([no_slots], seconds=1, force_fast=True, include_due_intervals=False)

        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertEqual(self._bay_debts(), {0: self.BOTH_KINDS, 1: self.BOTH_KINDS})
        self.assertIsNotNone(self.collector.degraded_reason())

        self._pass([self.healthy, self._two_bay_failing_scope(second_bay="present")], seconds=2)
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_bay_missing_from_the_slot_list_keeps_its_debt_until_it_is_read(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        only_bay_0 = _scope("failing-system", "enc-b", "da1")
        self._pass([self.healthy, only_bay_0], seconds=1)
        self.collector._fetch_smart_summaries.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(self._bay_debts(), {1: self.BOTH_KINDS})
        self.assertEqual(self.collector.degraded_reason(), PARTIAL_FAILURE_REASON)

        self._pass([self.healthy, only_bay_0], seconds=2)
        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]
        self.assertEqual(self._bay_debts(), {1: self.BOTH_KINDS})

        self._pass([self.healthy, self._two_bay_failing_scope(second_bay="present")], seconds=3)
        self.collector._fetch_smart_summaries.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_cached_root_pass_keeps_the_debt_of_an_absent_scope(self) -> None:
        # A cached-root census is not authoritative about which scopes exist.
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        self._pass([self.healthy], seconds=1, cached_root_only=True, include_due_intervals=False)
        self.assertEqual(self._bay_debts(), {0: self.BOTH_KINDS, 1: self.BOTH_KINDS})
        self.assertIsNotNone(self.collector.degraded_reason())

        # Control: a complete census without the scope settles its debt.
        self._pass([self.healthy], seconds=2)
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_incomplete_census_keeps_the_debt_of_an_absent_scope(self) -> None:
        started = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self._leave_both_bays_owing_a_retry(started)

        self._pass([self.healthy], seconds=1, incomplete=True)
        self.assertEqual(self._bay_debts(), {0: self.BOTH_KINDS, 1: self.BOTH_KINDS})
        self.assertIsNotNone(self.collector.degraded_reason())

        self._pass([self.healthy], seconds=2)
        self.assertIsNone(self._bay_debts())
        self.assertIsNone(self.collector.degraded_reason())

    def test_control_healthy_pass_advances_cadence_without_retry(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]

        asyncio.run(self.collector.run_once(force_fast=True, force_slow=True, include_due_intervals=False))

        self.assertIsNone(self.collector.degraded_reason())
        self.assertIsNotNone(self.collector.last_fast_metrics_at)
        self.assertIsNotNone(self.collector.last_slow_metrics_at)
        self.assertEqual(self.collector._fetch_smart_summaries.await_count, 2)  # type: ignore[attr-defined]

        self.collector._fetch_smart_summaries.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.collector._fetch_smart_summaries.assert_not_awaited()  # type: ignore[attr-defined]

    def test_control_due_cadence_still_collects_every_scope(self) -> None:
        self.collector._enumerate_scopes = AsyncMock(  # type: ignore[method-assign]
            return_value=[self.healthy, self.failing]
        )
        self.collector._fetch_smart_summaries = AsyncMock(side_effect=self._smart_side_effect(set()))  # type: ignore[method-assign]
        stale = isoformat_utc(utcnow() - timedelta(days=1))
        self.collector.last_fast_metrics_at = stale
        self.collector.last_slow_metrics_at = stale

        asyncio.run(self.collector.run_once(include_due_intervals=True))

        self.assertEqual(self.collector._fetch_smart_summaries.await_count, 2)  # type: ignore[attr-defined]
        self.assertNotEqual(self.collector.last_fast_metrics_at, stale)
        self.assertNotEqual(self.collector.last_slow_metrics_at, stale)


if __name__ == "__main__":
    unittest.main()
