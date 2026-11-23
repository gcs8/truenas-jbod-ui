"""Bound mapping writes before executor submission. Synthetic local files only."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import AsyncMock, Mock, patch

from app import main as app_main
from app import routes as app_routes
from app.read_ui_auth_config import ReadUiAuthSettings
from app.models.domain import MappingBundle
from app.services.inventory import SnapshotStateBusyError, _run_mapping_store_write
from app.services.mapping_store import MappingStore, bind_mapping_revision
from tests.test_mapping_export_preview_off_loop import _CountingExecutor, bare_service, seeded_store

ADMITTED = 2
WAITING = 8
BUSY_DETAIL = "The server is busy. Try again in a moment."


@contextmanager
def held_writer(store: MappingStore):
    """Hold the real transaction from another thread, without executor work."""
    entered, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with store._writer_transaction():
                entered.set()
                if not release.wait(15):
                    raise AssertionError("writer fixture was not released")
        except BaseException as exc:
            errors.append(exc)
            entered.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        if not entered.wait(5) or errors:
            raise AssertionError(f"writer fixture did not acquire its lock: {errors}")
        yield release
    finally:
        release.set()
        thread.join(5)
        if thread.is_alive() or errors:
            raise AssertionError(f"writer fixture did not finish: {errors}")


async def settle():
    # Give admitted calls time to reach the held transaction and refusals to finish.
    for _ in range(30):
        await asyncio.sleep(0.01)


class MappingWriteAdmissionTests(unittest.TestCase):
    def run_gated(self, scenario):
        with tempfile.TemporaryDirectory() as directory:
            path, store = seeded_store(directory)
            store.writer_lock_timeout_seconds = 10
            service = bare_service(store)
            executor = _CountingExecutor()
            with held_writer(MappingStore(path)) as release:
                async def main():
                    asyncio.get_running_loop().set_default_executor(executor)
                    tasks = []
                    try:
                        return await scenario(service, release, executor, tasks)
                    finally:
                        release.set()
                        await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    return asyncio.run(main())
                finally:
                    executor.shutdown(wait=True)

    @staticmethod
    def save(service, slot):
        return service.save_mapping(slot, {"notes": f"saved {slot}"}, selected_enclosure_id="shelf-a")

    def test_repeated_bursts_refuse_overflow_without_starving_unrelated_executor_work(self):
        async def scenario(service, release, executor, tasks):
            observations = []
            for batch in range(3):
                tasks.extend(asyncio.create_task(self.save(service, slot)) for slot in range(batch * 8, batch * 8 + 8))
                await settle()
                observations.append((executor.submitted, executor.queued))
            refused = [task.exception() for task in tasks if task.done() and not task.cancelled()]
            unrelated = asyncio.get_running_loop().run_in_executor(None, lambda: "unrelated")
            done, _ = await asyncio.wait({unrelated}, timeout=0.5)
            release.set()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            await unrelated
            await self.save(service, 30)
            rows = service.mapping_store.list_mappings("sys-a", "shelf-a")
            return observations, refused, bool(done), outcomes, rows

        observations, refused, unrelated_done, outcomes, rows = self.run_gated(scenario)
        with self.subTest(check="executor bound"):
            self.assertEqual(observations, [(ADMITTED, 0)] * 3, "writes queued unbounded executor work")
        with self.subTest(check="busy refusal"):
            self.assertEqual(len(refused), 24 - ADMITTED - WAITING)
            for error in refused:
                self.assertIsInstance(error, SnapshotStateBusyError)
                self.assertEqual((str(error), error.reason), (BUSY_DETAIL, "mapping-write-admission"))
        with self.subTest(check="unrelated work"):
            self.assertTrue(unrelated_done, "mapping writes starved unrelated executor work")
        successful = [result for result in outcomes if not isinstance(result, BaseException)]
        self.assertEqual(len(successful), ADMITTED + WAITING)
        self.assertEqual(sorted(row.slot for row in rows), list(range(10)) + [30])

    def test_cancelled_waiters_submit_nothing_and_running_callers_keep_slots_until_commit(self):
        async def scenario(service, release, executor, tasks):
            running = [asyncio.create_task(self.save(service, slot)) for slot in (10, 11)]
            tasks.extend(running)
            await settle()
            waiting = [asyncio.create_task(self.save(service, slot)) for slot in range(20, 28)]
            tasks.extend(waiting)
            await settle()
            for task in waiting:
                task.cancel()
            await settle()
            waiters_gone = all(task.done() for task in waiting)
            after_waiters = (executor.submitted, executor.queued)
            for _ in range(2):
                for task in running:
                    task.cancel()
                await settle()
            retained = all(not task.done() for task in running)
            late = asyncio.create_task(self.save(service, 30))
            tasks.append(late)
            await settle()
            after_cancel = (executor.submitted, executor.queued, late.done())
            release.set()
            outcomes = await asyncio.gather(*running, *waiting, return_exceptions=True)
            await late
            await self.save(service, 31)
            rows = service.mapping_store.list_mappings("sys-a", "shelf-a")
            return waiters_gone, after_waiters, retained, after_cancel, outcomes, service, executor.submitted, rows

        gone, after_waiters, retained, after_cancel, outcomes, service, submitted, rows = self.run_gated(scenario)
        with self.subTest(check="cancelled waiters"):
            self.assertTrue(gone, "cancelled waiters retained executor jobs")
            self.assertEqual(after_waiters, (ADMITTED, 0))
        with self.subTest(check="retained admission"):
            self.assertTrue(retained, "cancelled callers abandoned running writes")
            self.assertEqual(after_cancel, (ADMITTED, 0, False), "cancellation released admission before commit")
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in outcomes))
        self.assertEqual(submitted, ADMITTED + 2, "cancelled waiters submitted writes after release")
        self.assertEqual(service.invalidate_physical_enclosure_snapshot_cache.call_count, 4)
        self.assertEqual(sorted(row.slot for row in rows), [1, 2, 10, 11, 30, 31])

    def test_clear_and_import_share_write_admission_and_recover_with_original_results(self):
        async def scenario(service, release, executor, tasks):
            bundle = MappingBundle(mappings=[])
            preview = await service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a")
            tasks.extend(asyncio.create_task(self.save(service, slot)) for slot in range(10, 20))
            await settle()
            refusals = []
            for operation in (
                lambda: service.clear_mapping(1, selected_enclosure_id="shelf-a"),
                lambda: service.import_mapping_bundle(
                    bundle, selected_enclosure_id="shelf-a", expected_revision=preview["revision"],
                    import_digest=preview["import_digest"],
                ),
            ):
                attempt = asyncio.create_task(operation())
                tasks.append(attempt)
                done, _ = await asyncio.wait({attempt}, timeout=0.5)
                refusals.append(attempt.exception() if done else None)
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.save(service, 1)
            cleared = await service.clear_mapping(1, selected_enclosure_id="shelf-a")
            preview = await service.preview_mapping_bundle(bundle, selected_enclosure_id="shelf-a")
            imported = await service.import_mapping_bundle(
                bundle, selected_enclosure_id="shelf-a", expected_revision=preview["revision"],
                import_digest=preview["import_digest"],
            )
            return refusals, cleared, imported, service.mapping_store.list_mappings()

        refusals, cleared, imported, rows = self.run_gated(scenario)
        for operation, error in zip(("clear", "import"), refusals):
            with self.subTest(operation=operation):
                self.assertIsInstance(error, SnapshotStateBusyError, "write bypassed shared admission")
                self.assertEqual(error.reason, "mapping-write-admission")
        self.assertTrue(cleared)
        self.assertEqual(imported["imported"], 0)
        self.assertEqual(rows, [])

    def test_save_route_returns_existing_503_and_retry_after_then_succeeds(self):
        async def request(revision):
            sent = []
            payload = json.dumps({
                "notes": "route save", "expected_revision": revision, "clear_identify_after_save": False,
            }).encode()
            delivered = False
            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await asyncio.Future()
            async def send(message):
                sent.append(message)
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/api/slots/31/mapping",
                "raw_path": b"/api/slots/31/mapping", "query_string": b"system_id=sys-a&enclosure_id=shelf-a",
                "root_path": "", "headers": [(b"host", b"synthetic.example.test"),
                    (b"origin", b"http://synthetic.example.test"), (b"content-type", b"application/json")],
                "server": ("synthetic.example.test", 80), "client": ("synthetic.example.test", 1234),
            }
            await app_main.app(scope, receive, send)
            start = next(message for message in sent if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
            return start["status"], dict(start["headers"]), json.loads(body)

        async def scenario(service, release, executor, tasks):
            snapshot = await service._get_pre_write_snapshot("shelf-a")
            service.peek_cached_snapshot = Mock(return_value=snapshot)
            revision = bind_mapping_revision(service.mapping_store.save_revision("sys-a", "shelf-a", 31),
                                             service._slot_disk_binding(None))
            registry = Mock()
            registry.get_service.return_value = service
            tasks.extend(asyncio.create_task(self.save(service, slot)) for slot in range(10, 20))
            await settle()
            with (
                patch.object(app_routes, "get_inventory_registry", return_value=registry),
                patch.object(app_routes, "ensure_slot_bounds", new=AsyncMock()),
                patch.object(app_routes, "readback_after_write", new=AsyncMock(return_value=(snapshot, None))),
                patch.object(app_main.app.state, "operator_auth_settings", ReadUiAuthSettings(
                    auth_mode="network", allowed_hosts="synthetic.example.test",
                )),
                patch.object(app_main.app.state, "read_ui_public_origin", "http://synthetic.example.test"),
            ):
                attempt = asyncio.create_task(request(revision))
                tasks.append(attempt)
                done, _ = await asyncio.wait({attempt}, timeout=0.5)
                refusal = attempt.result() if done else (None, {}, {})
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                revision = bind_mapping_revision(service.mapping_store.save_revision("sys-a", "shelf-a", 31),
                                                 service._slot_disk_binding(None))
                served = await request(revision)
            return refusal, served

        refusal, served = self.run_gated(scenario)
        self.assertEqual(refusal[0], 503, "the saturated write route did not refuse promptly")
        self.assertEqual(refusal[1].get(b"retry-after"), b"1")
        self.assertEqual(refusal[2].get("detail"), BUSY_DETAIL)
        self.assertEqual(served[0], 200)
        self.assertEqual(served[2]["mapping"]["notes"], "route save")


class MappingWriteOwnershipControls(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_waiter_handoff_and_cancellation_before_submission(self):
        loop = asyncio.get_running_loop()
        for cancel_handoff in (False, True):
            with self.subTest(cancel_handoff=cancel_handoff):
                workers, submitted, tasks = {}, [], []
                committed = Mock()

                def submit(_executor, _context_run, write):
                    name = write()
                    submitted.append(name)
                    workers[name] = loop.create_future()
                    return workers[name]

                with patch.object(loop, "run_in_executor", side_effect=submit):
                    try:
                        for name in range(5):
                            tasks.append(asyncio.create_task(_run_mapping_store_write(
                                lambda name=name: name, committed,
                            )))
                        await settle()
                        self.assertEqual(submitted, [0, 1], "waiting writes reached the executor")
                        workers[0].set_result(0)
                        if cancel_handoff:
                            # Completion hands off the turn, then cancellation runs
                            # before the recipient resumes and can submit its work.
                            loop.call_soon(tasks[2].cancel)
                        await settle()
                        expected = 3 if cancel_handoff else 2
                        self.assertEqual(submitted, [0, 1, expected])
                        workers[expected].set_result(expected)
                        await settle()
                        if not cancel_handoff:
                            self.assertEqual(submitted, [0, 1, 2, 3])
                            workers[3].set_result(3)
                            await settle()
                        self.assertEqual(submitted[-1], 4, "waiter order or handed-turn recovery was lost")
                    finally:
                        for worker in workers.values():
                            if not worker.done():
                                worker.set_result("finished")
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_results_errors_context_and_submission_failure_release_admission(self):
        value = ContextVar("synthetic_mapping_context", default="missing")
        token = value.set("present")
        committed = Mock()
        loop = asyncio.get_running_loop()
        error = PermissionError("synthetic write failure")
        try:
            with patch.object(loop, "run_in_executor", side_effect=error):
                with self.assertRaises(PermissionError) as raised:
                    await _run_mapping_store_write(lambda: "unused", committed)
            self.assertIs(raised.exception, error)
            for _ in range(ADMITTED + 1):
                def fail():
                    raise error
                with self.assertRaises(PermissionError) as raised:
                    await _run_mapping_store_write(fail, committed)
                self.assertIs(raised.exception, error)
            self.assertEqual(await _run_mapping_store_write(value.get, committed), "present")
            committed.assert_not_called()
        finally:
            value.reset(token)

    async def test_cancellation_drains_late_error_without_invalidating_or_leaking_capacity(self):
        entered, release = threading.Event(), threading.Event()
        committed = Mock()
        def fail():
            entered.set()
            release.wait(5)
            raise PermissionError("synthetic late failure")
        task = asyncio.create_task(_run_mapping_store_write(fail, committed))
        try:
            for _ in range(500):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(entered.is_set())
            task.cancel()
            await settle()
            self.assertFalse(task.done())
        finally:
            release.set()
            outcome, = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcome, asyncio.CancelledError)
        committed.assert_not_called()
        for _ in range(ADMITTED + 1):
            self.assertEqual(await _run_mapping_store_write(lambda: "recovered", committed), "recovered")


if __name__ == "__main__":
    unittest.main()
