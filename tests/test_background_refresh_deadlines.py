"""A refresh that never finishes must not hang user requests (#1125).

User reloads and bay-assignment saves wait for the app's own background
inventory refresh (#1113, #1121). These tests replace the appliance read with a
call that never answers, and check that every request that waits on it answers
within a bounded wait, that the refresh itself is abandoned after its deadline,
that an abandoned refresh never publishes a late result over newer state, and
that no step exceeds the appliance call budget.

Every id here is synthetic. The appliance call holds a slot of a real
`MiddlewareCallLimiter` until it is answered, even when the caller stops
waiting, as a real sent call keeps its permit until the reply arrives.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app import main as app_main
from app import routes as app_routes
from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import SourceStatus
from app.services import inventory as inventory_module
from app.services.parsers import ParsedSSHData
from app.services.truenas_ws import MiddlewareCallLimiter, TrueNASRawData
from tests.test_background_refresh_busy_routes import (
    SHELF,
    SSH_OUTPUTS,
    SYSTEM_ID,
    build_service,
    call_app,
)

# Short bounds keep the suite fast; production values are module constants.
USER_WAIT_SECONDS = 0.3
REFRESH_DEADLINE_SECONDS = 0.6
# Generous ceiling for "answered within the bound" on a loaded CI runner.
ANSWER_CEILING_SECONDS = 3.0
LATE_MARKER = "synthetic late appliance read"


async def wait_for_condition(condition, *, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.01)


class HungAppliance:
    """One appliance whose next call can be made to hang until answered."""

    def __init__(self, limit: int = 1) -> None:
        self.limiter = MiddlewareCallLimiter(limit)
        self.reads = 0
        self.answer = asyncio.Event()
        self.hung = asyncio.Event()
        self.hang_next = False
        # A worker that cannot stop once started (for example a blocked
        # thread) keeps running after cancellation and finishes late.
        self.ignore_cancellation = False
        self.late_finished = asyncio.Event()
        self.calls: list[asyncio.Future] = []

    def bundle(self, *, late: bool = False) -> inventory_module.InventorySourceBundle:
        return inventory_module.InventorySourceBundle(
            raw_data=TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]),
            ssh_outputs=dict(SSH_OUTPUTS),
            ssh_collected=True,
            warnings=[LATE_MARKER] if late else [],
            sources={
                "api": SourceStatus(enabled=True, ok=False, message="API unreachable"),
                "ssh": SourceStatus(enabled=True, ok=True),
            },
            scale_ses_data=ParsedSSHData(),
            quantastor_ses_data=ParsedSSHData(),
        )

    async def _wire_call(self, hang: bool) -> inventory_module.InventorySourceBundle:
        async with self.limiter.slot():
            self.reads += 1
            if hang:
                self.hung.set()
                await self.answer.wait()
            return self.bundle(late=hang)

    async def collect(self) -> inventory_module.InventorySourceBundle:
        hang, self.hang_next = self.hang_next, False
        call = asyncio.ensure_future(self._wire_call(hang))
        self.calls.append(call)
        if not (hang and self.ignore_cancellation):
            # The sent call keeps its permit until answered; only this waiter stops.
            return await inventory_module._await_retained(call)
        while not call.done():
            try:
                await asyncio.wait({call})
            except asyncio.CancelledError:
                pass
        self.late_finished.set()
        return call.result()

    async def settle(self) -> None:
        self.answer.set()
        if self.calls:
            await asyncio.wait_for(asyncio.gather(*self.calls, return_exceptions=True), timeout=5)


class RefreshDeadlineTests(unittest.IsolatedAsyncioTestCase):
    limit = 1

    async def asyncSetUp(self) -> None:
        for name, value in (
            ("SNAPSHOT_REFRESH_WAIT_SECONDS", USER_WAIT_SECONDS),
            ("BACKGROUND_REFRESH_DEADLINE_SECONDS", REFRESH_DEADLINE_SECONDS),
        ):
            patcher = patch.object(inventory_module, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.appliance = HungAppliance(limit=self.limit)
        self.service = build_service(directory.name, self.appliance)
        registry = Mock()
        registry.get_service.return_value = self.service
        registry.has_system.return_value = True
        settings = Settings(
            systems=[SystemConfig(id=SYSTEM_ID, label="Synthetic SCALE", truenas=TrueNASConfig(platform="scale"))],
            default_system_id=SYSTEM_ID,
        )
        release_service = Mock()
        release_service.snapshot.return_value = {}
        for patcher in (
            patch.object(app_routes, "get_inventory_registry", return_value=registry),
            patch.object(app_routes, "get_settings", return_value=settings),
            patch.object(app_routes, "get_release_status_service", return_value=release_service),
            patch.object(app_routes, "resolve_admin_launch_url", return_value=None),
            patch.object(app_routes, "runtime_warnings_for", return_value=[]),
            patch.object(app_routes.upgrade_notice, "current_notice", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = registry
        self.application = app_main.create_app()
        self.addAsyncCleanup(self.settle)
        # The page has loaded once: the shelf's snapshot and sources are cached.
        await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        self.assertEqual(self.appliance.reads, 1)

    async def settle(self) -> None:
        await self.appliance.settle()
        tasks = [
            task for task in (
                self.service._source_bundle_refresh_task,
                *self.service._snapshot_refresh_tasks.values(),
                *getattr(self.service, "_background_refresh_workers", {}),
            )
            if task is not None
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    def expire_snapshot(self) -> None:
        self.service._cache_until[SHELF] = datetime.min.replace(tzinfo=timezone.utc)

    def expire_sources(self) -> None:
        self.service._source_bundle_until = datetime.min.replace(tzinfo=timezone.utc)

    async def start_hung_page_refresh(self) -> None:
        """A stale page read starts the page's own refresh, whose appliance call hangs."""
        self.expire_snapshot()
        self.expire_sources()
        self.appliance.hang_next = True
        await self.service.get_snapshot(selected_enclosure_id=SHELF, allow_stale_cache=True)
        await asyncio.wait_for(self.appliance.hung.wait(), timeout=2)

    async def start_hung_source_refresh(self) -> None:
        """The page's shared source refresh is running, and its appliance call hangs."""
        self.expire_sources()
        self.appliance.hang_next = True
        self.service._schedule_background_source_bundle_refresh()
        await asyncio.wait_for(self.appliance.hung.wait(), timeout=2)

    def assert_budget_respected(self) -> None:
        self.assertLessEqual(self.appliance.limiter.peak_active, self.appliance.limiter.limit)


class UserRequestsWaitingOnAHungRefreshTests(RefreshDeadlineTests):
    async def test_twenty_four_concurrent_requests_all_answer_within_the_wait_bound(self) -> None:
        # Long enough that only the user-facing wait bound can answer them.
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            # Another tab saved a name, so no cached view of the shelf remains
            # and every request below must wait on the hung refresh.
            self.service.invalidate_physical_enclosure_snapshot_cache(reason="test.rename", enclosure_id=SHELF)
            requests = []
            for index in range(24):
                kind = index % 4
                if kind == 0:
                    requests.append(call_app(self.application, "GET", "/", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"))
                elif kind == 1:
                    requests.append(call_app(self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"))
                elif kind == 2:
                    requests.append(call_app(
                        self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
                    ))
                else:
                    slot = index // 4
                    revision = self.service.mapping_store.save_revision(SYSTEM_ID, SHELF, slot)
                    requests.append(call_app(
                        self.application,
                        "POST",
                        f"/api/slots/{slot}/mapping",
                        query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
                        body={"expected_revision": revision, "serial": f"SANITIZED-SN-{slot:04d}"},
                    ))
            loop = asyncio.get_running_loop()
            started = loop.time()
            responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=ANSWER_CEILING_SECONDS)
            elapsed = loop.time() - started

        self.assertLess(elapsed, ANSWER_CEILING_SECONDS)
        for status, headers, content in responses:
            # Nothing cached can stand in, so each answers a clear, retryable busy.
            self.assertEqual(status, 503, content[:300])
            self.assertIn("retry-after", headers)
            self.assertIn("still refreshing", json.loads(content)["detail"])
        # A save whose own pre-write read cannot finish is refused before
        # writing, so it leaves no assignment behind.
        for slot in range(6):
            self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, slot))
        # Only the hung call reached the appliance; nothing was added behind it.
        self.assertEqual(self.appliance.reads, 2)
        self.assert_budget_respected()

    async def test_bay_assignment_save_answers_within_the_bound(self) -> None:
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            # As after a locate press: the page's sources are being re-read and
            # that call hangs, while the shelf's snapshot is still current.
            await self.start_hung_source_refresh()
            revision = self.service.mapping_store.save_revision(SYSTEM_ID, SHELF, 0)
            status, headers, content = await asyncio.wait_for(call_app(
                self.application,
                "POST",
                "/api/slots/0/mapping",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
                body={
                    "expected_revision": revision,
                    "serial": "SANITIZED-SN-0100",
                    "notes": "synthetic",
                    "clear_identify_after_save": False,
                },
            ), timeout=ANSWER_CEILING_SECONDS)

            # The assignment is stored, so the save reports success and says the
            # bay view shown is from before the save rather than answering busy.
            self.assertEqual(status, 200, content[:400])
            self.assertNotIn("retry-after", headers)
            payload = json.loads(content)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mapping"]["serial"], "SANITIZED-SN-0100")
            self.assertIn("Saved the bay assignment.", payload["warning"])
            self.assertIn("refresh has not finished", payload["warning"])
            self.assertIn(payload["warning"], payload["snapshot"]["warnings"])
            self.assertEqual(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0).serial, "SANITIZED-SN-0100")
            # The readback added no appliance call behind the hung one.
            self.assertEqual(self.appliance.reads, 2)
            self.assert_budget_respected()

        # Once the appliance answers, a fresh read shows the stored assignment.
        await self.settle()
        snapshot = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        bay = next(slot for slot in snapshot.slots if slot.slot == 0)
        self.assertEqual(bay.serial, "SANITIZED-SN-0100")
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()

    async def test_bay_assignment_clear_answers_within_the_bound(self) -> None:
        revision = self.service.mapping_store.save_revision(SYSTEM_ID, SHELF, 0)
        status, _headers, content = await call_app(
            self.application,
            "POST",
            "/api/slots/0/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={"expected_revision": revision, "serial": "SANITIZED-SN-0101"},
        )
        self.assertEqual(status, 200, content[:400])
        reads = self.appliance.reads
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_source_refresh()
            clear_revision = self.service.mapping_store.clear_revision(SYSTEM_ID, SHELF, 0)
            status, headers, content = await asyncio.wait_for(call_app(
                self.application,
                "DELETE",
                "/api/slots/0/mapping",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&expected_revision={clear_revision}",
            ), timeout=ANSWER_CEILING_SECONDS)

        self.assertEqual(status, 200, content[:400])
        self.assertNotIn("retry-after", headers)
        payload = json.loads(content)
        self.assertTrue(payload["ok"])
        self.assertIn("Cleared the bay assignment.", payload["warning"])
        self.assertIn(payload["warning"], payload["snapshot"]["warnings"])
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))
        self.assertEqual(self.appliance.reads, reads + 1)
        self.assert_budget_respected()

    async def test_stale_read_names_the_unfinished_refresh(self) -> None:
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            # Past the user wait bound the cached view is still served, and says so.
            await asyncio.sleep(USER_WAIT_SECONDS * 1.5)
            status, _headers, content = await asyncio.wait_for(
                call_app(self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"),
                timeout=ANSWER_CEILING_SECONDS,
            )
        self.assertEqual(status, 200, content[:300])
        warnings = json.loads(content)["warnings"]
        self.assertTrue(any("inventory refresh" in warning and "read at" in warning for warning in warnings), warnings)
        self.assertEqual(self.appliance.reads, 2)


class AbandonedRefreshTests(RefreshDeadlineTests):
    limit = 2

    async def test_refresh_past_its_deadline_is_abandoned_reported_and_the_next_one_runs(self) -> None:
        with self.assertLogs(inventory_module.logger, "WARNING") as logs:
            await self.start_hung_page_refresh()
            self.assertIn(SHELF, self.service._snapshot_refresh_tasks)
            # The refresh is abandoned at its deadline although the call never
            # answers, so neither it nor its source read blocks the next one.
            await wait_for_condition(
                lambda: SHELF not in self.service._snapshot_refresh_tasks
                and self.service._source_bundle_refresh_task is None,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
        self.assertTrue(any("did not finish" in line for line in logs.output), logs.output)
        self.assertFalse(self.appliance.answer.is_set())
        # The unanswered call still holds its permit.
        self.assertEqual(self.appliance.limiter.active, 1)

        # The next stale read is told why the view is old, and starts a new
        # refresh that completes on the remaining permit.
        status, _headers, content = await asyncio.wait_for(
            call_app(self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"),
            timeout=ANSWER_CEILING_SECONDS,
        )
        self.assertEqual(status, 200, content[:300])
        warnings = json.loads(content)["warnings"]
        self.assertTrue(any("did not finish" in warning for warning in warnings), warnings)
        next_refresh = self.service._snapshot_refresh_tasks.get(SHELF)
        self.assertIsNotNone(next_refresh, "the next refresh did not start")
        await asyncio.wait_for(asyncio.shield(next_refresh), timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(self.appliance.reads, 3)
        refreshed = self.service._cache[SHELF]
        self.assertNotIn(LATE_MARKER, refreshed.warnings)

        # Once a refresh has published again, reads no longer carry the warning.
        status, _headers, content = await call_app(
            self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
        )
        self.assertFalse(any("did not finish" in warning for warning in json.loads(content)["warnings"]))

        # The abandoned call's late answer is discarded.
        await self.settle()
        self.assertTrue(self.service._cache[SHELF] is refreshed, "the refreshed snapshot was replaced")
        self.assertNotIn(LATE_MARKER, self.service._source_bundle.warnings)
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assertLessEqual(self.appliance.limiter.peak_active, 2)

    async def test_late_finishing_abandoned_refresh_does_not_overwrite_a_newer_snapshot(self) -> None:
        self.appliance.ignore_cancellation = True
        # The page's own refresh starts with sources that are still fresh, so a
        # later reader can rebuild without waiting on the hung call.
        self.expire_snapshot()
        self.appliance.hang_next = True
        self.service._schedule_background_snapshot_refresh(SHELF)
        await asyncio.wait_for(self.appliance.hung.wait(), timeout=2)
        await asyncio.sleep(REFRESH_DEADLINE_SECONDS * 1.5)

        # A reader after the deadline publishes a newer snapshot from the cached sources.
        newer = await asyncio.wait_for(
            self.service.get_snapshot(selected_enclosure_id=SHELF), timeout=ANSWER_CEILING_SECONDS,
        )
        self.assertTrue(self.service._cache[SHELF] is newer, "the newer snapshot was replaced")
        sources_before = self.service._source_bundle

        # The abandoned call answers late; its result must not be published.
        self.appliance.answer.set()
        await asyncio.wait_for(self.appliance.late_finished.wait(), timeout=2)
        await self.settle()
        for _ in range(20):
            await asyncio.sleep(0)

        self.assertTrue(self.service._cache[SHELF] is newer, "the newer snapshot was replaced")
        self.assertNotIn(LATE_MARKER, self.service._cache[SHELF].warnings)
        self.assertTrue(self.service._source_bundle is sources_before, "the late sources were published")
        self.assertEqual(self.appliance.reads, 2)
        self.assert_budget_respected()
        self.assertEqual(self.appliance.limiter.active, 0)

    async def test_rebuild_that_finishes_after_its_deadline_is_not_published(self) -> None:
        # The refresh is rebuilding the shelf (it holds the shelf's state) in
        # work that cannot stop once started, and it outlives its deadline.
        release = asyncio.Event()
        # Release it even if an assertion fails, or the loop could not close.
        self.addCleanup(release.set)
        building = asyncio.Event()
        build_snapshot = self.service._build_snapshot

        async def build_that_cannot_stop(*args, **kwargs):
            if inventory_module._snapshot_background_refresh.get() and not building.is_set():
                building.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
            return await build_snapshot(*args, **kwargs)

        self.service._build_snapshot = build_that_cannot_stop
        published_before = self.service._snapshot_published_sequence[SHELF]
        cached_before = self.service._cache[SHELF]
        with self.assertLogs(inventory_module.logger, "WARNING"):
            self.service._schedule_background_snapshot_refresh(SHELF)
            await asyncio.wait_for(building.wait(), timeout=2)
            await wait_for_condition(
                lambda: SHELF not in self.service._snapshot_refresh_tasks,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )

        # A reader that needs a current view is answered within the bound.
        self.expire_snapshot()
        with self.assertRaises(inventory_module.SnapshotRefreshWaitTimeout):
            await asyncio.wait_for(
                self.service.get_snapshot(selected_enclosure_id=SHELF), timeout=ANSWER_CEILING_SECONDS,
            )

        # The rebuild finishes late; it learns and publishes nothing.
        release.set()
        await wait_for_condition(lambda: not self.service._snapshot_activity.get(SHELF), timeout=ANSWER_CEILING_SECONDS)
        self.assertTrue(self.service._cache.get(SHELF) is cached_before, "the late rebuild was published")
        self.assertEqual(self.service._snapshot_published_sequence[SHELF], published_before)
        # The refresh's own source read only.
        self.assertEqual(self.appliance.reads, 2)
        self.assert_budget_respected()

    async def test_abandoned_refresh_cannot_commit_late_slot_details_from_a_worker_thread(self) -> None:
        # The refresh reads a new disk, and its slot-detail save runs in a
        # real worker thread that is held there until after the deadline.
        store = self.service.slot_detail_store
        rows_before = store.load_all()
        cached_before = self.service._cache[SHELF]
        plain_bundle = self.appliance.bundle

        def bundle_with_a_new_disk(*, late: bool = False) -> inventory_module.InventorySourceBundle:
            bundle = plain_bundle(late=late)
            bundle.raw_data.disks = [
                {"name": "da0", "serial": "SANITIZED-LATE-DISK", "model": "Synthetic model", "size": 1000000000},
            ]
            return bundle

        self.appliance.bundle = bundle_with_a_new_disk
        save_entries = store.save_entries
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        guard_results: list[bool] = []

        def held_save(entries, *, expected_entries=None, commit_guard=None):
            entered.set()
            if not release.wait(8):
                raise AssertionError("the held save was never released")
            assert commit_guard is not None
            with commit_guard() as valid:
                guard_results.append(valid)
            return save_entries(entries, expected_entries=expected_entries, commit_guard=commit_guard)

        store.save_entries = held_save
        self.addCleanup(setattr, store, "save_entries", save_entries)
        with self.assertLogs(inventory_module.logger, "WARNING"):
            self.service._schedule_background_snapshot_refresh(SHELF)
            refresh = self.service._snapshot_refresh_tasks[SHELF]
            await wait_for_condition(entered.is_set, timeout=ANSWER_CEILING_SECONDS)
            # The deadline passes while the thread is still saving.
            await wait_for_condition(
                lambda: SHELF not in self.service._snapshot_refresh_tasks,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
        self.assertFalse(refresh.done(), "the save thread should still be held")

        release.set()
        await asyncio.wait_for(asyncio.gather(refresh, return_exceptions=True), timeout=ANSWER_CEILING_SECONDS)
        # The commit itself refuses, in the worker thread, after the deadline.
        self.assertEqual(guard_results, [False])
        self.assertEqual(store.load_all(), rows_before, "an abandoned refresh saved slot details late")
        self.assertTrue(self.service._cache[SHELF] is cached_before, "the abandoned refresh was published")
        self.assertFalse(self.service._background_refresh_workers)
        self.assertFalse(self.service._snapshot_activity)


class StartupPrewarmTests(RefreshDeadlineTests):
    """Opt-in startup prewarming is background work under the same rules."""

    def cold_service(self) -> inventory_module.InventoryService:
        # As just after startup: nothing has been read or cached yet.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = build_service(directory.name, self.appliance)
        self.registry.get_service.return_value = self.service
        return self.service

    async def test_cold_requests_during_a_hung_prewarm_answer_within_the_wait_bound(self) -> None:
        service = self.cold_service()
        self.appliance.hang_next = True
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            warm = asyncio.create_task(service.prewarm_cache())
            await asyncio.wait_for(self.appliance.hung.wait(), timeout=2)
            requests = [
                call_app(
                    self.application, "GET", "/" if index % 2 else "/api/inventory",
                    query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
                )
                for index in range(24)
            ]
            responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=ANSWER_CEILING_SECONDS)

        for status, headers, content in responses:
            # No snapshot exists yet, so each says so and asks for a retry.
            self.assertEqual(status, 503, content[:300])
            self.assertEqual(headers.get("retry-after"), "2")
            self.assertIn("first inventory read is still running", json.loads(content)["detail"])
        # Only the prewarm's call reached the appliance.
        self.assertEqual(self.appliance.reads, 2)
        self.assert_budget_respected()

        # Once the appliance answers, the prewarm finishes and publishes.
        self.appliance.answer.set()
        await asyncio.wait_for(warm, timeout=ANSWER_CEILING_SECONDS)
        self.assertIn(SHELF, service._cache)
        self.assertFalse(service._background_refresh_workers)
        self.assertEqual(self.appliance.limiter.active, 0)

    async def test_prewarm_publishes_then_warms_smart_outside_its_deadline(self) -> None:
        service = self.cold_service()
        smart = Mock()

        async def warm_smart(slots, *, selected_enclosure_id=None):
            # SMART warming is ordinary work, not part of the supervised prewarm.
            smart(sorted(slots), selected_enclosure_id, bool(service._background_refresh_workers))
            return []

        with patch.object(service, "get_slot_smart_summaries", new=warm_smart):
            await asyncio.wait_for(service.prewarm_cache(warm_smart=True), timeout=ANSWER_CEILING_SECONDS)
        self.assertIn(SHELF, service._cache)
        self.assertTrue(smart.called)
        warmed_enclosures = {call.args[1] for call in smart.call_args_list}
        self.assertIn(SHELF, warmed_enclosures)
        for call in smart.call_args_list:
            self.assertTrue(call.args[0], "a warmed enclosure had no bays")
            self.assertFalse(call.args[2], "SMART warming ran inside the supervised prewarm")

    async def test_prewarm_past_its_deadline_is_abandoned_and_never_publishes(self) -> None:
        service = self.cold_service()
        # The prewarm's read cannot stop once started and finishes late.
        self.appliance.ignore_cancellation = True
        self.appliance.hang_next = True
        with self.assertLogs(inventory_module.logger, "WARNING") as logs:
            warm = asyncio.create_task(service.prewarm_cache())
            await asyncio.wait_for(self.appliance.hung.wait(), timeout=2)
            await wait_for_condition(
                lambda: service._background_refresh_abandoned_at is not None,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
        self.assertTrue(any("did not finish" in line for line in logs.output), logs.output)
        self.assertFalse(warm.done(), "the prewarm's read should still be running")

        # A request while the abandoned prewarm still runs is still bounded.
        status, headers, content = await asyncio.wait_for(
            call_app(self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}"),
            timeout=ANSWER_CEILING_SECONDS,
        )
        self.assertEqual(status, 503, content[:300])
        self.assertIn("retry-after", headers)

        # The read answers late. The prewarm ends quietly and publishes nothing.
        self.appliance.answer.set()
        self.assertIsNone(await asyncio.wait_for(warm, timeout=ANSWER_CEILING_SECONDS))
        self.assertFalse(service._cache)
        self.assertIsNone(service._source_bundle)
        self.assertFalse(service._background_refresh_workers)
        self.assertEqual(self.appliance.limiter.active, 0)

        # The next read is the first to publish.
        snapshot = await asyncio.wait_for(
            service.get_snapshot(selected_enclosure_id=SHELF), timeout=ANSWER_CEILING_SECONDS,
        )
        self.assertNotIn(LATE_MARKER, snapshot.warnings)
        self.assertEqual(self.appliance.reads, 3)
        self.assert_budget_respected()


if __name__ == "__main__":
    unittest.main()
