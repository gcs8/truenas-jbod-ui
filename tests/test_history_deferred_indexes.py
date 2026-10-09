"""#910: a large upgraded history database answers before its new indexes exist.

The first v0.24.0-beta.1 start built five chronological indexes before the
service listened. On a 4 GB production database that took 58.7 seconds, so the
healthcheck failed and the image-only upgrade rolled back. Small databases
still build them inline; large ones defer them to the collector.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from history_service import store as store_module
from history_service import collector as collector_module
from history_service.collector import HistoryCollector
from history_service.config import HistorySettings
from history_service.store import CHRONOLOGICAL_INDEXES, HistoryStore

CHRONOLOGICAL_NAMES = {name for name, _ in CHRONOLOGICAL_INDEXES}


def _indexes(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE '%chronological'"
            )
        }


def _legacy_database(path: Path, samples: int) -> None:
    """A started database whose chronological indexes predate this release."""

    HistoryStore(str(path), recover_unreadable_database=False)
    with closing(sqlite3.connect(path)) as connection, connection:
        for name in CHRONOLOGICAL_NAMES:
            connection.execute(f"DROP INDEX IF EXISTS {name}")
        connection.executemany(
            """INSERT INTO metric_samples (
                observed_at, system_id, enclosure_key, slot, slot_label,
                metric_name, value_integer, disk_identity_key
            ) VALUES (?, 'system-1', 'enclosure-1', ?, 'slot', 'temperature_c', 30, 'disk')""",
            [("2026-01-01T00:00:00+00:00", index % 60) for index in range(samples)],
        )


class DeferredChronologicalIndexTests(unittest.TestCase):
    def test_large_upgraded_database_defers_its_index_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)

            self.assertEqual(_indexes(path), set())
            self.assertEqual(
                store.pending_chronological_indexes(),
                tuple(name for name, _ in CHRONOLOGICAL_INDEXES),
            )
            for name in store.pending_chronological_indexes():
                store.build_chronological_index(name)
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertEqual(store.pending_chronological_indexes(), ())

    def test_small_database_still_builds_its_indexes_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            store = HistoryStore(str(path), recover_unreadable_database=False)

            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertEqual(store.pending_chronological_indexes(), ())

    def test_collector_builds_deferred_indexes_before_collecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
            activities: list[str | None] = []
            original = store.build_chronological_index

            def build(name: str) -> None:
                status = collector.status()
                self.assertTrue(status["collection_running"])
                self.assertEqual(status["collection_kind"], "maintenance")
                activities.append(status["collection_activity"])
                original(name)

            with patch.object(store, "build_chronological_index", side_effect=build):
                asyncio.run(collector._build_pending_indexes())

            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertEqual(
                activities,
                [f"building history index {position} of 5" for position in range(1, 6)],
            )
            self.assertFalse(collector.collection_running)

    def test_a_failed_build_leaves_the_rest_for_the_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)

            with (
                patch.object(store, "build_chronological_index", side_effect=sqlite3.OperationalError("disk I/O")),
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                asyncio.run(collector._build_pending_indexes())

            self.assertFalse(collector.collection_running)
            self.assertEqual(len(store.pending_chronological_indexes()), 5)
            # The collector retries before its next pass.
            self.assertTrue(asyncio.run(collector._build_pending_indexes()))
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertIsNone(collector.degraded_reason())

    def test_collection_waits_until_a_failed_index_build_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(
                HistorySettings(sqlite_path=str(path), startup_grace_seconds=0), store
            )
            events: list[str] = []
            degraded: list[str | None] = []
            original = store.build_chronological_index

            def build(name: str) -> None:
                if "failed" not in events:
                    events.append("failed")
                    raise sqlite3.OperationalError("database is locked")
                events.append("built")
                original(name)

            def collect(**_kwargs) -> None:
                events.append("collect")
                degraded.append(collector.degraded_reason())
                collector._stopping.set()

            with (
                patch.object(collector_module, "INDEX_BUILD_RETRY_SECONDS", 0.01),
                patch.object(store, "build_chronological_index", side_effect=build),
                patch.object(collector, "run_once", side_effect=collect),
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                asyncio.run(asyncio.wait_for(collector._run_loop(), timeout=30))

            # No collection pass ran between the failure and the retry.
            self.assertEqual(events, ["failed", *["built"] * 5, "collect"])
            self.assertEqual(degraded, [None])
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)

    def test_collection_stays_paused_until_a_persistently_failing_build_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(
                HistorySettings(sqlite_path=str(path), startup_grace_seconds=0), store
            )
            quick = collector_module.INDEX_BUILD_QUICK_RETRIES
            failures_before_success = quick + 3
            events: list[str] = []
            reasons: list[str | None] = []
            waits: list[float] = []
            original = store.build_chronological_index
            original_wait_for = asyncio.wait_for

            def build(name: str) -> None:
                if events.count("failed") < failures_before_success:
                    events.append("failed")
                    reasons.append(None)
                    raise sqlite3.OperationalError("disk I/O")
                events.append("built")
                original(name)

            def collect(**_kwargs) -> None:
                events.append("collect")
                collector._stopping.set()

            async def short_wait(awaitable, timeout):
                if timeout in (
                    collector_module.INDEX_BUILD_RETRY_SECONDS,
                    collector_module.INDEX_BUILD_SLOW_RETRY_SECONDS,
                ):
                    waits.append(timeout)
                    reasons[-1] = collector.degraded_reason()
                    timeout = 0.001
                return await original_wait_for(awaitable, timeout)

            with (
                patch.object(collector_module.asyncio, "wait_for", side_effect=short_wait),
                patch.object(store, "build_chronological_index", side_effect=build),
                patch.object(collector, "run_once", side_effect=collect),
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                asyncio.run(original_wait_for(collector._run_loop(), timeout=30))

            # No scheduled pass ran while the build kept failing.
            self.assertEqual(events, ["failed"] * failures_before_success + ["built"] * 5 + ["collect"])
            # Quick retries first, then slower ones while collection stays paused.
            self.assertEqual(
                waits,
                [collector_module.INDEX_BUILD_RETRY_SECONDS] * quick
                + [collector_module.INDEX_BUILD_SLOW_RETRY_SECONDS] * (failures_before_success - quick),
            )
            self.assertEqual(
                reasons,
                [collector_module.INDEX_BUILD_DEGRADED_REASON] * quick
                + [collector_module.INDEX_BUILD_PAUSED_REASON] * (failures_before_success - quick),
            )
            self.assertIsNone(collector.degraded_reason())
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)

    def test_damage_found_while_listing_pending_indexes_pauses_collection(self) -> None:
        # The same damage handling as a failed build: the durable pause marker
        # and its recovery guidance, not a generic retry loop.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
            with (
                patch.object(
                    store,
                    "pending_chronological_indexes",
                    side_effect=sqlite3.DatabaseError("database disk image is malformed"),
                ),
                patch.object(store, "record_collection_pause", wraps=store.record_collection_pause) as recorded,
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                self.assertFalse(asyncio.run(collector._build_pending_indexes()))

            recorded.assert_called_once()
            self.assertTrue(collector.collection_pause()[0])
            self.assertEqual(collector.degraded_reason(), collector_module.COLLECTION_PAUSED_REASON)

    def test_a_failed_index_build_is_reported_as_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
            with (
                patch.object(store, "build_chronological_index", side_effect=sqlite3.OperationalError("disk I/O")),
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                self.assertFalse(asyncio.run(collector._build_pending_indexes()))
            self.assertEqual(collector.degraded_reason(), collector_module.INDEX_BUILD_DEGRADED_REASON)

    def test_a_restored_database_is_checked_again(self) -> None:
        # An online restore replaces the hot file under a running collector.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            backup = Path(directory) / "backup.sqlite3"
            _legacy_database(path, samples=40)
            _legacy_database(backup, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
                collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
                self.assertTrue(asyncio.run(collector._build_pending_indexes()))
                self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
                store.restore_backup(backup)
                self.assertEqual(_indexes(path), set())

                self.assertTrue(asyncio.run(collector._build_pending_indexes()))

            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)

    def test_reads_answer_while_an_index_builds(self) -> None:
        import threading
        import time

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=400_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            name = store.pending_chronological_indexes()[2]
            build = threading.Thread(target=store.build_chronological_index, args=(name,))
            build.start()
            slowest = 0.0
            reads = 0
            while build.is_alive():
                started = time.monotonic()
                # What /healthz and the dashboard read.
                store.quarantine_recovery_status()
                store.database_size_bytes()
                slowest = max(slowest, time.monotonic() - started)
                reads += 1
            build.join()

            self.assertIn(name, _indexes(path))
            self.assertGreater(reads, 1)
            self.assertLess(slowest, 1.0)


if __name__ == "__main__":
    unittest.main()
