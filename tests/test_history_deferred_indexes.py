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
from collections.abc import Iterator
from contextlib import closing, contextmanager
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


# Builds the deferred indexes of an upgraded database in a fresh interpreter and
# reports how far the build raised the process's resident-memory high-water
# mark. SQLite allocates outside Python, so tracemalloc cannot see this.
# Linux's VmHWM is this process's own peak. ru_maxrss is not usable here: it
# carries the parent's peak across fork and exec, which hides the build's growth.
_PEAK_CHILD = r"""
import json, sys
from history_service import store as store_module

def peak_kib():
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    raise SystemExit("VmHWM missing")

path, cache_kib = sys.argv[1], int(sys.argv[2])
store_module.SQLITE_CACHE_SIZE_KIB = cache_kib
if hasattr(store_module, "BULK_MAINTENANCE_CACHE_SIZE_KIB"):
    store_module.BULK_MAINTENANCE_CACHE_SIZE_KIB = cache_kib
store_module.CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS = 0
store = store_module.HistoryStore(path, recover_unreadable_database=False)
pending = store.pending_chronological_indexes()
before = peak_kib()
for name in pending:
    store.build_chronological_index(name)
after = peak_kib()
print(json.dumps({
    "pending": list(pending), "before_kib": before, "after_kib": after,
    "growth_kib": after - before,
}))
"""


def _large_legacy_database(path: Path, samples: int) -> None:
    """A 0.22.2-shaped database: history rows, no chronological indexes.

    Rows are generated inside SQLite and the existing indexes rebuilt after
    the insert, so a few hundred thousand rows take seconds. Identifier
    widths are realistic so the index keys, not the row count alone, carry
    the size.
    """

    HistoryStore(str(path), recover_unreadable_database=False)
    with closing(sqlite3.connect(path)) as connection:
        indexes = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
            "AND tbl_name IN ('metric_samples', 'slot_events')"
        ).fetchall()
        for name, _ in indexes:
            connection.execute(f"DROP INDEX {name}")
        connection.execute(
            """
            WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i + 1 < ?)
            INSERT INTO metric_samples (
                observed_at, system_id, enclosure_key, slot, slot_label,
                metric_name, value_integer, disk_identity_key
            )
            SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 1700000000 + i, 'unixepoch'),
                   printf('system-%032d', i % 3), printf('enclosure-%040d', i % 5),
                   i % 90, 'slot', printf('metric-%020d', i % 11), i % 70,
                   printf('disk-%060d', i % 450)
            FROM n
            """,
            (samples,),
        )
        connection.execute(
            """
            WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i + 1 < ?)
            INSERT INTO slot_events (
                observed_at, system_id, enclosure_key, slot, slot_label, event_type, details_json
            )
            SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', 1700000000 + i, 'unixepoch'),
                   printf('system-%032d', i % 3), printf('enclosure-%040d', i % 5),
                   i % 90, 'slot', 'seen', '{}'
            FROM n
            """,
            (samples // 4,),
        )
        connection.commit()
        for name, statement in indexes:
            if name not in CHRONOLOGICAL_NAMES:
                connection.execute(statement)
        connection.commit()


class _PragmaRecordingConnection(sqlite3.Connection):
    """Records temp_store and cache_size at the moment each CREATE INDEX runs."""

    seen: list[tuple[str, int, int]] = []

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE INDEX"):
            temp_store = int(super().execute("PRAGMA temp_store").fetchone()[0])
            cache_size = int(super().execute("PRAGMA cache_size").fetchone()[0])
            _PragmaRecordingConnection.seen.append((sql, temp_store, cache_size))
        return super().execute(sql, *args, **kwargs)


class IndexBuildMemoryTests(unittest.TestCase):
    """#1092: restoring a 0.22.2 history OOM-killed the history container.

    The deferred index builds ran with temp_store=MEMORY, so SQLite's sorter
    spilled every index key into heap: the build's memory grew with the table
    (about 1 GiB on a real restore) instead of staying near cache_size.
    """

    # SQLite temp_store values: 1 = FILE, 2 = MEMORY.
    TEMP_STORE_FILE = 1

    def _recording_connect(self):
        native_connect = sqlite3.connect
        _PragmaRecordingConnection.seen = []

        def connect(*args, **kwargs):
            kwargs.setdefault("factory", _PragmaRecordingConnection)
            return native_connect(*args, **kwargs)

        return patch.object(store_module.sqlite3, "connect", side_effect=connect)

    def test_index_build_peak_memory_is_bounded_by_cache_not_table_size(self) -> None:
        if not Path("/proc/self/status").is_file():
            self.skipTest("needs Linux /proc/self/status VmHWM")
        import json
        import os
        import subprocess
        import sys

        cache_kib = 2048
        # Before the fix this build grew the process by about 48 MiB at this
        # size, and the growth doubles with the rows. Bounded, it stays near the
        # page cache plus the sorter's one-cache in-memory run.
        budget_kib = 4 * cache_kib + 16 * 1024
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _large_legacy_database(path, samples=300_000)
            environment = {
                **os.environ,
                "PYTHONPATH": str(repository),
                # The shipped Compose file points TMPDIR at the history volume.
                "TMPDIR": directory,
                "SQLITE_TMPDIR": directory,
            }
            completed = subprocess.run(
                [sys.executable, "-c", _PEAK_CHILD, str(path), str(cache_kib)],
                capture_output=True,
                text=True,
                timeout=240,
                env=environment,
                cwd=directory,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            report = json.loads(completed.stdout.strip().splitlines()[-1])

            self.assertEqual(report["pending"], [name for name, _ in CHRONOLOGICAL_INDEXES])
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            self.assertLess(
                report["growth_kib"],
                budget_kib,
                f"index build grew RSS by {report['growth_kib']} KiB; budget {budget_kib} KiB",
            )
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM metric_samples").fetchone()[0], 300_000
                )

    def test_deferred_build_connection_spills_to_disk_with_a_bounded_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            with self._recording_connect():
                for name in store.pending_chronological_indexes():
                    store.build_chronological_index(name)

            statements = dict(CHRONOLOGICAL_INDEXES)
            seen = {sql: (temp_store, cache) for sql, temp_store, cache in _PragmaRecordingConnection.seen}
            self.assertEqual(set(seen), set(statements.values()))
            for temp_store, cache_size in seen.values():
                self.assertEqual(temp_store, self.TEMP_STORE_FILE)
                self.assertEqual(cache_size, -store_module.BULK_MAINTENANCE_CACHE_SIZE_KIB)

    def test_startup_upgrade_builds_indexes_with_disk_temporary_storage(self) -> None:
        # Small databases still build the chronological indexes inline at
        # startup; that path has the same sorter, so the same profile.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with self._recording_connect():
                HistoryStore(str(path), recover_unreadable_database=False)

            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)
            built = [entry for entry in _PragmaRecordingConnection.seen if "chronological" in entry[0]]
            self.assertTrue(built)
            for _sql, temp_store, cache_size in built:
                self.assertEqual(temp_store, self.TEMP_STORE_FILE)
                self.assertEqual(cache_size, -store_module.BULK_MAINTENANCE_CACHE_SIZE_KIB)

    def test_ordinary_connections_keep_their_existing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = HistoryStore(str(path), recover_unreadable_database=False)
            with closing(store._connect()) as connection:
                self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 2)
                self.assertEqual(
                    connection.execute("PRAGMA cache_size").fetchone()[0],
                    -store_module.SQLITE_CACHE_SIZE_KIB,
                )

    def test_index_build_refuses_to_start_without_free_disk_space(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(path)), store)
            full = shutil._ntuple_diskusage(total=1 << 30, used=1 << 30, free=0)

            with (
                patch.object(store_module.shutil, "disk_usage", return_value=full),
                self.assertLogs("history_service.store", "WARNING") as store_logs,
                self.assertLogs("history_service.collector", "WARNING"),
            ):
                self.assertFalse(asyncio.run(collector._build_pending_indexes()))

            self.assertIn("free", "\n".join(store_logs.output))
            # The empty slot_events index needs no space; the first sample index is refused.
            self.assertEqual(_indexes(path), {CHRONOLOGICAL_INDEXES[0][0]})
            self.assertEqual(collector.degraded_reason(), collector_module.INDEX_BUILD_SPACE_DEGRADED_REASON)
            # Space freed: the retry builds everything.
            self.assertTrue(asyncio.run(collector._build_pending_indexes()))
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)

    def test_index_size_estimate_tracks_the_key_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with (
                patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10),
                closing(sqlite3.connect(path)) as connection,
            ):
                HistoryStore(str(path), recover_unreadable_database=False)
                statements = dict(CHRONOLOGICAL_INDEXES)
                slot = HistoryStore._estimate_index_bytes(
                    connection, statements["idx_metric_samples_slot_chronological"]
                )
                scope = HistoryStore._estimate_index_bytes(
                    connection, statements["idx_metric_samples_scope_chronological"]
                )
                events = HistoryStore._estimate_index_bytes(
                    connection, statements["idx_slot_events_scope_chronological"]
                )
            # 4,000 rows of short keys: tens of bytes each, not kilobytes.
            self.assertGreater(slot, 4_000 * 20)
            self.assertLess(slot, 4_000 * 200)
            self.assertGreater(scope, slot)  # adds metric_name
            self.assertEqual(events, 0)  # no slot events in this fixture

    def test_index_size_estimate_counts_retained_rows_not_lifetime_ids(self) -> None:
        # Retention deletes old rows without renumbering AUTOINCREMENT ids, so
        # max(rowid) can be far above what the index will hold.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE sqlite_sequence SET seq = 100000000 WHERE name = 'metric_samples'")
                connection.execute(
                    """INSERT INTO metric_samples (
                        observed_at, system_id, enclosure_key, slot, slot_label,
                        metric_name, value_integer, disk_identity_key
                    ) VALUES ('2026-01-02T00:00:00+00:00', 'system-1', 'enclosure-1', 1, 'slot',
                              'temperature_c', 30, 'disk')"""
                )
                connection.execute("DELETE FROM metric_samples WHERE id < 100000000")
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 0):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            with closing(sqlite3.connect(path)) as connection:
                self.assertGreater(
                    connection.execute("SELECT max(id) FROM metric_samples").fetchone()[0], 100_000_000
                )
                estimate = HistoryStore._estimate_index_bytes(
                    connection, dict(CHRONOLOGICAL_INDEXES)["idx_metric_samples_scope_chronological"]
                )
            self.assertLess(estimate, 1024)
            # A volume with only the margin free still admits the build.
            import shutil

            roomy = shutil._ntuple_diskusage(
                total=1 << 30, used=0, free=store_module.INDEX_BUILD_FREE_SPACE_MARGIN_BYTES + (1 << 20)
            )
            with patch.object(store_module.shutil, "disk_usage", return_value=roomy):
                for name in store.pending_chronological_indexes():
                    store.build_chronological_index(name)
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)


class ExactCountsDuringIndexBuildTests(unittest.TestCase):
    """#1092: the UI's exact_counts poll scanned the tables the build was reading."""

    def _store(self, directory: str) -> HistoryStore:
        path = Path(directory) / "history.sqlite3"
        _legacy_database(path, samples=40)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                """INSERT INTO slot_state_current (
                    system_id, enclosure_key, slot, slot_label, present, identify_active, last_seen_at
                ) VALUES ('system-1', 'enclosure-1', 0, 'slot', 1, 0, '2026-01-01T00:00:00+00:00')"""
            )
        with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
            return HistoryStore(str(path), recover_unreadable_database=False)

    def _overview(self, store: HistoryStore) -> dict:
        from history_service import main as history_main

        with (
            patch.object(history_main, "store", store),
            patch.object(history_main.collector, "status", return_value={}),
        ):
            return asyncio.run(history_main.overview(exact_counts=True, degrade_missing_catalog=True))

    def test_exact_counts_are_deferred_while_an_index_build_is_active(self) -> None:
        from history_service import main as history_main

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            with (
                store.index_maintenance(),
                patch.object(store, "counts", side_effect=AssertionError("exact scan ran")),
            ):
                payload = self._overview(store)

            self.assertEqual(payload["counts_deferred_reason"], history_main.EXACT_COUNTS_DEFERRED_REASON)
            # Totals still come from the trigger-maintained counters.
            self.assertEqual(payload["counts"]["metric_sample_count"], 40)
            self.assertEqual(payload["counts"]["count_mode"], "tracked")
            self.assertTrue(payload["scopes"])
            for scope in payload["scopes"]:
                self.assertIsNone(scope["metric_sample_count"])
                self.assertEqual(scope["activity_counts_deferred"], 1)

            after = self._overview(store)
            self.assertIsNone(after["counts_deferred_reason"])
            self.assertTrue(after["counts_exact"])
            self.assertEqual(after["scopes"][0]["metric_sample_count"], 40)

    def test_collector_defers_exact_counts_for_its_whole_build_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            collector = HistoryCollector(HistorySettings(sqlite_path=str(store.file_path)), store)
            admitted: list[bool] = []
            original = collector._build_index_blocking

            def build(name, position, total):
                with store.exact_count_scan() as allowed:
                    admitted.append(allowed)
                original(name, position, total)

            with patch.object(collector, "_build_index_blocking", side_effect=build):
                self.assertTrue(asyncio.run(collector._build_pending_indexes()))

            self.assertEqual(admitted, [False] * 5)
            self.assertFalse(store.index_maintenance_active())
            with store.exact_count_scan() as allowed:
                self.assertTrue(allowed)

    def test_index_build_waits_for_a_running_exact_count_scan(self) -> None:
        import threading

        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            name = store.pending_chronological_indexes()[0]
            scanning = threading.Event()
            release = threading.Event()

            def scan() -> None:
                with store.exact_count_scan() as allowed:
                    self.assertTrue(allowed)
                    scanning.set()
                    release.wait(5)

            scanner = threading.Thread(target=scan)
            scanner.start()
            self.assertTrue(scanning.wait(5))
            builder = threading.Thread(target=store.build_chronological_index, args=(name,))
            builder.start()
            try:
                for _ in range(500):
                    if store.index_maintenance_active():
                        break
                    threading.Event().wait(0.01)
                self.assertTrue(store.index_maintenance_active())
                # A new exact scan is refused while the build waits.
                with store.exact_count_scan() as allowed:
                    self.assertFalse(allowed)
                builder.join(0.5)
                self.assertTrue(builder.is_alive())
                self.assertNotIn(name, _indexes(store.file_path))
            finally:
                release.set()
                scanner.join(5)
                builder.join(10)
            self.assertIn(name, _indexes(store.file_path))

    def test_a_stop_while_waiting_for_an_exact_scan_cancels_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            name = store.pending_chronological_indexes()[0]
            calls = 0

            def should_continue() -> bool:
                nonlocal calls
                calls += 1
                return calls < 2

            with store.exact_count_scan() as allowed:
                self.assertTrue(allowed)
                with self.assertRaises(store_module.HistoryIndexBuildCancelled):
                    store.build_chronological_index(name, should_continue=should_continue)
            self.assertFalse(store.index_maintenance_active())
            self.assertEqual(_indexes(store.file_path), set())


_HISTORY_COUNT_TABLES = ("slot_events", "metric_samples", "metric_rollups")


@contextmanager
def _history_count_scans() -> Iterator[list[str]]:
    """Record every COUNT query on a history table, on any connection opened."""

    scans: list[str] = []
    native_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = native_connect(*args, **kwargs)

        def trace(sql: str) -> None:
            if "COUNT(" in sql.upper() and any(table in sql for table in _HISTORY_COUNT_TABLES):
                scans.append(sql)

        connection.set_trace_callback(trace)
        return connection

    with patch.object(sqlite3, "connect", side_effect=connect):
        yield scans


class SegmentedCountsDuringIndexBuildTests(unittest.TestCase):
    """#1093 review: the segmented layout's tracked-count fallback still scanned."""

    def _segmented_store(self, directory: str) -> HistoryStore:
        from tests.test_segmented_history_reader import _populate_history_source, _write_covering_catalog

        root = Path(directory)
        hot, segment, catalog = root / "hot.sqlite3", root / "segment.sqlite3", root / "catalog.json"
        _legacy_database(hot, samples=40)
        _populate_history_source(
            segment, first_id=1, samples=(("2025-01-01T00:00:00+00:00", "temperature_c"),)
        )
        _write_covering_catalog(catalog, [segment])
        with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
            store = HistoryStore(str(hot), recover_unreadable_database=False, segment_catalog_path=catalog)
        # Authenticate the catalog before tracing, as a running service has.
        store._segmented_reader()
        return store

    def _summary(self, store: HistoryStore, exact_counts: bool):
        from history_service import main as history_main

        with patch.object(history_main, "store", store):
            return asyncio.run(
                history_main.read_history_summary(exact_counts, degrade_missing_catalog=True)
            )

    def test_refused_exact_counts_scan_no_history_table_in_any_source(self) -> None:
        from history_service import main as history_main

        with tempfile.TemporaryDirectory() as directory:
            store = self._segmented_store(directory)
            with store.index_maintenance(), _history_count_scans() as scans:
                summary = self._summary(store, exact_counts=True)

            self.assertEqual(scans, [])
            self.assertEqual(summary.counts_deferred_reason, history_main.EXACT_COUNTS_DEFERRED_REASON)
            # 40 hot samples from the tracked counter, 1 recorded for the segment.
            self.assertEqual(summary.counts["metric_sample_count"], 41)
            self.assertEqual(summary.counts["count_mode"], "segmented-tracked")

    def test_default_segmented_overview_counts_without_scanning(self) -> None:
        # The default overview and the collector's metrics poll run during a
        # build too; neither may scan the hot tables the build reads.
        with tempfile.TemporaryDirectory() as directory:
            store = self._segmented_store(directory)
            with _history_count_scans() as scans:
                summary = self._summary(store, exact_counts=False)
                estimated = store.estimated_counts()

            self.assertEqual(scans, [])
            self.assertEqual(summary.counts["metric_sample_count"], 41)
            self.assertEqual(estimated["metric_sample_count"], 41)
            self.assertEqual(estimated["event_count"], 0)

    def test_scan_free_counts_still_authenticate_each_segment(self) -> None:
        # Recorded segment counts are reused only after the guarded connection
        # re-checks the file, as list_history_system_summaries does.
        from history_service.segment_reader import HistoryStorageUnavailableError

        with tempfile.TemporaryDirectory() as directory:
            store = self._segmented_store(directory)
            self.assertEqual(store.estimated_counts()["metric_sample_count"], 41)
            with closing(sqlite3.connect(Path(directory) / "segment.sqlite3")) as connection, connection:
                connection.execute("DELETE FROM metric_samples")

            with self.assertRaises(HistoryStorageUnavailableError):
                store.estimated_counts()

    def test_admitted_exact_counts_still_scan_every_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._segmented_store(directory)
            with _history_count_scans() as scans:
                summary = self._summary(store, exact_counts=True)

            self.assertTrue(scans)
            self.assertTrue(summary.counts_exact)
            self.assertIsNone(summary.counts_deferred_reason)
            self.assertEqual(summary.counts["metric_sample_count"], 41)


def _table_counter(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(
            connection.execute(
                "SELECT row_count FROM history_table_counts WHERE table_name = ?", (table,)
            ).fetchone()[0]
        )


def _set_table_counter(path: Path, table: str, value: int) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE history_table_counts SET row_count = ? WHERE table_name = ?", (value, table)
        )


class UntrustedTableCounterTests(unittest.TestCase):
    """#1093 security review: a derived counter sized index builds unchecked."""

    def test_restore_recounts_an_inflated_counter(self) -> None:
        # Two retained rows far apart: the inflated counter still fits under
        # max(rowid) - min(rowid) + 1, so only a recount can expose it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "backup.sqlite3", root / "target.sqlite3"
            _legacy_database(source, samples=2)
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.execute("UPDATE metric_samples SET id = 300001 WHERE id = 2")
            _set_table_counter(source, "metric_samples", 300_000)
            store = HistoryStore(str(target), recover_unreadable_database=False)

            store.restore_backup(source)

            self.assertEqual(_table_counter(target, "metric_samples"), 2)
            self.assertEqual(_table_counter(target, "slot_events"), 0)

    def test_startup_recounts_a_counter_above_its_rowid_span(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            _set_table_counter(path, "metric_samples", 9_000_000_000_000_000_000)
            _set_table_counter(path, "slot_events", 5)  # an empty table

            with self.assertLogs("history_service.store", "WARNING") as logs:
                HistoryStore(str(path), recover_unreadable_database=False)

            self.assertEqual(_table_counter(path, "metric_samples"), 40)
            self.assertEqual(_table_counter(path, "slot_events"), 0)
            self.assertIn("metric_samples", "\n".join(logs.output))

    def test_index_size_estimate_recounts_a_counter_above_its_rowid_span(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=40)
            HistoryStore(str(path), recover_unreadable_database=False)
            _set_table_counter(path, "metric_samples", 9_000_000_000_000_000_000)
            statement = dict(CHRONOLOGICAL_INDEXES)["idx_metric_samples_scope_chronological"]

            with (
                closing(sqlite3.connect(path)) as connection,
                self.assertLogs("history_service.store", "WARNING"),
            ):
                estimate = HistoryStore._estimate_index_bytes(connection, statement)

            self.assertLess(estimate, 40 * 200)
            self.assertEqual(_table_counter(path, "metric_samples"), 40)

    def test_index_size_estimate_counts_rows_behind_an_undersized_counter(self) -> None:
        # A too-low counter fits under every upper bound; sizing from it would
        # skip the space check entirely, so the build counts the rows it reads.
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            _set_table_counter(path, "metric_samples", 0)
            statement = dict(CHRONOLOGICAL_INDEXES)["idx_metric_samples_scope_chronological"]

            with (
                closing(sqlite3.connect(path)) as connection,
                self.assertLogs("history_service.store", "WARNING"),
            ):
                estimate = HistoryStore._estimate_index_bytes(connection, statement)
            self.assertGreater(estimate, 4_000 * 20)
            self.assertEqual(_table_counter(path, "metric_samples"), 4_000)

            _set_table_counter(path, "metric_samples", 0)
            full = shutil._ntuple_diskusage(total=1 << 30, used=1 << 30, free=0)
            with (
                patch.object(store_module.shutil, "disk_usage", return_value=full),
                self.assertLogs("history_service.store", "WARNING"),
                self.assertRaises(store_module.HistoryIndexBuildSpaceError),
            ):
                store.build_chronological_index("idx_metric_samples_scope_chronological")

    def test_index_size_estimate_never_exceeds_the_database_file(self) -> None:
        # Two rows can stretch the rowid span and the counter; the estimate
        # stays bounded by the rows counted and by the file's own pages.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=2)
            HistoryStore(str(path), recover_unreadable_database=False)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE metric_samples SET id = ? WHERE id = 2", (1 << 62,))
            _set_table_counter(path, "metric_samples", 1 << 62)
            statement = dict(CHRONOLOGICAL_INDEXES)["idx_metric_samples_scope_chronological"]

            with closing(sqlite3.connect(path)) as connection:
                estimate = HistoryStore._estimate_index_bytes(connection, statement)
                pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])

            self.assertLessEqual(estimate, 2 * pages * page_size)

    def test_a_space_refusal_keeps_collecting_and_retries_the_build_later(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            _legacy_database(path, samples=4_000)
            with patch.object(store_module, "CHRONOLOGICAL_INDEX_INLINE_MAX_ROWS", 10):
                store = HistoryStore(str(path), recover_unreadable_database=False)
            collector = HistoryCollector(
                HistorySettings(sqlite_path=str(path), startup_grace_seconds=0), store
            )
            full = shutil._ntuple_diskusage(total=1 << 30, used=1 << 30, free=0)
            roomy = shutil._ntuple_diskusage(total=1 << 40, used=0, free=1 << 40)
            disk = {"usage": full}
            events: list[str] = []
            reasons: list[str | None] = []
            original_build = store.build_chronological_index
            original_wait_for = asyncio.wait_for

            def build(name: str, **kwargs) -> None:
                events.append("build")
                original_build(name, **kwargs)

            def collect(**_kwargs) -> None:
                events.append("collect")
                reasons.append(collector.degraded_reason())
                collects = events.count("collect")
                if collects == 2:
                    # Space is freed and the slower retry comes due.
                    disk["usage"] = roomy
                    collector._index_build_space_retry_at = 0.0
                elif collects == 3:
                    collector._stopping.set()

            async def short_wait(awaitable, timeout):
                return await original_wait_for(awaitable, min(timeout, 0.001))

            with (
                patch.object(store_module.shutil, "disk_usage", side_effect=lambda _path: disk["usage"]),
                patch.object(collector_module.asyncio, "wait_for", side_effect=short_wait),
                patch.object(store, "build_chronological_index", side_effect=build),
                patch.object(collector, "run_once", side_effect=collect),
                self.assertLogs("history_service.collector", "WARNING"),
                self.assertLogs("history_service.store", "WARNING"),
            ):
                asyncio.run(original_wait_for(collector._run_loop(), timeout=30))

            # The empty slot_events index builds, the next is refused, and
            # collection continues; the build is not retried before it is due.
            self.assertEqual(
                events, ["build", "build", "collect", "collect", *["build"] * 4, "collect"]
            )
            self.assertEqual(
                reasons,
                [collector_module.INDEX_BUILD_SPACE_DEGRADED_REASON] * 2 + [None],
            )
            self.assertEqual(_indexes(path), CHRONOLOGICAL_NAMES)


if __name__ == "__main__":
    unittest.main()
