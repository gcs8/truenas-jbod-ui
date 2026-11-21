"""Mapping export, import preview and the import's pre-check read run off the event loop.

A slow mapping-file read must not stall other requests on the same loop. The
read runs on a worker thread that the request keeps until it finishes, even
through cancellation, and readers still never wait for a writer lock. The
result and the errors the read raises are unchanged. Admission is bounded
before work reaches the shared executor: a burst beyond the bound is refused
as busy, and a cancelled caller keeps its admission until its worker ends.
Synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.config import SystemConfig
from app.models.domain import InventorySnapshot, ManualMapping, MappingBundle
from app.services import mapping_store as mapping_store_module
from app import main as app_main
from app import routes as app_routes
from app.services.inventory import InventoryService, SnapshotStateBusyError
from app.services.mapping_store import (
    MappingReadBusy,
    MappingRevisionConflict,
    MappingScopeConflict,
    MappingStore,
)

# A loop that runs the read itself cannot release it; only this outside timer
# can, long after a read on a worker thread was released by the loop's timer.
RESCUE_SECONDS = 1.0


def mapping(slot: int, notes: str) -> ManualMapping:
    return ManualMapping(system_id="sys-a", enclosure_id="shelf-a", slot=slot, notes=notes)


def seeded_store(directory: str) -> tuple[Path, MappingStore]:
    path = Path(directory) / "slot_mappings.json"
    store = MappingStore(path)
    store.save_mapping(mapping(1, "first"))
    store.save_mapping(mapping(2, "second"))
    return path, store


def bare_service(store: MappingStore) -> InventoryService:
    """The real async mapping methods; appliance reads replaced (an empty bundle reads none)."""
    service = object.__new__(InventoryService)
    service.system = SystemConfig(id="sys-a")
    service.mapping_store = store
    service._get_pre_write_snapshot = AsyncMock(
        return_value=InventorySnapshot(
            slots=[], layout_rows=[], layout_slot_count=0, layout_columns=1, refresh_interval_seconds=30,
        )
    )
    service._read_mapping_import_bays = AsyncMock(return_value={})
    service.invalidate_physical_enclosure_snapshot_cache = Mock()
    service.invalidate_snapshot_cache = Mock()
    return service


class _GatedLstat:
    """Hold the mapping file's ``lstat`` until released; record the reading thread."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []
        self._original = os.lstat

    def __call__(self, name, *args, **kwargs):
        if Path(name) == self.path and not self.release.is_set():
            self.threads.append(threading.current_thread())
            self.entered.set()
            self.release.wait(timeout=10)
        return self._original(name, *args, **kwargs)

    def patch(self):
        return patch.object(mapping_store_module.os, "lstat", self)


def operations(service: InventoryService, revision: str):
    bundle = MappingBundle(mappings=[])
    return {
        "export": lambda: service.export_mapping_bundle(selected_enclosure_id="shelf-a"),
        "preview": lambda: service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a"),
        # A stale confirmation is refused by the pre-check read, before any write.
        "import_precheck": lambda: service.import_mapping_bundle(
            bundle, selected_enclosure_id="shelf-a", expected_revision="0" * len(revision),
            import_digest="0" * 64,
        ),
    }


class MappingReadsLeaveTheLoopFreeTests(unittest.TestCase):
    def run_with_heartbeat(self, gate: _GatedLstat, read):
        """Run ``read`` while another request's 20 ms timer is due; it releases the read."""
        loop_threads: list[threading.Thread] = []

        async def scenario():
            loop = asyncio.get_running_loop()
            loop_threads.append(threading.current_thread())
            started = loop.time()
            ticks: list[float] = []

            def other_request() -> None:
                ticks.append(loop.time() - started)
                gate.release.set()

            loop.call_later(0.02, other_request)
            try:
                result = await read()
            except Exception as exc:  # the error mapping is checked by the caller
                result = exc
            return ticks, result

        rescue = threading.Timer(RESCUE_SECONDS, gate.release.set)
        rescue.start()
        try:
            with gate.patch():
                ticks, result = asyncio.run(scenario())
        finally:
            gate.release.set()
            rescue.cancel()
            rescue.join()
        return ticks, result, loop_threads[0]

    def test_a_slow_mapping_read_does_not_block_another_request_on_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision = store.scope_revision("sys-a", "shelf-a")
            for name, read in operations(bare_service(MappingStore(path)), revision).items():
                with self.subTest(operation=name):
                    gate = _GatedLstat(path)
                    ticks, result, loop_thread = self.run_with_heartbeat(gate, read)
                    self.assertTrue(gate.threads, "the operation never read the mapping file")
                    self.assertNotIn(
                        loop_thread, gate.threads, f"{name} read the mapping file on the event loop thread",
                    )
                    self.assertTrue(ticks, "the other request's timer never ran")
                    self.assertLess(
                        ticks[0], RESCUE_SECONDS / 2,
                        f"{name} blocked the event loop until the outside rescue released its read",
                    )
                    if name == "import_precheck":
                        self.assertIsInstance(result, MappingRevisionConflict)
                    else:
                        self.assertNotIsInstance(result, Exception)

    def test_a_cancelled_request_keeps_its_read_until_the_worker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision = store.scope_revision("sys-a", "shelf-a")
            for name, read in operations(bare_service(MappingStore(path)), revision).items():
                with self.subTest(operation=name):
                    gate = _GatedLstat(path)

                    async def scenario():
                        loop = asyncio.get_running_loop()
                        task = asyncio.ensure_future(read())
                        # Off the loop, the read waits in its worker while the loop runs.
                        entered = await loop.run_in_executor(None, gate.entered.wait, 5)
                        self.assertTrue(entered, f"{name} never reached its read")
                        task.cancel()
                        await asyncio.sleep(0.05)
                        still_owned = not task.done()
                        gate.release.set()
                        (outcome,) = await asyncio.gather(task, return_exceptions=True)
                        return still_owned, outcome

                    rescue = threading.Timer(RESCUE_SECONDS, gate.release.set)
                    rescue.start()
                    try:
                        with gate.patch():
                            still_owned, outcome = asyncio.run(scenario())
                    finally:
                        gate.release.set()
                        rescue.cancel()
                        rescue.join()
                    self.assertNotIn(threading.main_thread(), gate.threads, f"{name} read on the event loop thread")
                    self.assertIsInstance(
                        outcome, asyncio.CancelledError, f"{name} could not be cancelled during its read",
                    )
                    self.assertTrue(still_owned, f"{name} let go of its read while the worker still ran")


class MappingReadResultsAndErrorsUnchangedTests(unittest.TestCase):
    """Controls: off the loop, the same results and the same errors as before."""

    def test_export_and_preview_return_the_published_rows_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            service = bare_service(MappingStore(path))
            bundle = asyncio.run(service.export_mapping_bundle(selected_enclosure_id="shelf-a"))
            self.assertEqual([(row.slot, row.notes) for row in bundle.mappings], [(1, "first"), (2, "second")])
            self.assertEqual(bundle.revision, store.scope_revision("sys-a", "shelf-a"))
            self.assertEqual((bundle.system_id, bundle.enclosure_id), ("sys-a", "shelf-a"))

            preview = asyncio.run(service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a"))
            expected = store.preview_replace_mappings(
                "sys-a", "shelf-a", service._rewrite_mapping_bundle(bundle, "shelf-a"),
            )
            self.assertEqual(preview["revision"], expected["revision"])
            self.assertEqual(preview["revision"], bundle.revision)
            for key in ("additions", "updates", "removals"):
                self.assertEqual(preview[key], expected[key])

    def test_store_errors_reach_the_caller_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, _store = seeded_store(directory)
            for error in (MappingScopeConflict(), MappingReadBusy(path), PermissionError("synthetic")):
                service = bare_service(MappingStore(path))
                for name, read in operations(service, "0" * 16).items():
                    with (
                        self.subTest(operation=name, error=type(error).__name__),
                        patch.object(MappingStore, "_read_source_bytes", side_effect=error),
                    ):
                        with self.assertRaises(type(error)) as raised:
                            asyncio.run(read())
                        self.assertIs(raised.exception, error)


# The admission contract: at most this many mapping reads in the shared default
# executor at once, at most this many callers waiting for a turn, busy beyond.
ADMITTED_READS = 2
WAITING_READS = 8
EXECUTOR_WORKERS = 4  # more than the admitted reads, as every real default executor
BUSY_DETAIL = "The server is busy. Try again in a moment."


class _CountingExecutor(ThreadPoolExecutor):
    """A default executor that counts submissions and exposes its queue."""

    def __init__(self) -> None:
        super().__init__(max_workers=EXECUTOR_WORKERS)
        self.submitted = 0

    def submit(self, *args, **kwargs):
        self.submitted += 1
        return super().submit(*args, **kwargs)

    @property
    def queued(self) -> int:
        return self._work_queue.qsize()


class MappingReadAdmissionTests(unittest.TestCase):
    """Bursts, cancellation and recovery against the real service and store."""

    def run_gated(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            revision = store.scope_revision("sys-a", "shelf-a")
            service = bare_service(MappingStore(path))
            gate = _GatedLstat(path)
            executor = _CountingExecutor()

            async def main():
                asyncio.get_running_loop().set_default_executor(executor)
                return await scenario(service, revision, gate, executor)

            rescue = threading.Timer(5.0, gate.release.set)
            rescue.start()
            try:
                with gate.patch():
                    return asyncio.run(main())
            finally:
                gate.release.set()
                rescue.cancel()
                rescue.join()
                executor.shutdown(wait=True)

    @staticmethod
    async def settle() -> None:
        # Let admitted callers reach their gated reads and refused ones finish.
        for _ in range(50):
            await asyncio.sleep(0.01)

    def test_a_burst_beyond_the_bound_is_refused_as_busy_not_queued_in_the_executor(self) -> None:
        async def scenario(service, revision, gate, executor):
            reads = list(operations(service, revision).values())
            tasks = [asyncio.ensure_future(reads[index % len(reads)]()) for index in range(24)]
            await self.settle()
            submitted, queued = executor.submitted, executor.queued
            refused = [
                task for task in tasks
                if task.done() and not task.cancelled() and isinstance(task.exception(), SnapshotStateBusyError)
            ]
            # Unrelated work in the same default executor still gets a thread.
            unrelated = asyncio.get_running_loop().run_in_executor(None, lambda: "unrelated done")
            done, _ = await asyncio.wait({unrelated}, timeout=1.0)
            gate.release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            await unrelated
            recovered = await service.export_mapping_bundle(selected_enclosure_id="shelf-a")
            return submitted, queued, refused, bool(done), outcomes, recovered

        submitted, queued, refused, unrelated_done, outcomes, recovered = self.run_gated(scenario)
        with self.subTest(check="executor submissions"):
            self.assertLessEqual(submitted, ADMITTED_READS, "mapping reads beyond the bound reached the executor")
        with self.subTest(check="executor queue"):
            self.assertEqual(queued, 0, "mapping reads were left queued in the shared executor")
        with self.subTest(check="refused as busy"):
            self.assertEqual(
                len(refused), 24 - ADMITTED_READS - WAITING_READS, "the burst beyond the bound was not refused",
            )
            for error in refused:
                self.assertEqual(str(error.exception()), BUSY_DETAIL)
        with self.subTest(check="unrelated executor work"):
            self.assertTrue(unrelated_done, "mapping reads starved unrelated default-executor work")
        admitted = [outcome for outcome in outcomes if not isinstance(outcome, SnapshotStateBusyError)]
        self.assertEqual(len(admitted), ADMITTED_READS + WAITING_READS)
        for outcome in admitted:
            # Admitted callers get the store's own answer: rows, or the stale pre-check refusal.
            if isinstance(outcome, BaseException):
                self.assertIsInstance(outcome, MappingRevisionConflict)
        self.assertEqual([row.slot for row in recovered.mappings], [1, 2], "no recovery after the burst drained")

    def test_cancelled_callers_leave_no_executor_jobs_and_keep_admission_until_their_worker_ends(self) -> None:
        async def scenario(service, revision, gate, executor):
            export = operations(service, revision)["export"]
            running = [asyncio.ensure_future(export()) for _ in range(ADMITTED_READS)]
            await self.settle()
            waiting = [asyncio.ensure_future(export()) for _ in range(WAITING_READS)]
            await self.settle()
            for task in waiting:
                task.cancel()
            await self.settle()
            waiters_gone = all(task.done() for task in waiting)
            after_waiters = (executor.submitted, executor.queued)
            # Cancel the admitted callers too; their workers are still in the read.
            for task in running:
                task.cancel()
            await self.settle()
            running_retained = not any(task.done() for task in running)
            late = asyncio.ensure_future(export())
            await self.settle()
            after_running = (executor.submitted, executor.queued, late.done())
            gate.release.set()
            running_outcomes = await asyncio.gather(*running, return_exceptions=True)
            late_bundle = await late
            recovered = await service.export_mapping_bundle(selected_enclosure_id="shelf-a")
            return (
                waiters_gone, after_waiters, running_retained, after_running,
                running_outcomes, late_bundle, recovered, executor.submitted,
            )

        (
            waiters_gone, after_waiters, running_retained, after_running,
            running_outcomes, late_bundle, recovered, total_submitted,
        ) = self.run_gated(scenario)
        with self.subTest(check="cancelled waiters"):
            self.assertTrue(waiters_gone, "cancelled waiting callers were kept behind queued executor jobs")
            self.assertEqual(after_waiters, (ADMITTED_READS, 0), "cancelled waiting callers left executor jobs")
        with self.subTest(check="cancelled admitted callers"):
            self.assertTrue(running_retained, "a cancelled caller let go of its read while the worker still ran")
            self.assertEqual(
                after_running, (ADMITTED_READS, 0, False),
                "admission was released on caller cancellation, before the worker finished",
            )
        for outcome in running_outcomes:
            self.assertIsInstance(outcome, asyncio.CancelledError)
        self.assertEqual([row.slot for row in late_bundle.mappings], [1, 2])
        self.assertEqual([row.slot for row in recovered.mappings], [1, 2], "no recovery after cancellation")
        self.assertEqual(total_submitted, ADMITTED_READS + 2)

    def test_the_export_route_answers_a_refused_read_with_the_existing_busy_response(self) -> None:
        async def request() -> tuple[int, dict[bytes, bytes], dict]:
            sent: list[dict] = []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Future()

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": "/api/mappings/export",
                "raw_path": b"/api/mappings/export", "query_string": b"system_id=sys-a&enclosure_id=shelf-a",
                "root_path": "", "headers": [(b"host", b"synthetic")], "server": ("synthetic", 80),
                "client": ("synthetic", 1234),
            }
            await app_main.app(scope, receive, send)
            start = next(message for message in sent if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            return start["status"], dict(start["headers"]), json.loads(body)

        async def scenario(service, revision, gate, executor):
            export = operations(service, revision)["export"]
            held = [asyncio.ensure_future(export()) for _ in range(ADMITTED_READS + WAITING_READS)]
            await self.settle()
            attempt = asyncio.ensure_future(request())
            done, _ = await asyncio.wait({attempt}, timeout=1.0)
            refused = attempt.result() if done else (None, {}, {})
            gate.release.set()
            await asyncio.gather(*held, attempt)
            served = await request()
            return refused, served

        registry = Mock()

        async def scenario_with_registry(service, revision, gate, executor):
            registry.get_service.return_value = service
            return await scenario(service, revision, gate, executor)

        with patch.object(app_routes, "get_inventory_registry", return_value=registry):
            refused, served = self.run_gated(scenario_with_registry)
        status, headers, body = refused
        self.assertEqual(status, 503)
        self.assertEqual(body.get("detail"), BUSY_DETAIL)
        self.assertEqual(headers.get(b"retry-after"), b"1")
        self.assertEqual(served[0], 200)
        self.assertEqual([row["slot"] for row in served[2]["mappings"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
