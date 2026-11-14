"""A user reload or save must not answer busy behind the app's own refresh (#1113).

Every id here is synthetic. The appliance read is replaced by a fake that holds
a slot of a real `MiddlewareCallLimiter`, so the tests also prove the fix adds
no appliance call and never overlaps two.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app import main as app_main
from app import routes as app_routes
from app.config import Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import SourceStatus
from app.services import inventory as inventory_module
from app.services.mapping_store import MappingStore, bind_mapping_revision
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import MiddlewareCallLimiter, TrueNASRawData
from tests import core_ses_fixtures as fx
from tests import wait_bounds

SHELF = "500cccc00000110"
SYSTEM_ID = "synthetic-scale"
SSH_OUTPUTS = {"sesutil map": fx.sesutil_map(("ses0", "ExampleCo Shelf 0100", SHELF, 12, {0: "da0"}))}


class FakeAppliance:
    """Stands in for one appliance read, counted against a real call budget."""

    def __init__(self, limit: int = 1) -> None:
        self.limiter = MiddlewareCallLimiter(limit)
        self.reads = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.hold: asyncio.Event | None = None
        self.holding = asyncio.Event()

    async def collect(self) -> inventory_module.InventorySourceBundle:
        async with self.limiter.slot():
            self.reads += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                hold, self.hold = self.hold, None
                if hold is not None:
                    self.holding.set()
                    await hold.wait()
                return inventory_module.InventorySourceBundle(
                    raw_data=TrueNASRawData(enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]),
                    ssh_outputs=dict(SSH_OUTPUTS),
                    ssh_collected=True,
                    warnings=[],
                    sources={
                        "api": SourceStatus(enabled=True, ok=False, message="API unreachable"),
                        "ssh": SourceStatus(enabled=True, ok=True),
                    },
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            finally:
                self.in_flight -= 1


def editor_revision(service: inventory_module.InventoryService, slot: int, *, clear: bool = False) -> str:
    """The save or clear revision the bay editor holds for `slot` right now.

    That is the store's current revision bound to the disk the cached view of
    the shelf shows in the bay (#1141), as the page's slot view carries it.
    """
    store = service.mapping_store
    revision = (store.clear_revision if clear else store.save_revision)(SYSTEM_ID, SHELF, slot)
    cached = service._cache.get(SHELF)
    bay = next((item for item in cached.slots if item.slot == slot), None) if cached else None
    return bind_mapping_revision(revision, service._slot_disk_binding(bay))


def build_service(temp_dir: str, appliance: FakeAppliance) -> inventory_module.InventoryService:
    settings = Settings()
    system = SystemConfig(
        id=SYSTEM_ID,
        label="Synthetic SCALE",
        truenas=TrueNASConfig(host="https://nas.example.test", api_key="token", platform="scale"),
        ssh=SSHConfig(enabled=True, host="nas.example.test", user="jbodmap"),
    )
    service = inventory_module.InventoryService(
        settings,
        system,
        AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )
    service._collect_inventory_source_bundle = appliance.collect
    return service


async def call_app(
    application,
    method: str,
    path: str,
    *,
    query: str = "",
    body: dict | None = None,
) -> tuple[int, dict[str, str], bytes]:
    payload = b"" if body is None else json.dumps(body).encode()
    messages: list[dict] = []
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1")]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    await application(
        {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
            "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80),
        },
        receive,
        send,
    )
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {key.decode().lower(): value.decode() for key, value in start.get("headers", [])}
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], response_headers, content


async def wait_until(condition) -> None:
    # The save reaches its readback through worker-thread hops, so a fixed
    # number of loop turns is not enough on a loaded host (#1177).
    await wait_bounds.wait_until(condition)


class UserActionsDuringBackgroundRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.appliance = FakeAppliance(limit=1)
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
        self.application = app_main.create_app()
        # The page has loaded once: the shelf's snapshot and sources are cached.
        await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        self.assertEqual(self.appliance.reads, 1)

    def expire_snapshot(self) -> None:
        self.service._cache_until[SHELF] = datetime.min.replace(tzinfo=timezone.utc)

    def assert_budget_respected(self, *, reads: int) -> None:
        # Never more than the budget in flight, and no extra appliance reads.
        self.assertLessEqual(self.appliance.limiter.peak_active, self.appliance.limiter.limit)
        self.assertEqual(self.appliance.peak_in_flight, 1)
        self.assertEqual(self.appliance.reads, reads)
        self.assertEqual(self.appliance.limiter.active, 0)

    async def test_bay_assignment_save_while_the_page_refresh_reads_the_appliance(self) -> None:
        self.expire_snapshot()
        release = asyncio.Event()
        self.appliance.hold = release
        # A stale page read is served from cache and starts the page's own
        # background refresh, which is now reading the appliance.
        await self.service.get_snapshot(selected_enclosure_id=SHELF, allow_stale_cache=True)
        await asyncio.wait_for(self.appliance.holding.wait(), timeout=wait_bounds.HANG_TIMEOUT_SECONDS)

        revision = editor_revision(self.service, 0)
        save = asyncio.create_task(call_app(
            self.application,
            "POST",
            "/api/slots/0/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={"expected_revision": revision, "serial": "SYNTH-SN-0001", "notes": "synthetic"},
        ))
        # The save has stored the assignment and now waits for its readback.
        await wait_until(lambda: self.service._snapshot_activity.get(SHELF))
        self.assertFalse(save.done())
        release.set()
        status, headers, content = await asyncio.wait_for(save, timeout=5)

        self.assertEqual(status, 200, content)
        self.assertNotIn("retry-after", headers)
        payload = json.loads(content)
        self.assertTrue(payload["ok"])
        bay = next(slot for slot in payload["snapshot"]["slots"] if slot["slot"] == 0)
        self.assertEqual(bay["serial"], "SYNTH-SN-0001")
        # The page refresh's read and the save's own forced read, one at a time.
        self.assert_budget_respected(reads=3)

    async def test_bay_assignment_save_while_the_page_refresh_rebuilds_the_snapshot(self) -> None:
        release = asyncio.Event()
        building = asyncio.Event()
        build_snapshot = self.service._build_snapshot

        async def hold_background_build(*args, **kwargs):
            if inventory_module._snapshot_background_refresh.get() and not building.is_set():
                building.set()
                await release.wait()
            return await build_snapshot(*args, **kwargs)

        self.service._build_snapshot = hold_background_build
        # The page's own refresh (as after a locate press) is rebuilding the
        # shelf while the snapshot the save reads first is still fresh.
        self.service._schedule_background_snapshot_refresh(SHELF)
        await asyncio.wait_for(building.wait(), timeout=wait_bounds.HANG_TIMEOUT_SECONDS)
        self.assertTrue(self.service._snapshot_activity.get(SHELF))

        revision = editor_revision(self.service, 0)
        save = asyncio.create_task(call_app(
            self.application,
            "POST",
            "/api/slots/0/mapping",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
            body={"expected_revision": revision, "serial": "SYNTH-SN-0002"},
        ))
        await wait_until(lambda: self.service._snapshot_activity.get(SHELF, 0) >= 2)
        release.set()
        status, headers, content = await asyncio.wait_for(save, timeout=5)

        self.assertEqual(status, 200, content)
        bay = next(slot for slot in json.loads(content)["snapshot"]["slots"] if slot["slot"] == 0)
        self.assertEqual(bay["serial"], "SYNTH-SN-0002")
        # The background read, then the save's forced read.
        self.assert_budget_respected(reads=3)

    async def test_page_reload_after_a_rename_while_the_readback_still_runs(self) -> None:
        release = asyncio.Event()
        self.appliance.hold = release
        # The rename's forced readback is reading the appliance ...
        readback = asyncio.create_task(
            self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
        )
        await asyncio.wait_for(self.appliance.holding.wait(), timeout=wait_bounds.HANG_TIMEOUT_SECONDS)
        # ... when the name is saved back, invalidating that shelf again.
        self.service.invalidate_physical_enclosure_snapshot_cache(reason="test.rename", enclosure_id=SHELF)

        reload = asyncio.create_task(call_app(
            self.application,
            "GET",
            "/",
            query=f"system_id={SYSTEM_ID}&enclosure_id={SHELF}",
        ))
        await wait_until(lambda: self.service._snapshot_activity.get(SHELF, 0) >= 2)
        release.set()
        status, headers, content = await asyncio.wait_for(reload, timeout=5)
        # The readback began before the second save; it must not publish.
        with self.assertRaises(inventory_module.SnapshotStateBusyError):
            await readback

        self.assertEqual(status, 200, content[:400])
        self.assertNotIn("retry-after", headers)
        self.assertIn(SHELF.encode(), content)
        # The reload's snapshot stays cached for the next request.
        self.assertIn(SHELF, self.service._cache)
        self.assertNotIn(SHELF, self.service._snapshot_invalidated)
        # The readback's read only; the reload reused the sources it published.
        self.assert_budget_respected(reads=2)

    async def test_request_that_began_before_an_invalidation_still_fails_closed(self) -> None:
        # The fix narrows the fence to requests that began before the change;
        # it does not let an older build publish over the newer state.
        release = asyncio.Event()
        building = asyncio.Event()
        build_snapshot = self.service._build_snapshot

        async def hold_first_build(*args, **kwargs):
            if not building.is_set():
                building.set()
                await release.wait()
            return await build_snapshot(*args, **kwargs)

        self.service._build_snapshot = hold_first_build
        older = asyncio.create_task(self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF))
        await asyncio.wait_for(building.wait(), timeout=wait_bounds.HANG_TIMEOUT_SECONDS)
        self.service.invalidate_physical_enclosure_snapshot_cache(reason="test.save", enclosure_id=SHELF)
        release.set()
        with self.assertRaises(inventory_module.SnapshotStateBusyError):
            await older
        self.assertNotIn(SHELF, self.service._cache)


if __name__ == "__main__":
    unittest.main()
