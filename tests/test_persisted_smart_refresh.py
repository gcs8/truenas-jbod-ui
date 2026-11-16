"""A persisted SMART hit's background refresh reads live data on its own (#1209).

Serving a persisted SMART row may return at once, but the refresh it schedules
must do a live read without waiting for another request, and a caller that did
not accept stale data must not receive the persisted row by joining the loader
that served it. Independent callers still share one in-flight read. Only the
SMART transport is fake; snapshot, caches and the slot-detail store are real.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import PathConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureOption, InventorySnapshot, SlotView, SmartSummaryView, SourceStatus
from app.services.inventory import InventoryService, utcnow
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore

SHELF = "synthetic-shelf"
PERSISTED_HOURS = 17
LIVE_HOURS = 321


class PersistedSmartRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        system = SystemConfig(
            id="synthetic-system",
            truenas=TrueNASConfig(platform="linux", host="https://source-a.example.test"),
            ssh=SSHConfig(enabled=True, host="source-a.example.test", commands=[]),
        )
        settings = Settings(
            systems=[system], default_system_id=system.id,
            paths=PathConfig(**{name: str(root / f"{name}.json") for name in PathConfig.model_fields}),
            config_file=str(root / "synthetic-settings.yaml"),
        )
        self.service = InventoryService(
            settings, system,
            AsyncMock(side_effect=AssertionError("unexpected appliance call")),
            AsyncMock(side_effect=AssertionError("unexpected SSH call")),
            None,
            MappingStore(settings.paths.mapping_file),
            ProfileRegistry(settings),
            SlotDetailStore(settings.paths.slot_detail_cache_file),
        )
        self.live = AsyncMock(return_value=(SmartSummaryView(available=True, power_on_hours=LIVE_HOURS), None))
        self.service._fetch_smart_summary_over_ssh = self.live

    def _publish_snapshot(self) -> SlotView:
        service = self.service
        slot = SlotView(
            slot=0, slot_label="0", row_index=0, column_index=0, enclosure_id=SHELF,
            present=True, state="healthy", device_name="sda", smart_device_names=["sda"],
            serial="SANITIZED-DISK-A",
        )
        snapshot = InventorySnapshot(
            slots=[slot], refresh_interval_seconds=30,
            selected_system_id=service.system.id, selected_system_platform=service.system.truenas.platform,
            selected_enclosure_id=SHELF,
            enclosures=[EnclosureOption(id=SHELF, label="Synthetic shelf", slot_count=1)],
            sources={"api": SourceStatus(enabled=True, ok=True, message="Synthetic successful read")},
        )
        service._cache[SHELF] = snapshot
        service._cache_until[SHELF] = utcnow() + timedelta(minutes=5)
        service._canonical_enclosure_options = {option.id: option for option in snapshot.enclosures}
        service._canonical_default_enclosure_id = SHELF
        service._observe_smart_disk_identities(snapshot.slots)
        return slot

    def _seed_persisted(self, slot: SlotView) -> None:
        entry = self.service._build_slot_detail_entry(
            slot, smart_summary=SmartSummaryView(available=True, power_on_hours=PERSISTED_HOURS),
        )
        self.assertIsNotNone(entry)
        self.service.slot_detail_store.save_entries([entry])

    async def _settle_refreshes(self) -> None:
        for _ in range(3):
            tasks = list(self.service._smart_refresh_tasks.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 5)
            await asyncio.sleep(0)

    def _cached_hours(self, slot: SlotView) -> int | None:
        cached = self.service._smart_cache.get(self.service._smart_cache_key(slot))
        return None if cached is None else cached.power_on_hours

    async def test_lone_persisted_read_refreshes_without_another_request(self) -> None:
        slot = self._publish_snapshot()
        self._seed_persisted(slot)

        first = await self.service.get_slot_smart_summary(0, SHELF, allow_stale_cache=True)
        self.assertEqual(first.power_on_hours, PERSISTED_HOURS)
        await self._settle_refreshes()

        # No second request has been made: the scheduled refresh alone must
        # have read the disk and replaced both the memory and the stored copy.
        self.assertEqual(self.live.await_count, 1, "the scheduled refresh did not perform a live read")
        self.assertEqual(self._cached_hours(slot), LIVE_HOURS)
        stored = self.service.slot_detail_store.get_entry(self.service.system.id, SHELF, 0)
        self.assertEqual(stored.smart_fields.get("power_on_hours"), LIVE_HOURS)
        self.assertFalse(stored.smart_stale)

    async def test_fresh_caller_joining_a_stale_loader_gets_live_data_in_one_read(self) -> None:
        slot = self._publish_snapshot()
        self._seed_persisted(slot)
        store = self.service.slot_detail_store
        release = threading.Event()
        real_load_all = store.load_all

        def gated_load_all():
            # Hold the stale-allowed loader at its store read so a second caller
            # can join it while it is still in flight.
            release.wait(5)
            return real_load_all()

        store.load_all = gated_load_all
        stale = asyncio.create_task(
            self.service._get_slot_smart_summary_for_slot_view(slot, allow_stale_cache=True),
        )
        key = self.service._smart_cache_key(slot)
        for _ in range(100):
            if key in self.service._smart_load_tasks:
                break
            await asyncio.sleep(0)
        self.assertIn(key, self.service._smart_load_tasks)
        stale_loader = self.service._smart_load_tasks[key]
        fresh = asyncio.create_task(
            self.service._get_slot_smart_summary_for_slot_view(slot, allow_stale_cache=False),
        )
        await asyncio.sleep(0)
        self.assertIs(self.service._smart_load_tasks.get(key), stale_loader, "the fresh caller did not join")
        release.set()

        stale_result, fresh_result = await asyncio.wait_for(asyncio.gather(stale, fresh), 5)
        await self._settle_refreshes()

        self.assertEqual(stale_result.power_on_hours, PERSISTED_HOURS)
        self.assertEqual(fresh_result.power_on_hours, LIVE_HOURS, "a fresh caller was served the persisted row")
        # The background refresh and the fresh caller share one live read.
        self.assertEqual(self.live.await_count, 1)
        self.assertEqual(self._cached_hours(slot), LIVE_HOURS)

    async def test_independent_fresh_callers_still_share_one_live_read(self) -> None:
        # Control: keyed single-flight for callers that want live data.
        slot = self._publish_snapshot()

        results = await asyncio.wait_for(asyncio.gather(
            self.service._get_slot_smart_summary_for_slot_view(slot),
            self.service._get_slot_smart_summary_for_slot_view(slot),
        ), 5)

        self.assertEqual([result.power_on_hours for result in results], [LIVE_HOURS, LIVE_HOURS])
        self.assertEqual(self.live.await_count, 1)

    async def test_concurrent_stale_callers_share_one_loader_and_one_refresh(self) -> None:
        slot = self._publish_snapshot()
        self._seed_persisted(slot)

        results = await asyncio.wait_for(asyncio.gather(
            self.service.get_slot_smart_summary(0, SHELF, allow_stale_cache=True),
            self.service.get_slot_smart_summary(0, SHELF, allow_stale_cache=True),
        ), 5)
        await self._settle_refreshes()

        self.assertEqual([result.power_on_hours for result in results], [PERSISTED_HOURS, PERSISTED_HOURS])
        self.assertEqual(self.live.await_count, 1)
        self.assertEqual(self._cached_hours(slot), LIVE_HOURS)


if __name__ == "__main__":
    unittest.main()
