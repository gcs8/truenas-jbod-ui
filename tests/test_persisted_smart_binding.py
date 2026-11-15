"""Persisted slot details belong to the appliance that produced them (#1207).

A configured system ID names a slot in the settings, not an appliance. When a
reload keeps the ID but points it at another endpoint, the slot-detail store is
reused, so its rows must say which collection wrote them and every reader must
refuse rows written for a different one. Synthetic fixtures only: the
appliance client is a mock that always fails, no network is touched.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import PathConfig, Settings, SSHConfig, SystemConfig, TrueNASConfig
from app.models.domain import SlotView, SmartSummaryView
from app.route_support import ensure_read_slot_bounds
from app.services.inventory_registry import InventoryRegistry
from app.services.slot_detail_store import SlotDetailCacheEntry
from app.services.truenas_ws import TrueNASAPIError

SYSTEM_ID = "synthetic-system"
SHELF = "synthetic-shelf"


def _system(host: str, *, platform: str = "core", label: str | None = None) -> SystemConfig:
    return SystemConfig(
        id=SYSTEM_ID,
        label=label,
        truenas=TrueNASConfig(platform=platform, host=host),
        ssh=SSHConfig(enabled=False),
    )


def _settings(root: Path, system: SystemConfig) -> Settings:
    return Settings(
        systems=[system],
        default_system_id=system.id,
        paths=PathConfig(**{name: str(root / f"{name}.json") for name in PathConfig.model_fields}),
        config_file=str(root / "synthetic-settings.yaml"),
    )


def _slot(serial: str = "SANITIZED-DISK-A") -> SlotView:
    return SlotView(
        slot=0, slot_label="0", row_index=0, column_index=0, enclosure_id=SHELF,
        present=True, state="healthy", device_name="da0", smart_device_names=["da0"],
        serial=serial,
    )


class _QuietInventoryLogs:
    """The failing synthetic collection logs warnings; keep test output readable."""

    def __enter__(self):
        self._logger = logging.getLogger("app.services.inventory")
        self._level = self._logger.level
        self._logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *exc):
        self._logger.setLevel(self._level)
        return False


class PersistedSmartBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.old_system = _system("https://source-a.example.test")
        self.original = InventoryRegistry(_settings(self.root, self.old_system))
        self.predecessor = self.original.get_service(SYSTEM_ID)
        # The row is written the way the predecessor's own SMART read writes it.
        entry = self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=17),
        )
        self.assertIsNotNone(entry)
        self.original.slot_detail_store.save_entries([entry])

    def _reload(self, system: SystemConfig) -> InventoryRegistry:
        successor = InventoryRegistry(_settings(self.root, system), previous=self.original)
        # The store is reused across the reload; that is what makes this a risk.
        self.assertIs(successor.slot_detail_store, self.original.slot_detail_store)
        return successor

    def _layout_unavailable_service(self, registry: InventoryRegistry):
        service = registry.get_service(SYSTEM_ID)
        service.truenas_client.fetch_all = AsyncMock(side_effect=TrueNASAPIError("Synthetic source unavailable"))
        with _QuietInventoryLogs():
            bounds = asyncio.run(ensure_read_slot_bounds(0, service, SHELF))
        # The real read-bound resolver admits the cache-only fallback.
        self.assertEqual(bounds, "unavailable")
        return service

    def test_endpoint_change_refuses_predecessor_rows_for_single_and_batch_reads(self) -> None:
        service = self._layout_unavailable_service(self._reload(_system("https://source-b.example.test")))

        single = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        batch = service.get_cached_slot_smart_summaries_without_layout([0], SHELF)

        self.assertIsNone(single, "another appliance's SMART row was served as this one's")
        self.assertEqual(batch, {})

    def test_same_endpoint_reload_still_serves_its_own_rows(self) -> None:
        # Control: a display-only change keeps the same collection identity.
        service = self._layout_unavailable_service(
            self._reload(_system("https://source-a.example.test", label="Renamed shelf")),
        )

        single = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        batch = service.get_cached_slot_smart_summaries_without_layout([0], SHELF)

        self.assertIsNotNone(single)
        self.assertEqual(single.power_on_hours, 17)
        self.assertTrue(single.available)
        self.assertEqual(batch[0].power_on_hours, 17)

    def test_platform_change_at_same_endpoint_refuses_predecessor_rows(self) -> None:
        successor = self._reload(_system("https://source-a.example.test", platform="scale"))
        service = successor.get_service(SYSTEM_ID)

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))

    def test_late_predecessor_write_stays_bound_to_the_old_endpoint(self) -> None:
        successor = self._reload(_system("https://source-b.example.test"))
        service = self._layout_unavailable_service(successor)
        # A SMART read the predecessor started before the reload finishes now.
        late = self.predecessor._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=23),
        )
        self.assertIsNotNone(late)
        successor.slot_detail_store.save_entries([late])

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))
        self.assertEqual(service.get_cached_slot_smart_summaries_without_layout([0], SHELF), {})

    def test_successor_row_is_served_and_predecessor_cannot_read_it(self) -> None:
        successor = self._reload(_system("https://source-b.example.test"))
        service = self._layout_unavailable_service(successor)
        own = service._build_slot_detail_entry(
            _slot(), smart_summary=SmartSummaryView(available=True, power_on_hours=321),
        )
        self.assertIsNotNone(own)
        successor.slot_detail_store.save_entries([own])

        served = service.get_cached_slot_smart_summary_without_layout(0, SHELF)
        self.assertIsNotNone(served)
        self.assertEqual(served.power_on_hours, 321)
        self.assertIsNone(self.predecessor.get_cached_slot_smart_summary_without_layout(0, SHELF))

    def test_layout_readers_refuse_predecessor_rows_for_the_same_disk(self) -> None:
        # Readers that hold a live slot view also gate on the binding, not only
        # on the disk identity: the row is the predecessor's observation.
        service = self._reload(_system("https://source-b.example.test")).get_service(SYSTEM_ID)
        live = _slot()

        self.assertIsNone(service._build_persisted_smart_summary(live))
        self.assertIsNone(service._previous_slot_detail_entry(live))
        rebuilt = service._build_slot_detail_entry(live, smart_summary=None)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.smart_fields, {}, "predecessor SMART was carried into a successor row")

    def test_layout_readers_still_serve_own_rows(self) -> None:
        # Control for the layout readers on an unchanged endpoint.
        service = self._reload(_system("https://source-a.example.test", label="Renamed shelf")).get_service(SYSTEM_ID)

        persisted = service._build_persisted_smart_summary(_slot())
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.power_on_hours, 17)
        rebuilt = service._build_slot_detail_entry(_slot(), smart_summary=None)
        self.assertEqual(rebuilt.smart_fields.get("power_on_hours"), 17)
        self.assertTrue(rebuilt.smart_stale)

    def test_unbound_row_is_not_served(self) -> None:
        # A row with no recorded collection identity cannot show which
        # appliance wrote it, so it is refused rather than guessed.
        store = self.original.slot_detail_store
        store.save_entries([SlotDetailCacheEntry(
            system_id=SYSTEM_ID, enclosure_id=SHELF, slot=0, identifiers=["sanitized-disk-a", "da0"],
            slot_fields={"serial": "SANITIZED-DISK-A"},
            smart_fields={"available": True, "power_on_hours": 5},
        )])
        service = self._layout_unavailable_service(
            self._reload(_system("https://source-a.example.test", label="Renamed shelf")),
        )

        self.assertIsNone(service.get_cached_slot_smart_summary_without_layout(0, SHELF))
        self.assertIsNone(service._build_persisted_smart_summary(_slot()))


if __name__ == "__main__":
    unittest.main()
