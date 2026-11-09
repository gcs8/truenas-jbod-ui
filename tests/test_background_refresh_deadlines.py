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
from app.models.domain import ManualMapping, SourceStatus
from app.services import inventory as inventory_module
from app.services.parsers import ParsedSSHData
from app.services.truenas_ws import MiddlewareCallLimiter, TrueNASRawData
from tests.test_background_refresh_busy_routes import (
    SHELF,
    SSH_OUTPUTS,
    SYSTEM_ID,
    build_service,
    call_app,
    editor_revision,
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
                    revision = editor_revision(self.service, slot)
                    requests.append(call_app(
                        self.application,
                        "POST",
                        f"/api/slots/{slot}/mapping",
                        query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
                        body={"expected_revision": revision, "serial": f"SANITIZED-SN-{slot:04d}"},
                    ))
            # The user's Refresh presses (forced reads) join the running
            # refresh instead of answering busy (#1139); the rest stay bounded.
            refreshes = [asyncio.ensure_future(request) for request in requests[2::4]]
            bounded = [request for index, request in enumerate(requests) if index % 4 != 2]
            loop = asyncio.get_running_loop()
            started = loop.time()
            responses = await asyncio.wait_for(asyncio.gather(*bounded), timeout=ANSWER_CEILING_SECONDS)
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
            # Only the hung call reached the appliance; nothing was added behind
            # it, and no joined Refresh started a call of its own meanwhile.
            self.assertEqual(self.appliance.reads, 2)
            self.assertEqual(len(self.appliance.calls), 2)
            self.assertFalse(any(refresh.done() for refresh in refreshes), "a Refresh did not join the running refresh")

            # Once the appliance answers, every joined Refresh gets a fresh view.
            self.appliance.answer.set()
            refreshed = await asyncio.wait_for(asyncio.gather(*refreshes), timeout=ANSWER_CEILING_SECONDS)
        for status, _headers, content in refreshed:
            self.assertEqual(status, 200, content[:300])
        # Concurrent forced reads still take turns, at most one read each, as
        # before #1126; joining adds none and the budget holds.
        self.assertLessEqual(self.appliance.reads, 2 + len(refreshes))
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()

    async def test_bay_assignment_save_answers_within_the_bound(self) -> None:
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            # As after a locate press: the page's sources are being re-read and
            # that call hangs, while the shelf's snapshot is still current.
            await self.start_hung_source_refresh()
            revision = editor_revision(self.service, 0)
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
        revision = editor_revision(self.service, 0)
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
            clear_revision = editor_revision(self.service, 0, clear=True)
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


class UserRefreshDuringARunningRefreshTests(RefreshDeadlineTests):
    """A Refresh pressed while the page's own refresh runs joins it (#1139).

    The running refresh is longer than the user wait bound. Before #1139 the
    forced read answered 503 "still refreshing" at that bound and the page
    showed "Refresh failed".
    """

    async def assert_refresh_joins_the_running_refresh(self, path: str) -> None:
        published_before = self.service._snapshot_published_sequence[SHELF]
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            pressed_at = datetime.now(timezone.utc)
            refresh = asyncio.ensure_future(call_app(
                self.application, "GET", path, query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
            ))
            # Well past the user wait bound, the Refresh is still waiting for
            # the running refresh rather than answering busy.
            await asyncio.sleep(USER_WAIT_SECONDS * 3)
            self.assertFalse(refresh.done(), refresh.result()[2][:300] if refresh.done() else "")
            # It started no appliance call of its own while that one runs.
            self.assertEqual(len(self.appliance.calls), 2)

            # A page read meanwhile still answers at once from the cached view.
            # (A bay-assignment save meanwhile joins as well, #1141; see
            # MappingWriteDuringARunningRefreshTests.)
            status, _headers, content = await asyncio.wait_for(call_app(
                self.application, "GET", "/api/inventory", query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            ), timeout=ANSWER_CEILING_SECONDS)
            self.assertEqual(status, 200, content[:300])
            self.assertFalse(refresh.done(), refresh.result()[2][:300] if refresh.done() else "")

            # The running refresh finishes; the Refresh answers with a view read
            # after it was pressed, and no error.
            self.appliance.answer.set()
            status, headers, content = await asyncio.wait_for(refresh, timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(status, 200, content[:300])
        self.assertNotIn("retry-after", headers)
        self.assertNotEqual(json.loads(content).get("ok"), False, content[:300])
        current = self.service._cache[SHELF]
        self.assertGreater(self.service._snapshot_published_sequence[SHELF], published_before)
        self.assertGreaterEqual(current.last_updated.replace(tzinfo=current.last_updated.tzinfo or timezone.utc), pressed_at)
        self.assertNotIn(LATE_MARKER, current.warnings)
        # The running refresh's read, then one read for this Refresh: no
        # concurrent second read, and the call budget held throughout.
        await self.settle()
        self.assertEqual(self.appliance.reads, 3)
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()

    async def test_inventory_refresh_joins_a_refresh_longer_than_the_wait_bound(self) -> None:
        await self.assert_refresh_joins_the_running_refresh("/api/inventory")

    async def test_sas_fabric_refresh_joins_a_refresh_longer_than_the_wait_bound(self) -> None:
        await self.assert_refresh_joins_the_running_refresh("/api/sas-fabric")

    async def test_storage_view_refresh_joins_a_refresh_longer_than_the_wait_bound(self) -> None:
        await self.assert_refresh_joins_the_running_refresh("/api/storage-views")

    async def test_refresh_still_fails_when_the_running_refresh_misses_its_deadline(self) -> None:
        # The running refresh's read cannot stop and outlives the deadline.
        self.appliance.ignore_cancellation = True
        loop = asyncio.get_running_loop()
        with self.assertLogs(inventory_module.logger, "WARNING") as logs:
            await self.start_hung_page_refresh()
            started = loop.time()
            status, headers, content = await asyncio.wait_for(call_app(
                self.application, "GET", "/api/inventory",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
            ), timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS)
            elapsed = loop.time() - started
        self.assertTrue(any("did not finish" in line for line in logs.output), logs.output)
        # A real failure is still reported, retryably, once the deadline passes.
        self.assertEqual(status, 503, content[:300])
        self.assertIn("retry-after", headers)
        self.assertLess(elapsed, REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS)
        self.assertEqual(len(self.appliance.calls), 2)
        self.assert_budget_respected()

    async def test_refresh_does_not_wait_on_a_refresh_already_past_its_deadline(self) -> None:
        # Joining is bounded by the running refresh's own deadline. One that
        # was abandoned but cannot stop yet is not waited on past the short
        # bound, even if the configured deadline is now much longer.
        self.appliance.ignore_cancellation = True
        with self.assertLogs(inventory_module.logger, "WARNING"):
            await self.start_hung_page_refresh()
            await wait_for_condition(
                lambda: self.service._background_refresh_abandoned_at is not None,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
        self.assertTrue(self.service._background_refresh_workers, "the abandoned read should still be running")
        loop = asyncio.get_running_loop()
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            started = loop.time()
            status, headers, _content = await asyncio.wait_for(call_app(
                self.application, "GET", "/api/inventory",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
            ), timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)
        self.assertLess(loop.time() - started, ANSWER_CEILING_SECONDS)
        self.assertEqual(len(self.appliance.calls), 2)

    async def test_refresh_joins_for_at_most_one_refresh_deadline_in_total(self) -> None:
        # However late the running refresh's own deadline, a Refresh waits at
        # most BACKGROUND_REFRESH_DEADLINE_SECONDS from its first wait, so
        # successive refreshes cannot keep it waiting.
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, headers, _content = await asyncio.wait_for(call_app(
            self.application, "GET", "/api/inventory",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
        ), timeout=ANSWER_CEILING_SECONDS)
        elapsed = loop.time() - started
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)
        # Joined past the short bound, then stopped at the total cap.
        self.assertGreaterEqual(elapsed, USER_WAIT_SECONDS)
        self.assertLess(elapsed, ANSWER_CEILING_SECONDS)
        self.assertEqual(len(self.appliance.calls), 2)


class MappingWriteDuringARunningRefreshTests(RefreshDeadlineTests):
    """A bay-assignment save or clear during a slow page refresh joins it (#1141).

    The shelf's cached view is present but expired, so the write's strict
    pre-write read waits for the running refresh. Since #1126 it answered 503
    "still refreshing" at the short user bound, before writing; before #1126 it
    waited and the write succeeded. It now joins the running refresh like a
    user Refresh (#1139), within that refresh's deadline and the same total cap,
    then validates against the view the refresh published.
    """

    def save(self, slot: int, serial: str, revision: str | None = None):
        if revision is None:
            revision = editor_revision(self.service, slot)
        return asyncio.ensure_future(call_app(
            self.application,
            "POST",
            f"/api/slots/{slot}/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={"expected_revision": revision, "serial": serial, "clear_identify_after_save": False},
        ))

    def stored_serial(self, slot: int) -> str | None:
        mapping = self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, slot)
        return None if mapping is None else mapping.serial

    async def test_save_with_an_expired_view_joins_a_slow_refresh_and_is_stored(self) -> None:
        published_before = self.service._snapshot_published_sequence[SHELF]
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            self.assertIn(SHELF, self.service._cache, "the expired view should still be cached")
            save = self.save(0, "SANITIZED-SN-0300")
            # Well past the user wait bound the save is waiting for the running
            # refresh, has written nothing and started no appliance call.
            await asyncio.sleep(USER_WAIT_SECONDS * 3)
            self.assertFalse(save.done(), save.result()[2][:300] if save.done() else "")
            self.assertIsNone(self.stored_serial(0))
            self.assertEqual(len(self.appliance.calls), 2)

            self.appliance.answer.set()
            status, headers, content = await asyncio.wait_for(save, timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(status, 200, content[:400])
        self.assertNotIn("retry-after", headers)
        payload = json.loads(content)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mapping"]["serial"], "SANITIZED-SN-0300")
        self.assertEqual(self.stored_serial(0), "SANITIZED-SN-0300")
        # The save validated against the view the running refresh published,
        # and its readback shows the stored assignment.
        self.assertGreater(self.service._snapshot_published_sequence[SHELF], published_before)
        bay = next(slot for slot in payload["snapshot"]["slots"] if slot["slot"] == 0)
        self.assertEqual(bay["serial"], "SANITIZED-SN-0300")
        # Setup, the running refresh, then the save's readback: the pre-write
        # read reused the joined refresh and added no read of its own.
        await self.settle()
        self.assertEqual(self.appliance.reads, 3)
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()

    async def test_clear_with_an_expired_view_joins_a_slow_refresh(self) -> None:
        status, _headers, content = await self.save(0, "SANITIZED-SN-0301")
        self.assertEqual(status, 200, content[:400])
        reads = self.appliance.reads
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            revision = editor_revision(self.service, 0, clear=True)
            clear = asyncio.ensure_future(call_app(
                self.application,
                "DELETE",
                "/api/slots/0/mapping",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&expected_revision={revision}",
            ))
            await asyncio.sleep(USER_WAIT_SECONDS * 3)
            self.assertFalse(clear.done(), clear.result()[2][:300] if clear.done() else "")
            self.assertEqual(self.stored_serial(0), "SANITIZED-SN-0301")
            self.assertEqual(len(self.appliance.calls), reads + 1)

            self.appliance.answer.set()
            status, headers, content = await asyncio.wait_for(clear, timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(status, 200, content[:400])
        self.assertNotIn("retry-after", headers)
        self.assertTrue(json.loads(content)["ok"])
        self.assertIsNone(self.stored_serial(0))
        await self.settle()
        # The running refresh, then the clear's readback.
        self.assertEqual(self.appliance.reads, reads + 2)
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()

    async def test_save_still_fails_retryably_when_the_running_refresh_misses_its_deadline(self) -> None:
        # The running refresh's read cannot stop and outlives its deadline.
        self.appliance.ignore_cancellation = True
        loop = asyncio.get_running_loop()
        with self.assertLogs(inventory_module.logger, "WARNING") as logs:
            await self.start_hung_page_refresh()
            started = loop.time()
            status, headers, content = await asyncio.wait_for(
                self.save(0, "SANITIZED-SN-0302"), timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
            elapsed = loop.time() - started
        self.assertTrue(any("did not finish" in line for line in logs.output), logs.output)
        self.assertEqual(status, 503, content[:300])
        self.assertIn("retry-after", headers)
        self.assertIn("still refreshing", json.loads(content)["detail"])
        self.assertLess(elapsed, REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS)
        # Refused before writing, and no call of its own.
        self.assertIsNone(self.stored_serial(0))
        self.assertEqual(len(self.appliance.calls), 2)
        self.assert_budget_respected()

    async def abandon_the_running_refresh(self) -> None:
        # The running refresh's read cannot stop; it is abandoned at its
        # deadline and is still unwinding afterwards.
        self.appliance.ignore_cancellation = True
        with self.assertLogs(inventory_module.logger, "WARNING"):
            await self.start_hung_page_refresh()
            await wait_for_condition(
                lambda: self.service._background_refresh_abandoned_at is not None,
                timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
        self.assertTrue(self.service._background_refresh_workers, "the abandoned read should still be running")

    async def test_pre_write_read_does_not_wait_on_a_refresh_already_past_its_deadline(self) -> None:
        # Called on the service so that only the pre-write read runs: it does
        # not wait past the short bound on an abandoned refresh, even with a
        # much longer configured deadline, and nothing is written.
        await self.abandon_the_running_refresh()
        loop = asyncio.get_running_loop()
        revision = editor_revision(self.service, 0)
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            started = loop.time()
            with self.assertRaises(inventory_module.SnapshotRefreshWaitTimeout):
                await asyncio.wait_for(self.service.save_mapping(
                    0,
                    {"serial": "SANITIZED-SN-0303"},
                    selected_enclosure_id=SHELF,
                    expected_revision=revision,
                    invalidate_snapshot=False,
                ), timeout=ANSWER_CEILING_SECONDS)
            with self.assertRaises(inventory_module.SnapshotRefreshWaitTimeout):
                await asyncio.wait_for(self.service.clear_mapping(
                    0,
                    selected_enclosure_id=SHELF,
                    expected_revision=editor_revision(self.service, 0, clear=True),
                    invalidate_snapshot=False,
                ), timeout=ANSWER_CEILING_SECONDS)
        self.assertLess(loop.time() - started, ANSWER_CEILING_SECONDS)
        self.assertIsNone(self.stored_serial(0))
        self.assertEqual(len(self.appliance.calls), 2)

    async def test_save_after_an_abandoned_refresh_waits_at_most_for_the_next_refresh_deadline(self) -> None:
        # Through the route, the bay check reads the expired view and starts
        # the page's next refresh. That refresh is not abandoned, so the save
        # joins it, but only until its own deadline: its appliance call
        # cannot get a permit while the abandoned call holds the only one.
        await self.abandon_the_running_refresh()
        loop = asyncio.get_running_loop()
        with self.assertLogs(inventory_module.logger, "WARNING"):
            started = loop.time()
            status, headers, content = await asyncio.wait_for(
                self.save(0, "SANITIZED-SN-0307"), timeout=REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS,
            )
            elapsed = loop.time() - started
        self.assertEqual(status, 503, content[:300])
        self.assertIn("retry-after", headers)
        self.assertLess(elapsed, REFRESH_DEADLINE_SECONDS + ANSWER_CEILING_SECONDS)
        self.assertIsNone(self.stored_serial(0))
        # Only the abandoned call holds a permit; nothing overlapped it.
        self.assertEqual(self.appliance.limiter.active, 1)
        self.assert_budget_respected()

    async def test_save_joins_for_at_most_one_refresh_deadline_in_total(self) -> None:
        # The running refresh's own deadline is far away; the save still stops
        # at BACKGROUND_REFRESH_DEADLINE_SECONDS from its first wait.
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, headers, _content = await asyncio.wait_for(
            self.save(0, "SANITIZED-SN-0304"), timeout=ANSWER_CEILING_SECONDS,
        )
        elapsed = loop.time() - started
        self.assertEqual(status, 503)
        self.assertIn("retry-after", headers)
        self.assertGreaterEqual(elapsed, USER_WAIT_SECONDS)
        self.assertLess(elapsed, ANSWER_CEILING_SECONDS)
        self.assertIsNone(self.stored_serial(0))
        self.assertEqual(len(self.appliance.calls), 2)

    async def test_save_from_a_superseded_view_is_still_refused_after_joining(self) -> None:
        # The editor read the bay at revision r0; while its save waits on the
        # running refresh, another tab stores a different assignment.
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            save = self.save(0, "SANITIZED-SN-0305", revision=editor_revision(self.service, 0))
            await asyncio.sleep(USER_WAIT_SECONDS * 2)
            self.assertFalse(save.done())
            self.service.mapping_store.save_mapping(
                ManualMapping(system_id=SYSTEM_ID, slot=0, enclosure_id=SHELF, serial="SANITIZED-SN-0306"),
                expected_revision=self.service.mapping_store.save_revision(SYSTEM_ID, SHELF, 0),
            )
            self.appliance.answer.set()
            status, _headers, content = await asyncio.wait_for(save, timeout=ANSWER_CEILING_SECONDS)
        # Joining does not weaken the write's own check (#1121): the editor's
        # superseded view is refused and the other tab's assignment stays.
        self.assertEqual(status, 409, content[:300])
        # The disk did not change, so the refusal names the assignment change.
        self.assertEqual(json.loads(content)["error"], "mapping_revision_conflict")
        self.assertEqual(self.stored_serial(0), "SANITIZED-SN-0306")
        await self.settle()
        self.assertEqual(self.appliance.reads, 2)
        self.assert_budget_respected()

    async def test_concurrent_saves_and_refreshes_join_without_adding_appliance_calls(self) -> None:
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            # Four tabs save different bays from the same view of the shelf,
            # while two more press Refresh.
            revision = editor_revision(self.service, 0)
            saves = [self.save(slot, f"SANITIZED-SN-{310 + slot:04d}", revision=revision) for slot in range(4)]
            refreshes = [
                asyncio.ensure_future(call_app(
                    self.application, "GET", "/api/inventory",
                    query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&force=true",
                ))
                for _ in range(2)
            ]
            await asyncio.sleep(USER_WAIT_SECONDS * 3)
            self.assertFalse(any(request.done() for request in saves + refreshes), "a request did not join")
            # Joining adds no appliance call while the running one is held.
            self.assertEqual(len(self.appliance.calls), 2)
            self.assertEqual(self.appliance.limiter.active, 1)
            for slot in range(4):
                self.assertIsNone(self.stored_serial(slot))

            self.appliance.answer.set()
            saved = await asyncio.wait_for(asyncio.gather(*saves), timeout=ANSWER_CEILING_SECONDS * 2)
            refreshed = await asyncio.wait_for(asyncio.gather(*refreshes), timeout=ANSWER_CEILING_SECONDS * 2)
        for status, _headers, content in refreshed:
            self.assertEqual(status, 200, content[:300])
        # The saves share one scope revision, so exactly one is stored. The
        # others were based on a view the first one superseded and are
        # refused before writing, as without a running refresh (#1121).
        stored = [slot for slot in range(4) if self.stored_serial(slot) is not None]
        self.assertEqual(len(stored), 1, [response[:1] for response in saved])
        for slot, (status, headers, content) in enumerate(saved):
            if slot in stored:
                self.assertEqual(status, 200, content[:300])
                self.assertEqual(self.stored_serial(slot), f"SANITIZED-SN-{310 + slot:04d}")
            else:
                self.assertIn(status, (409, 503), content[:300])
                if status == 503:
                    self.assertIn("retry-after", headers)
        await self.settle()
        # Afterwards each request makes at most one read of its own (two for a
        # stored save: its pre-write read and its readback), one at a time.
        self.assertLessEqual(self.appliance.reads, 2 + len(refreshes) + len(saves) + 1)
        self.assertEqual(self.appliance.limiter.active, 0)
        self.assert_budget_respected()


class BayDiskSwapTests(RefreshDeadlineTests):
    """Bay 0 holds disk A until a test puts disk B at the same device name."""

    DISK_A = "SANITIZED-SN-0400"
    DISK_B = "SANITIZED-SN-0401"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.disk_serial = self.DISK_A
        plain_bundle = self.appliance.bundle

        def bundle_with_a_disk(*, late: bool = False) -> inventory_module.InventorySourceBundle:
            # Raw appliance rows only: the build itself decides which disk sits
            # in bay 0, as for a real disk swap at the same device name.
            result = plain_bundle(late=late)
            result.raw_data.disks = [
                {"name": "da0", "serial": self.disk_serial, "model": "Synthetic disk", "size": 1_000_000_000},
            ]
            return result

        self.appliance.bundle = bundle_with_a_disk

    async def open_editor(self, *, saved_notes: str | None = None):
        """The page reads the bay with disk A in it, as the editor shows it."""
        if saved_notes is not None:
            self.service.mapping_store.save_mapping(
                ManualMapping(system_id=SYSTEM_ID, enclosure_id=SHELF, slot=0, notes=saved_notes),
            )
        snapshot = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        bay = next(slot for slot in snapshot.slots if slot.slot == 0)
        self.assertEqual(bay.serial, self.DISK_A)
        return bay

    async def write_while_the_running_refresh_reads(self, write, *, release_after: float, serial: str):
        """Send the write while the page's refresh is held, then let it read `serial`."""
        # Each round holds a fresh appliance call.
        self.appliance.answer.clear()
        self.appliance.hung.clear()
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            request = write()
            await asyncio.sleep(release_after)
            if release_after > USER_WAIT_SECONDS:
                self.assertFalse(request.done(), request.result()[2][:300] if request.done() else "")
            self.disk_serial = serial
            self.appliance.answer.set()
            response = await asyncio.wait_for(request, timeout=ANSWER_CEILING_SECONDS)
        await self.settle()
        return response

    def cached_bay(self):
        return next(slot for slot in self.service._cache[SHELF].slots if slot.slot == 0)


class MappingWriteAfterTheBayDiskChangedTests(BayDiskSwapTests):
    """A bay-assignment write is bound to the disk its editor showed (#1141).

    The editor's save and clear revisions carry the identity of the disk that
    was in the bay. When the write's pre-write read, joined to a running
    refresh or not, finds another disk in that bay, the write is refused with a
    409 that says so and nothing is stored, although no other tab changed the
    saved assignments. Before, the old disk's serial and notes were stored onto
    the bay, or its notes cleared, against the new disk.
    """

    SAVE_DETAIL = "The disk in this bay changed. Reload to see it, then save again."
    CLEAR_DETAIL = "The disk in this bay changed. Reload to see it, then clear again."

    def save_from(self, bay):
        return asyncio.ensure_future(call_app(
            self.application,
            "POST",
            "/api/slots/0/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={
                "expected_revision": bay.mapping_revision,
                "serial": bay.serial,
                "device_name": bay.device_name,
                "notes": "edited against disk A",
                "clear_identify_after_save": False,
            },
        ))

    def clear_from(self, bay):
        return asyncio.ensure_future(call_app(
            self.application,
            "DELETE",
            "/api/slots/0/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&expected_revision={bay.mapping_clear_revision}",
        ))

    def assert_disk_change_refused(self, response, detail: str) -> None:
        status, headers, content = response
        self.assertEqual(status, 409, content[:400])
        self.assertNotIn("retry-after", headers)
        payload = json.loads(content)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "mapping_disk_changed")
        self.assertEqual(payload["detail"], detail)
        # The refusing read published disk B; the bay shows it, not A's draft.
        self.assertEqual(self.cached_bay().serial, self.DISK_B)

    async def test_save_is_refused_when_the_joined_refresh_finds_another_disk(self) -> None:
        bay = await self.open_editor()
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.save_from(bay), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_B,
        )
        self.assert_disk_change_refused(response, self.SAVE_DETAIL)
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))
        self.assertIsNone(self.cached_bay().notes)

    async def test_save_is_refused_when_a_short_wait_finds_another_disk(self) -> None:
        # Inherited before #1142: within the short bound the same stale save
        # was stored as well.
        bay = await self.open_editor()
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.save_from(bay), release_after=0.05, serial=self.DISK_B,
        )
        self.assert_disk_change_refused(response, self.SAVE_DETAIL)
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))

    async def test_save_is_refused_when_an_earlier_refresh_already_replaced_the_disk(self) -> None:
        # The editor stayed open on disk A while a refresh published disk B;
        # the save's pre-write read reuses that current view without waiting.
        bay = await self.open_editor()
        self.disk_serial = self.DISK_B
        await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        reads = self.appliance.reads
        response = await asyncio.wait_for(self.save_from(bay), timeout=ANSWER_CEILING_SECONDS)
        self.assert_disk_change_refused(response, self.SAVE_DETAIL)
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))
        self.assertEqual(self.appliance.reads, reads)

    async def test_clear_is_refused_when_the_joined_refresh_finds_another_disk(self) -> None:
        bay = await self.open_editor(saved_notes="SANITIZED notes for disk A")
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.clear_from(bay), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_B,
        )
        self.assert_disk_change_refused(response, self.CLEAR_DETAIL)
        self.assertEqual(
            self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0).notes, "SANITIZED notes for disk A",
        )

    async def test_clear_is_refused_when_a_short_wait_finds_another_disk(self) -> None:
        bay = await self.open_editor(saved_notes="SANITIZED notes for disk A")
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.clear_from(bay), release_after=0.05, serial=self.DISK_B,
        )
        self.assert_disk_change_refused(response, self.CLEAR_DETAIL)
        self.assertEqual(
            self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0).notes, "SANITIZED notes for disk A",
        )

    async def test_save_and_clear_against_the_same_disk_after_a_joined_refresh_are_stored(self) -> None:
        bay = await self.open_editor()
        status, _headers, content = await self.write_while_the_running_refresh_reads(
            lambda: self.save_from(bay), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_A,
        )
        self.assertEqual(status, 200, content[:400])
        stored = self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0)
        self.assertEqual((stored.serial, stored.notes), (self.DISK_A, "edited against disk A"))

        bay = next(slot for slot in json.loads(content)["snapshot"]["slots"] if slot["slot"] == 0)
        self.assertEqual(bay["serial"], self.DISK_A)
        clear_revision = bay["mapping_clear_revision"]
        status, _headers, content = await self.write_while_the_running_refresh_reads(
            lambda: asyncio.ensure_future(call_app(
                self.application,
                "DELETE",
                "/api/slots/0/mapping",
                query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}&expected_revision={clear_revision}",
            )),
            release_after=USER_WAIT_SECONDS * 3,
            serial=self.DISK_A,
        )
        self.assertEqual(status, 200, content[:400])
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))

    async def test_a_disk_change_and_an_assignment_change_differ_in_the_editor_revision(self) -> None:
        # Swapping the disk changes the bay's save and clear revisions although
        # nothing in the assignment store changed.
        bay_a = await self.open_editor()
        self.disk_serial = self.DISK_B
        snapshot = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        bay_b = next(slot for slot in snapshot.slots if slot.slot == 0)
        self.assertEqual(bay_b.serial, self.DISK_B)
        self.assertNotEqual(bay_a.mapping_revision, bay_b.mapping_revision)
        self.assertNotEqual(bay_a.mapping_clear_revision, bay_b.mapping_clear_revision)
        # Another bay with no disk change keeps its revisions.
        other_a = next(slot for slot in self.service._cache[SHELF].slots if slot.slot == 1)
        self.disk_serial = self.DISK_B
        snapshot = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        other_b = next(slot for slot in snapshot.slots if slot.slot == 1)
        self.assertEqual(other_a.mapping_revision, other_b.mapping_revision)


class MappingImportAfterTheBayDiskChangedTests(BayDiskSwapTests):
    """A confirmed restore is bound to the disks its preview saw (#1141).

    The preview records the disk in every bay the restore would write and binds
    them into its import digest. The confirmation makes the same strict
    pre-write read a save makes, joined to a running refresh or not; when any of
    those bays now holds another disk, it is refused with a 409 and nothing is
    written. Before, a preview made while disk A was in the bay restored A's
    assignment onto disk B. A preview made after the swap names the mismatch,
    and confirming it then restores the backup on purpose.
    """

    BAYS_CHANGED_DETAIL = "Bays changed since the preview. Preview the import again."

    def bundle_for(self, slot: int, serial: str, notes: str) -> dict:
        return {
            "schema_version": 1,
            "mappings": [{"system_id": SYSTEM_ID, "enclosure_id": SHELF, "slot": slot, "serial": serial,
                          "device_name": "da0", "notes": notes}],
        }

    async def preview(self, bundle: dict) -> dict:
        status, _headers, content = await asyncio.wait_for(call_app(
            self.application, "POST", "/api/mappings/import/preview",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}", body=bundle,
        ), timeout=ANSWER_CEILING_SECONDS)
        self.assertEqual(status, 200, content[:400])
        return json.loads(content)

    def confirm(self, bundle: dict, preview: dict):
        return asyncio.ensure_future(call_app(
            self.application, "POST", "/api/mappings/import",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={"bundle": bundle, "expected_revision": preview["revision"],
                  "import_digest": preview["import_digest"], "confirmed": True},
        ))

    def assert_bays_changed_refused(self, response) -> None:
        status, headers, content = response
        self.assertEqual(status, 409, content[:400])
        self.assertNotIn("retry-after", headers)
        payload = json.loads(content)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "mapping_import_bays_changed")
        self.assertEqual(payload["detail"], self.BAYS_CHANGED_DETAIL)
        self.assertIsNone(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0))
        self.assertEqual(self.cached_bay().serial, self.DISK_B)

    async def preview_of_disk_a(self) -> tuple[dict, dict]:
        await self.open_editor()
        bundle = self.bundle_for(0, self.DISK_A, "SANITIZED notes for disk A")
        return bundle, await self.preview(bundle)

    async def test_import_is_refused_when_the_joined_refresh_finds_another_disk(self) -> None:
        bundle, preview = await self.preview_of_disk_a()
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.confirm(bundle, preview), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_B,
        )
        self.assert_bays_changed_refused(response)

    async def test_import_is_refused_when_a_short_wait_finds_another_disk(self) -> None:
        bundle, preview = await self.preview_of_disk_a()
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.confirm(bundle, preview), release_after=0.05, serial=self.DISK_B,
        )
        self.assert_bays_changed_refused(response)

    async def published_disk_b_then_confirm(self, bundle: dict, preview: dict, *, release_after: float):
        # A strict read joins the page's refresh, which publishes disk B before
        # the restore is confirmed; nothing in the assignment store changed.
        self.appliance.answer.clear()
        self.appliance.hung.clear()
        with patch.object(inventory_module, "BACKGROUND_REFRESH_DEADLINE_SECONDS", 60.0, create=True):
            await self.start_hung_page_refresh()
            strict = asyncio.ensure_future(self.service._get_pre_write_snapshot(SHELF))
            await asyncio.sleep(release_after)
            self.disk_serial = self.DISK_B
            self.appliance.answer.set()
            await asyncio.wait_for(strict, timeout=ANSWER_CEILING_SECONDS)
        await self.settle()
        self.assertEqual(self.cached_bay().serial, self.DISK_B)
        reads = self.appliance.reads
        response = await asyncio.wait_for(self.confirm(bundle, preview), timeout=ANSWER_CEILING_SECONDS)
        return response, reads

    async def test_import_is_refused_after_a_short_refresh_already_published_another_disk(self) -> None:
        bundle, preview = await self.preview_of_disk_a()
        response, _reads = await self.published_disk_b_then_confirm(bundle, preview, release_after=0.05)
        self.assert_bays_changed_refused(response)

    async def test_import_is_refused_after_a_joined_refresh_already_published_another_disk(self) -> None:
        bundle, preview = await self.preview_of_disk_a()
        response, reads = await self.published_disk_b_then_confirm(
            bundle, preview, release_after=USER_WAIT_SECONDS * 3,
        )
        self.assert_bays_changed_refused(response)
        # The confirmation's pre-write read reused the current view.
        self.assertEqual(self.appliance.reads, reads)

    async def test_preview_after_the_swap_names_the_other_disk_and_confirming_restores_the_backup(self) -> None:
        await self.open_editor()
        self.disk_serial = self.DISK_B
        await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        bundle = self.bundle_for(0, self.DISK_A, "SANITIZED notes for disk A")
        preview = await self.preview(bundle)
        self.assertEqual(preview["disk_warnings"], [{
            "enclosure_id": SHELF, "slot": 0, "field": "serial",
            "backup": self.DISK_A, "current": self.DISK_B,
        }])
        status, _headers, content = await asyncio.wait_for(
            self.confirm(bundle, preview), timeout=ANSWER_CEILING_SECONDS,
        )
        # Restoring historical assignments onto the current disk is the user's choice.
        self.assertEqual(status, 200, content[:400])
        stored = self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0)
        self.assertEqual((stored.serial, stored.notes), (self.DISK_A, "SANITIZED notes for disk A"))

    async def test_import_into_unchanged_bays_after_a_joined_refresh_is_stored(self) -> None:
        bundle, preview = await self.preview_of_disk_a()
        # The backup names the disk that is in the bay: nothing to warn about.
        self.assertEqual(preview.get("disk_warnings"), [])
        status, _headers, content = await self.write_while_the_running_refresh_reads(
            lambda: self.confirm(bundle, preview), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_A,
        )
        self.assertEqual(status, 200, content[:400])
        stored = self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0)
        self.assertEqual((stored.serial, stored.notes), (self.DISK_A, "SANITIZED notes for disk A"))

    async def test_a_disk_change_in_a_bay_the_import_does_not_write_does_not_refuse_it(self) -> None:
        await self.open_editor()
        bundle = self.bundle_for(1, "SANITIZED-SN-0499", "SANITIZED notes for bay 1")
        preview = await self.preview(bundle)
        status, _headers, content = await self.write_while_the_running_refresh_reads(
            lambda: self.confirm(bundle, preview), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_B,
        )
        self.assertEqual(status, 200, content[:400])
        self.assertEqual(self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 1).serial, "SANITIZED-SN-0499")
        self.assertEqual(self.cached_bay().serial, self.DISK_B)

    async def test_a_restore_that_removes_an_assignment_is_refused_after_the_bay_changed(self) -> None:
        # Removing a bay's assignment is a write to it as well, as a clear is.
        await self.open_editor(saved_notes="SANITIZED notes for disk A")
        bundle = {"schema_version": 1, "mappings": []}
        preview = await self.preview(bundle)
        self.assertEqual([item["slot"] for item in preview["removals"]], [0])
        response = await self.write_while_the_running_refresh_reads(
            lambda: self.confirm(bundle, preview), release_after=USER_WAIT_SECONDS * 3, serial=self.DISK_B,
        )
        status, _headers, content = response
        self.assertEqual(status, 409, content[:400])
        self.assertEqual(json.loads(content)["error"], "mapping_import_bays_changed")
        self.assertEqual(
            self.service.mapping_store.get_mapping(SYSTEM_ID, SHELF, 0).notes, "SANITIZED notes for disk A",
        )


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
