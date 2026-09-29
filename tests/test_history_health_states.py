"""Health grading, startup state, and plain status-file diagnostics (#456, #441)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from history_service import main as history_main
from history_service.collector import HistoryCollector
from history_service.config import HistorySettings
from history_service.diagnostics import RETENTION_FAILURE_SENTENCES


def _collector(**settings: Any) -> HistoryCollector:
    store = MagicMock()
    store.latest_backup_snapshot_at.return_value = datetime(2026, 7, 1, tzinfo=timezone.utc)
    store.read_retention_wait_anchor.return_value = None
    return HistoryCollector(HistorySettings(**settings), store)


class DegradedDefinitionTests(unittest.TestCase):
    def test_a_healthy_collector_is_not_degraded(self) -> None:
        self.assertIsNone(_collector().degraded_reason())

    def test_a_failed_manual_refresh_alone_is_not_degraded(self) -> None:
        collector = _collector()
        collector.last_error = "History full refresh failed; see service logs."

        self.assertIsNone(collector.degraded_reason())

    def test_a_failed_background_pass_is_degraded(self) -> None:
        collector = _collector()
        collector._record_background_failure(datetime(2026, 7, 1, tzinfo=timezone.utc))

        self.assertEqual(collector.degraded_reason(), "The last background collection failed.")
        collector._clear_background_failure_backoff()
        self.assertIsNone(collector.degraded_reason())

    def test_a_read_only_database_is_degraded(self) -> None:
        collector = _collector()
        collector.store.maintain_retention.side_effect = sqlite3.OperationalError(
            "attempt to write a readonly database"
        )

        collector._run_retention_if_due(datetime(2026, 7, 1, tzinfo=timezone.utc), backup_succeeded=True)

        self.assertEqual(collector.degraded_reason(), "The history database is read-only.")

    def test_cleanup_is_degraded_only_after_two_failures_in_a_row(self) -> None:
        collector = _collector()
        collector.store.maintain_retention.side_effect = sqlite3.OperationalError("disk I/O error")
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)

        collector._run_retention_if_due(now, backup_succeeded=True)
        self.assertIsNone(collector.degraded_reason())

        collector._run_retention_if_due(now, backup_succeeded=True)
        self.assertEqual(collector.degraded_reason(), "History cleanup has failed twice in a row.")
        self.assertEqual(collector.status()["retention_consecutive_failures"], 2)

        collector.store.maintain_retention.side_effect = None
        collector.store.maintain_retention.return_value = {"has_more": False}
        collector._run_retention_if_due(now, backup_succeeded=True)
        self.assertIsNone(collector.degraded_reason())
        self.assertEqual(collector.status()["retention_consecutive_failures"], 0)


class HealthzShapeTests(unittest.TestCase):
    def _healthz(self, collector_status: dict[str, object], degraded: str | None) -> dict[str, object]:
        with (
            patch.object(history_main, "startup_failure_reason", None),
            patch.object(history_main.collector, "status", return_value=collector_status),
            patch.object(history_main.collector, "degraded_reason", return_value=degraded),
            patch.object(history_main.store, "database_size_bytes", return_value=4096),
        ):
            response = asyncio.run(history_main.healthz())
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    def test_healthz_carries_collector_fields_once_and_a_plain_detail(self) -> None:
        payload = self._healthz({"collector_running": True, "last_error": None}, None)

        self.assertEqual(set(payload), {"status", "detail", "collector", "database_size_bytes"})
        self.assertEqual(payload["status"], "ok")
        self.assertIsNone(payload["detail"])

    def test_healthz_reports_why_it_is_degraded(self) -> None:
        payload = self._healthz(
            {"collector_running": True, "last_error": None},
            "History cleanup has failed twice in a row.",
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["detail"], "History cleanup has failed twice in a row.")

    def _healthz_with_size_error(self, error: Exception, degraded: str | None = None) -> dict[str, object]:
        with (
            patch.object(history_main, "startup_failure_reason", None),
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.collector, "degraded_reason", return_value=degraded),
            patch.object(history_main.store, "database_size_bytes", side_effect=error),
        ):
            response = asyncio.run(history_main.healthz())
        self.assertEqual(response.status_code, 200)
        return json.loads(response.body)

    def test_a_missing_segmented_catalog_is_degraded_not_a_crash(self) -> None:
        with patch.object(history_main.store, "segment_catalog_path", Path("/nonexistent-qa/segments/catalog.json")):
            payload = self._healthz_with_size_error(
                FileNotFoundError(2, "No such file", "/nonexistent-qa/segments/catalog.json")
            )

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["detail"], history_main.SEGMENT_CATALOG_MISSING_REASON)
        self.assertIsNone(payload["database_size_bytes"])

    def test_an_unreadable_segmented_catalog_is_degraded_without_internal_text(self) -> None:
        for error in (ValueError("Segmented history migration recovery is pending."), PermissionError(13, "denied")):
            with self.subTest(error=type(error).__name__):
                payload = self._healthz_with_size_error(error)
                self.assertEqual(payload["status"], "degraded")
                self.assertEqual(payload["detail"], history_main.SEGMENT_CATALOG_UNREADABLE_REASON)
                self.assertIsNone(payload["database_size_bytes"])

    def test_a_missing_segment_under_an_existing_catalog_is_not_called_a_fresh_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.json"
            catalog.write_text("{}", encoding="utf-8")
            with patch.object(history_main.store, "segment_catalog_path", catalog):
                payload = self._healthz_with_size_error(FileNotFoundError(2, "No such file", "segment-0001.sqlite"))
        self.assertEqual(payload["detail"], history_main.SEGMENT_CATALOG_UNREADABLE_REASON)

    def test_an_existing_degraded_reason_is_kept_when_sizing_fails(self) -> None:
        payload = self._healthz_with_size_error(FileNotFoundError(), "The last background collection failed.")

        self.assertEqual(payload["detail"], "The last background collection failed.")

    def test_a_failed_manual_refresh_leaves_healthz_ok(self) -> None:
        payload = self._healthz(
            {"collector_running": True, "last_error": "History full refresh failed; see service logs."},
            None,
        )

        self.assertEqual(payload["status"], "ok")


class StartingStateTests(unittest.TestCase):
    def test_collector_reports_starting_during_the_grace_period_only(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            collector = _collector(startup_grace_seconds=30)
            await collector.start()
            await asyncio.sleep(0)
            during = collector.status()["collector_starting"]
            await collector.stop()
            after = collector.status()["collector_starting"]
            return during, after

        with patch.object(HistoryCollector, "run_once"):
            during, after = asyncio.run(scenario())

        self.assertTrue(during)
        self.assertFalse(after)

    def test_state_label_says_starting_running_or_stopped(self) -> None:
        label = history_main.collector_state_label
        self.assertEqual(label({"collector_running": True, "collector_starting": True}), "Starting")
        self.assertEqual(label({"collector_running": True, "collector_starting": False}), "Running")
        self.assertEqual(label({"collector_running": False, "collector_starting": True}), "Stopped")

    def test_starting_rides_the_service_status_projection(self) -> None:
        projected = history_main.public_collector_status(
            {"collector_running": True, "collector_starting": True, "retention_consecutive_failures": 1}
        )

        self.assertIs(projected["collector_starting"], True)
        self.assertEqual(projected["retention_consecutive_failures"], 1)


class CountLabelTests(unittest.TestCase):
    def test_an_uncounted_value_is_a_dash_not_deferred(self) -> None:
        self.assertEqual(history_main.format_count(None), "-")
        self.assertEqual(history_main.format_count(12), "12")


class UnsafeStatusFileTests(unittest.TestCase):
    def test_a_writable_status_file_is_named_in_plain_words_and_clears_when_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "status.json"
            status_path.write_text("{}", encoding="utf-8")
            os.chmod(status_path, 0o666)
            collector = _collector(scheduled_backup_status_file=str(status_path))

            with self.assertLogs("history_service.collector", level="WARNING") as logs:
                self.assertIsNone(
                    collector._segmented_backup_at_for_retention(datetime(2026, 7, 1, tzinfo=timezone.utc))
                )

            status = collector.status()
            self.assertEqual(status["last_retention_error_kind"], "backup_status_mode")
            self.assertEqual(
                status["last_retention_error"],
                RETENTION_FAILURE_SENTENCES["backup_status_mode"],
            )
            self.assertIn("0640", status["last_retention_error"])
            self.assertNotIn(temp_dir, status["last_retention_error"])
            self.assertTrue(any("0666" in line for line in logs.output), logs.output)

            os.chmod(status_path, 0o640)
            collector._segmented_backup_at_for_retention(datetime(2026, 7, 1, tzinfo=timezone.utc))
            self.assertIsNone(collector.status()["last_retention_error_kind"])


if __name__ == "__main__":
    unittest.main()
