"""An incomplete refresh keeps trusted facts but discloses the failure (#1208).

When a refresh returns incomplete topology and a trusted snapshot is cached,
the cached slots stay, with the time they were read. The answer must also say
that the latest attempt failed and that the view is retained: the attempt's
source status and warnings travel with it, and the strict reader used by the
snapshot export returns that warning. The canonical cached snapshot itself is
never changed.

Every id here is synthetic; the snapshot build is replaced, so no appliance or
SSH call is made.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import EnclosureOption, InventorySnapshot, SlotView, SourceStatus
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore

SHELF = "synthetic-shelf"
READ_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
RETAINED_PHRASE = "latest inventory refresh was incomplete"
ATTEMPT_WARNING = "Synthetic enclosure query failed."


def build_service(temp_dir: str, platform: str) -> InventoryService:
    settings = Settings()
    system = SystemConfig(id="synthetic-system", truenas=TrueNASConfig(platform=platform))
    unexpected = AsyncMock(side_effect=AssertionError("unexpected appliance call"))
    return InventoryService(
        settings,
        system,
        unexpected,
        AsyncMock(side_effect=AssertionError("unexpected SSH call")),
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )


def slot(vdev_name: str) -> SlotView:
    return SlotView(
        slot=0,
        slot_label="00",
        row_index=0,
        column_index=0,
        device_name="disk0",
        pool_name="synthetic-pool",
        vdev_name=vdev_name,
        vdev_class="data",
    )


def trusted_snapshot(service: InventoryService) -> InventorySnapshot:
    platform = service.system.truenas.platform
    return InventorySnapshot(
        slots=[slot("mirror-0")],
        refresh_interval_seconds=30,
        last_updated=READ_AT,
        generated_at=READ_AT,
        selected_system_id=service.system.id,
        selected_system_platform=platform,
        selected_enclosure_id=SHELF,
        enclosures=[EnclosureOption(id=SHELF, label="Synthetic shelf", slot_count=1)],
        platform_context={"topology_complete": True} if platform == "quantastor" else {},
        sources={"api": SourceStatus(enabled=True, ok=True, message="Synthetic read succeeded.")},
    )


def incomplete_snapshot(service: InventoryService) -> InventorySnapshot:
    platform = service.system.truenas.platform
    return InventorySnapshot(
        slots=[slot("disk")] if platform == "quantastor" else [],
        refresh_interval_seconds=30,
        selected_system_id=service.system.id,
        selected_system_platform=platform,
        selected_enclosure_id=SHELF,
        enclosures=[],
        platform_context={"topology_complete": False} if platform == "quantastor" else {},
        sources={"api": SourceStatus(enabled=True, ok=False, message="Synthetic read failed.")},
        warnings=[ATTEMPT_WARNING],
    )


def seed_expired(service: InventoryService, snapshot: InventorySnapshot) -> None:
    service._cache[SHELF] = snapshot
    service._cache_until[SHELF] = datetime.now(timezone.utc) - timedelta(seconds=1)


class IncompleteRefreshStatusTests(unittest.IsolatedAsyncioTestCase):
    def assert_retained_with_failure(self, returned: InventorySnapshot, trusted: InventorySnapshot) -> None:
        # The trusted facts and their read time are kept.
        self.assertEqual(returned.slots[0].vdev_name, "mirror-0")
        self.assertEqual(returned.last_updated, READ_AT)
        self.assertEqual([option.id for option in returned.enclosures], [SHELF])
        # The latest attempt's failure is disclosed, not the old "ok".
        self.assertFalse(returned.sources["api"].ok)
        self.assertEqual(returned.sources["api"].message, "Synthetic read failed.")
        self.assertTrue(returned.warnings, "a retained view must carry a warning")
        self.assertIn(RETAINED_PHRASE, returned.warnings[0])
        self.assertIn("2026-01-02 03:04:05 UTC", returned.warnings[0])
        self.assertIn(ATTEMPT_WARNING, returned.warnings)
        # The canonical last-good snapshot is unchanged.
        self.assertIs(self.service._cache[SHELF], trusted)
        self.assertTrue(trusted.sources["api"].ok)
        self.assertEqual(trusted.warnings, [])
        self.assertIsNot(returned, trusted)

    async def test_forced_refresh_discloses_failed_attempt_on_core(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.service = build_service(temp_dir, "core")
            trusted = trusted_snapshot(self.service)
            seed_expired(self.service, trusted)
            self.service._build_snapshot = AsyncMock(return_value=incomplete_snapshot(self.service))

            result = await self.service._get_snapshot_result(force_refresh=True, selected_enclosure_id=SHELF)

            self.assertEqual(result.cache_state, "trusted-fallback")
            self.assert_retained_with_failure(result.value, trusted)
            returned = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)
            self.assert_retained_with_failure(returned, trusted)

    async def test_expired_read_discloses_failed_attempt_on_quantastor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.service = build_service(temp_dir, "quantastor")
            trusted = trusted_snapshot(self.service)
            seed_expired(self.service, trusted)
            self.service._build_snapshot = AsyncMock(return_value=incomplete_snapshot(self.service))

            returned = await self.service.get_snapshot(selected_enclosure_id=SHELF)

            self.assert_retained_with_failure(returned, trusted)

    async def test_strict_reader_returns_retained_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.service = build_service(temp_dir, "core")
            trusted = trusted_snapshot(self.service)
            seed_expired(self.service, trusted)
            self.service._build_snapshot = AsyncMock(return_value=incomplete_snapshot(self.service))

            returned, warning = await self.service.get_current_or_cached_snapshot(SHELF)

            self.assertIsNotNone(warning, "the export must be told the view is retained")
            self.assertIn(RETAINED_PHRASE, warning)
            self.assertEqual(returned.warnings[0], warning)
            self.assert_retained_with_failure(returned, trusted)

    async def test_complete_refresh_publishes_without_retained_warning(self) -> None:
        # Control: a complete refresh replaces the cache and carries no
        # retained-data warning, so the guard above is what adds it.
        with tempfile.TemporaryDirectory() as temp_dir:
            self.service = build_service(temp_dir, "core")
            seed_expired(self.service, trusted_snapshot(self.service))
            fresh = trusted_snapshot(self.service).model_copy(
                update={"last_updated": READ_AT + timedelta(hours=1), "slots": [slot("mirror-1")]}
            )
            self.service._build_snapshot = AsyncMock(return_value=fresh)

            returned, warning = await self.service.get_current_or_cached_snapshot(SHELF)

            self.assertIsNone(warning)
            self.assertEqual(returned.slots[0].vdev_name, "mirror-1")
            self.assertTrue(returned.sources["api"].ok)
            self.assertFalse(any(RETAINED_PHRASE in item for item in returned.warnings))
            self.assertIs(self.service._cache[SHELF], fresh)

    async def test_incomplete_refresh_without_trusted_cache_is_returned_as_is(self) -> None:
        # Control: with nothing trusted to keep, the failed attempt itself is
        # the answer, with no retained-data claim.
        with tempfile.TemporaryDirectory() as temp_dir:
            self.service = build_service(temp_dir, "core")
            attempt = incomplete_snapshot(self.service)
            self.service._build_snapshot = AsyncMock(return_value=attempt)

            returned = await self.service.get_snapshot(force_refresh=True, selected_enclosure_id=SHELF)

            self.assertFalse(returned.sources["api"].ok)
            self.assertEqual(returned.warnings, [ATTEMPT_WARNING])


if __name__ == "__main__":
    unittest.main()
