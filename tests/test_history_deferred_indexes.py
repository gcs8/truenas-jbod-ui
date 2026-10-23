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

            def build(name: str, **kwargs) -> None:
                status = collector.status()
                self.assertTrue(status["collection_running"])
                self.assertEqual(status["collection_kind"], "maintenance")
                activities.append(status["collection_activity"])
                original(name, **kwargs)

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

            def build(name: str, **kwargs) -> None:
                if "failed" not in events:
                    events.append("failed")
                    raise sqlite3.OperationalError("database is locked")
                events.append("built")
                original(name, **kwargs)

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

            def build(name: str, **kwargs) -> None:
                if events.count("failed") < failures_before_success:
                    events.append("failed")
                    reasons.append(None)
                    raise sqlite3.OperationalError("disk I/O")
                events.append("built")
                original(name, **kwargs)

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

    def test_stop_interrupts_native_index_and_restart_keeps_completed_indexes(self) -> None:
        self._exercise_index_shutdown(cancel_task=False)

    def test_stop_drains_index_worker_after_outer_task_cancellation(self) -> None:
        self._exercise_index_shutdown(cancel_task=True)

    def _exercise_index_shutdown(self, *, cancel_task: bool) -> None:
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(
                HistorySettings(sqlite_path=str(path), startup_grace_seconds=0), store
            )
            entered = threading.Event()
            release = threading.Event()
            closed = threading.Event()
            target = CHRONOLOGICAL_INDEXES[1][0]
            native_connect = sqlite3.connect

            class GatedConnection(sqlite3.Connection):
                cancellation_check = None

                def set_progress_handler(self, progress_handler, n):
                    self.cancellation_check = progress_handler
                    return super().set_progress_handler(progress_handler, n)

                def execute(self, sql, *args, **kwargs):
                    if sql == dict(CHRONOLOGICAL_INDEXES)[target]:
                        self.target_build = True
                        def progress():
                            if not entered.is_set():
                                entered.set()
                                if not release.wait(5):
                                    raise AssertionError("test did not release native progress callback")
                            return self.cancellation_check() if self.cancellation_check else 0

                        super().set_progress_handler(progress, 1)
                    return super().execute(sql, *args, **kwargs)

                def close(self):
                    super().close()
                    if getattr(self, "target_build", False):
                        closed.set()

            def connect(*args, **kwargs):
                return native_connect(*args, **kwargs, factory=GatedConnection)

            async def exercise():
                await collector.start()
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    self.assertTrue(collector.collection_running)
                    if cancel_task:
                        assert collector._task is not None
                        collector._task.cancel()
                        await asyncio.sleep(0)
                    stopping = asyncio.create_task(collector.stop())
                    await asyncio.sleep(0)
                    self.assertFalse(stopping.done())
                    release.set()
                    await asyncio.wait_for(stopping, 2)
                    # Assert owned-resource settlement before fixture cleanup.
                    self.assertTrue(closed.is_set())
                    self.assertFalse(collector.collection_running)
                    self.assertIsNone(collector._task)
                    self.assertIsNone(collector.current_collection_activity)
                finally:
                    release.set()
                    await collector.stop()

            with (
                patch.object(store_module.sqlite3, "connect", side_effect=connect),
                patch.object(collector, "run_once") as collect,
            ):
                asyncio.run(exercise())
                collect.assert_not_called()

            self.assertEqual(_indexes(path), {CHRONOLOGICAL_INDEXES[0][0]})
            self.assertEqual(collector._index_build_failures, 0)
            self.assertIsNone(collector._chronological_indexes_identity)
            self.assertIsNone(collector.degraded_reason())
            with closing(native_connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT count(*) FROM metric_samples").fetchone()[0], 4_000)
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()

            # A fresh store/collector sees the completed first index, then builds
            # only the missing ones before admitting its first collection.
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                reopened = HistoryStore(str(path), recover_unreadable_database=False)
            restarted = HistoryCollector(collector.settings, reopened)
            built = []
            original_build = reopened.build_chronological_index

            def build(name, **kwargs):
                built.append(name)
                original_build(name, **kwargs)

            async def collect_after_restart(**_kwargs):
                self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
                restarted._stopping.set()

            async def restart():
                await restarted.start()
                assert restarted._task is not None
                await asyncio.wait_for(restarted._task, 5)
                await restarted.stop()

            with (
                patch.object(reopened, "build_chronological_index", side_effect=build),
                patch.object(restarted, "run_once", side_effect=collect_after_restart) as collected,
            ):
                asyncio.run(restart())
                collected.assert_awaited_once()
            self.assertEqual(built, [name for name, _ in CHRONOLOGICAL_INDEXES[1:]])

    def test_store_cancellation_uses_native_progress_and_rolls_back_only_current_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            first, target = [name for name, _ in CHRONOLOGICAL_INDEXES[:2]]
            store.build_chronological_index(first)
            calls = 0

            def should_continue():
                nonlocal calls
                calls += 1
                return calls < 4

            with self.assertRaises(store_module.HistoryIndexBuildCancelled):
                store.build_chronological_index(target, should_continue=should_continue)
            self.assertGreaterEqual(calls, 4)
            self.assertEqual(_indexes(path), {first})
            store.build_chronological_index(target)
            self.assertEqual(_indexes(path), {first, target})

    def test_stop_before_connection_or_execution_admits_no_index(self) -> None:
        for cutoff in (1, 2):
            with self.subTest(cutoff=cutoff), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "history.sqlite3"
                _legacy_database(path, samples=40)
                with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                    store = HistoryStore(str(path), recover_unreadable_database=False)
                calls = 0

                def should_continue():
                    nonlocal calls
                    calls += 1
                    return calls < cutoff

                with (
                    patch.object(store, "_connect", wraps=store._connect) as connect,
                    self.assertRaises(store_module.HistoryIndexBuildCancelled),
                ):
                    store.build_chronological_index(
                        CHRONOLOGICAL_INDEXES[0][0], should_continue=should_continue
                    )
                self.assertEqual(connect.call_count, cutoff - 1)
                self.assertEqual(_indexes(path), set())

    def test_stop_after_final_index_does_not_collect_or_cache_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(
                HistorySettings(sqlite_path=str(path), startup_grace_seconds=0), store
            )
            original = collector._build_index_blocking

            async def exercise():
                # Invoke stop at the return boundary of the final actual build,
                # before _build_pending_indexes publishes completion.
                def build(name, position, total):
                    original(name, position, total)
                    if position == total:
                        collector._stopping.set()

                with (
                    patch.object(collector, "_build_index_blocking", side_effect=build),
                    patch.object(collector, "run_once") as collect,
                ):
                    await collector.start()
                    assert collector._task is not None
                    await asyncio.wait_for(collector._task, 5)
                    await collector.stop()
                    collect.assert_not_called()

            asyncio.run(exercise())
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertIsNone(collector._chronological_indexes_identity)

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
