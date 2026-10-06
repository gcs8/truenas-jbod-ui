"""Health grading, startup state, and plain status-file diagnostics (#456, #441)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from history_service import main as history_main
from history_service.collector import HistoryCollector
from history_service.config import HistorySettings
from history_service.diagnostics import RETENTION_FAILURE_SENTENCES
from history_service.segment_catalog import MIGRATION_PENDING_MARKER
from history_service.store import HistoryStore


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


def _asgi_get(path: str, query: bytes = b"", *, method: str = "GET", body: bytes = b"") -> tuple[int, bytes]:
    """Drive the real app, its exception handlers and middleware, without lifespan."""

    async def invoke() -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        async def receive() -> Any:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Any) -> None:
            messages.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": query,
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }
        try:
            await history_main.app(scope, receive, send)
        except Exception:
            # An unhandled error still sends its 500 first; report that status.
            if not any(message["type"] == "http.response.start" for message in messages):
                raise
        return messages

    messages = asyncio.run(invoke())
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body


class MissingSegmentCatalogDashboardTests(unittest.TestCase):
    """#833: the page and its overview poll degrade like /healthz (#663) does.

    A real store is configured for segmented history with a catalog path that
    does not exist, the way a fresh segmented deployment starts.
    """

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.catalog = self.root / "segments" / "catalog.json"
        self.store = HistoryStore(str(self.root / "history.db"), segment_catalog_path=str(self.catalog))
        for patcher in (
            patch.object(history_main, "store", self.store),
            patch.object(history_main, "startup_failure_reason", None),
            patch.object(history_main.collector, "status", return_value={"collector_running": True}),
            patch.object(history_main.collector, "degraded_reason", return_value=None),
            patch.object(history_main.get_release_status_service(), "snapshot", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _storage_banner(markup: str) -> re.Match[str]:
        banner = re.search(r'<div id="history-storage-degraded"([^>]*)>(.*?)</div>', markup, flags=re.DOTALL)
        assert banner is not None, "the dashboard must carry the storage notice hook"
        return banner

    def test_overview_answers_degraded_content_with_the_missing_catalog_reason(self) -> None:
        for query in (b"", b"exact_counts=true"):
            with self.subTest(query=query):
                status, body = _asgi_get("/api/history/overview", query)
                self.assertEqual(status, 200, body)
                payload = json.loads(body)
                self.assertEqual(payload["degraded_reason"], history_main.SEGMENT_CATALOG_MISSING_REASON)
                self.assertEqual(payload["counts"], {})
                self.assertIs(payload["counts_exact"], False)
                self.assertEqual(payload["scopes"], [])
                self.assertIsNone(payload["database"]["size_bytes"])
                self.assertIsNone(payload["database"]["reclaimable_bytes"])
                self.assertEqual(payload["database"]["reclaimable_label"], "unknown")
                self.assertIs(payload["collector"]["collector_running"], True)
        self.assertFalse(os.path.lexists(self.catalog), "a read must not create the catalog")

    def test_dashboard_page_shows_the_missing_catalog_reason_and_an_unknown_size(self) -> None:
        status, body = _asgi_get("/")

        self.assertEqual(status, 200, body)
        markup = body.decode("utf-8")
        banner = self._storage_banner(markup)
        self.assertNotIn("hidden", banner.group(1))
        self.assertEqual(banner.group(2).strip(), history_main.SEGMENT_CATALOG_MISSING_REASON)
        self.assertRegex(markup, r'id="db-size-value" class="value">\s*unknown\s*<')
        self.assertIn("Free inside the file: unknown", markup)
        # Nothing was read, so the page must not claim nothing was collected.
        self.assertNotIn("No slot history has been collected yet.", markup)
        self.assertFalse(os.path.lexists(self.catalog), "a read must not create the catalog")

    def test_healthz_names_the_same_missing_catalog_reason_as_the_dashboard(self) -> None:
        status, body = _asgi_get("/healthz")

        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["detail"], history_main.SEGMENT_CATALOG_MISSING_REASON)
        self.assertIsNone(payload["database_size_bytes"])

    def test_a_catalog_naming_a_missing_segment_still_fails_closed(self) -> None:
        self.catalog.parent.mkdir(parents=True)
        self.catalog.write_text(
            json.dumps({
                "catalog_version": 1, "generation_id": "generation-0001", "complete": True,
                "segments": [{
                    "segment_id": "segment-0001", "file_name": "segment-0001.sqlite3",
                    "size_bytes": 4096, "sha256": "0" * 64,
                    "coverage_start": "2026-09-01T00:00:00+00:00",
                    "coverage_end": "2026-09-02T00:00:00+00:00",
                }],
            }),
            encoding="utf-8",
        )
        self._assert_storage_unavailable()

    def test_a_pending_migration_marker_still_fails_closed(self) -> None:
        self.catalog.parent.mkdir(parents=True)
        (self.catalog.parent / MIGRATION_PENDING_MARKER).write_text("synthetic", encoding="utf-8")
        self._assert_storage_unavailable()

    def _assert_storage_unavailable(self) -> None:
        for path in ("/", "/api/history/overview"):
            with self.subTest(path=path):
                status, body = _asgi_get(path)
                self.assertEqual(status, 503, body)
                self.assertEqual(json.loads(body), {"detail": history_main.HISTORY_UNAVAILABLE_DETAIL})

    def test_a_refresh_reply_keeps_its_storage_unavailable_answer(self) -> None:
        # Only the page and GET /api/history/overview degrade (#833); the
        # refresh reply reuses the overview payload but keeps its own contract.
        admission = history_main.ManualRefreshAdmission(cooldown_seconds=900)
        with (
            patch.object(history_main, "refresh_admission", admission),
            patch.object(history_main.settings, "refresh_auth_mode", "network"),
            patch.object(history_main.collector, "collection_pause", return_value=(False, None)),
            patch.object(type(history_main.collector), "collection_running", new_callable=PropertyMock, return_value=False),
            patch.object(history_main.collector, "run_once", new_callable=AsyncMock) as run_once,
        ):
            status, body = _asgi_get("/api/history/refresh", method="POST", body=b'{"mode":"fast"}')

        run_once.assert_awaited_once()
        self.assertEqual(status, 503, body)
        self.assertEqual(json.loads(body), {"detail": history_main.HISTORY_UNAVAILABLE_DETAIL})

    def test_a_readable_store_reports_no_degraded_reason(self) -> None:
        with patch.object(history_main, "store", HistoryStore(str(self.root / "plain.db"))):
            overview_status, overview_body = _asgi_get("/api/history/overview")
            page_status, page_body = _asgi_get("/")

        self.assertEqual((overview_status, page_status), (200, 200))
        payload = json.loads(overview_body)
        self.assertIsNone(payload["degraded_reason"])
        self.assertIsInstance(payload["database"]["size_bytes"], int)
        banner = self._storage_banner(page_body.decode("utf-8"))
        self.assertIn("hidden", banner.group(1))
        self.assertEqual(banner.group(2).strip(), "")
        self.assertIn("No slot history has been collected yet.", page_body.decode("utf-8"))


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
