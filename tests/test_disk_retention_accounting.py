"""Synthetic regression for source-disk retention across the virtual fallback.

The v0.23.0 system-scoped virtual inventory correctly stops claiming physical
bay locations when no enclosure identity is trustworthy, but a release gate
could not mechanically prove that every source disk survived the change:
enclosure counts, slot counts and one preferred identifier all legitimately
differ between the predecessor and the successor rendering.

These tests pin the aggregate contract that makes retention provable without
exporting any disk identifier, and they exercise the same identity
normalization the renderer uses.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import MultipathMember, MultipathView, SlotView, SourceStatus
from app.services import inventory as inventory_module
from app.services.inventory import InventoryService, InventorySourceBundle
from app.services.inventory_accounting import (
    build_disk_retention_accounting,
    disk_record_identity_tokens,
    slot_identity_tokens,
)
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData
from app.services.profile_registry import ProfileRegistry
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData


# Mixed identity availability: a stable serial, a persistent/GPT id without a
# serial, a device-name-only fallback, and a multipath pair that is one disk.
MIXED_IDENTITY_DISKS: list[dict[str, object]] = [
    {
        "name": "da0",
        "serial": "SYNTH-RETENTION-A",
        "model": "Synthetic A",
        "size": 1_000_000_000,
        "status": "ONLINE",
    },
    {
        "name": "da1",
        "identifier": "{uuid}5d9b1f0e-0000-4000-8000-000000000001",
        "model": "Synthetic B",
        "size": 1_000_000_000,
        "status": "ONLINE",
    },
    {
        "name": "da2",
        "model": "Synthetic C",
        "size": 1_000_000_000,
        "status": "ONLINE",
    },
    {
        "name": "da3",
        "serial": "SYNTH-RETENTION-D",
        "model": "Synthetic D",
        "size": 1_000_000_000,
        "status": "ONLINE",
    },
    {
        # Second path to the same physical disk as da3.
        "name": "da4",
        "serial": "SYNTH-RETENTION-D",
        "model": "Synthetic D",
        "size": 1_000_000_000,
        "status": "ONLINE",
    },
]


def build_service(settings: Settings, system: SystemConfig, temp_dir: str) -> InventoryService:
    return InventoryService(
        settings,
        system,
        AsyncMock(),
        AsyncMock(),
        None,
        MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
        ProfileRegistry(settings),
        SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
    )


def synthetic_enclosure_rows() -> list[dict[str, object]]:
    """One physical enclosure that renders a bay per source disk."""
    return [
        {
            "id": "enc-a",
            "name": "Synthetic enclosure",
            "label": "Synthetic enclosure",
            "elements": {
                "Array Device Slot": [
                    {
                        # The API slot numbering base is 1.
                        "slot": index + 1,
                        "descriptor": f"Slot {index + 1:02d}",
                        "status": "OK",
                        "dev": disk["name"],
                    }
                    for index, disk in enumerate(MIXED_IDENTITY_DISKS)
                ]
            },
        }
    ]


def raw_data_for(disks: list[dict[str, object]], *, enclosures: list | None = None) -> TrueNASRawData:
    return TrueNASRawData(
        enclosures=enclosures or [],
        disks=disks,
        pools=[],
        disk_temperatures={},
        smart_test_results=[],
    )


def build_snapshot(service: InventoryService, raw_data: TrueNASRawData):
    service._get_inventory_source_bundle = AsyncMock(
        return_value=InventorySourceBundle(
            raw_data=raw_data,
            ssh_outputs={},
            ssh_collected=False,
            warnings=[],
            sources={"api": SourceStatus(enabled=True, ok=True)},
            scale_ses_data=ParsedSSHData(),
            quantastor_ses_data=ParsedSSHData(),
        )
    )
    return asyncio.run(service._build_snapshot())


class DiskRetentionAccountingUnitTests(unittest.TestCase):
    """The rendering and the acceptance check share one normalization."""

    def _slot(self, **fields) -> SlotView:
        base = {"slot": 0, "slot_label": "Disk 1", "row_index": 0, "column_index": 0}
        base.update(fields)
        return SlotView(**base)

    def test_several_physical_views_of_one_disk_form_one_equivalence_class(self) -> None:
        slots = [
            self._slot(slot=0, device_name="da3", serial="SYNTH-RETENTION-D"),
            self._slot(slot=1, device_name="da4", serial="SYNTH-RETENTION-D"),
            self._slot(
                slot=2,
                device_name="multipath/disk1",
                multipath=MultipathView(
                    name="disk1",
                    device_name="multipath/disk1",
                    members=[MultipathMember(device_name="da3"), MultipathMember(device_name="da4")],
                ),
            ),
        ]
        accounting = build_disk_retention_accounting(source_disks=[], slots=slots)

        self.assertEqual(accounting.rendered_unique_disk_count, 1)
        self.assertEqual(accounting.duplicate_disk_view_count, 2)

    def test_a_missing_source_disk_is_reported_as_unplaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            service = build_service(Settings(systems=[system]), system, temp_dir)
            source_disks = service._build_disk_records(MIXED_IDENTITY_DISKS, ParsedSSHData(), {}, {})
            rendered = [
                self._slot(slot=0, device_name="da0", serial="SYNTH-RETENTION-A"),
                self._slot(slot=1, device_name="da2"),
                self._slot(slot=2, device_name="da3", serial="SYNTH-RETENTION-D"),
                self._slot(slot=3, device_name="da4", serial="SYNTH-RETENTION-D"),
            ]

            accounting = build_disk_retention_accounting(source_disks=source_disks, slots=rendered)

            # da1 (persistent id, no serial) was dropped by the rendering.
            self.assertEqual(accounting.unplaced_disk_count, 1)

    def test_identity_free_occupied_bays_do_not_consume_a_source_disk(self) -> None:
        # Only bay-scoped attributes: nothing here identifies the occupant.
        unknown = self._slot(
            slot=0,
            present=True,
            identity_state="unknown",
            sas_address="0x5000000000000000",
            enclosure_id="enc-a",
        )

        self.assertEqual(slot_identity_tokens(unknown), frozenset())
        accounting = build_disk_retention_accounting(source_disks=[], slots=[unknown])
        self.assertEqual(accounting.rendered_unique_disk_count, 0)

    def test_aggregate_totals_expose_no_identifier_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            service = build_service(Settings(systems=[system]), system, temp_dir)
            source_disks = service._build_disk_records(MIXED_IDENTITY_DISKS, ParsedSSHData(), {}, {})

            accounting = build_disk_retention_accounting(source_disks=source_disks, slots=[])

            for value in vars(accounting).values() if hasattr(accounting, "__dict__") else (
                getattr(accounting, name) for name in accounting.__slots__
            ):
                self.assertIsInstance(value, int)
            self.assertTrue(disk_record_identity_tokens(source_disks[0]))


class VirtualFallbackRetentionTests(unittest.TestCase):
    """End-to-end aggregates across a physical-to-virtual inventory change."""

    def test_multi_system_virtual_fallback_retains_every_source_disk(self) -> None:
        systems = [
            SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core")),
            SystemConfig(id="system-b", truenas=TrueNASConfig(platform="core")),
        ]
        settings = Settings(systems=systems)
        for system in systems:
            with self.subTest(system=system.id), tempfile.TemporaryDirectory() as temp_dir:
                service = build_service(settings, system, temp_dir)

                snapshot = build_snapshot(service, raw_data_for(MIXED_IDENTITY_DISKS))

                summary = snapshot.summary
                self.assertEqual(summary.source_disk_count, len(MIXED_IDENTITY_DISKS))
                # The multipath pair is one logical disk.
                self.assertEqual(summary.rendered_unique_disk_count, 4)
                self.assertEqual(summary.duplicate_disk_view_count, 1)
                self.assertEqual(summary.unplaced_disk_count, 0)

    def test_a_dropped_source_disk_fails_the_aggregate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            service = build_service(Settings(systems=[system]), system, temp_dir)
            original = InventoryService._build_system_disk_virtual_enclosure

            def drop_one(self, disk_records, *args, **kwargs):
                return original(self, list(disk_records)[1:], *args, **kwargs)

            service._build_system_disk_virtual_enclosure = drop_one.__get__(service, InventoryService)
            snapshot = build_snapshot(service, raw_data_for(MIXED_IDENTITY_DISKS))

            self.assertEqual(snapshot.summary.source_disk_count, len(MIXED_IDENTITY_DISKS))
            self.assertGreater(snapshot.summary.unplaced_disk_count, 0)

    def test_physical_snapshot_reports_zero_unplaced_disks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="system-a",
                default_profile_id="supermicro-cse-946-top-60",
                truenas=TrueNASConfig(platform="core"),
            )
            settings = Settings(systems=[system])
            service = build_service(settings, system, temp_dir)
            snapshot = build_snapshot(
                service,
                raw_data_for(MIXED_IDENTITY_DISKS, enclosures=synthetic_enclosure_rows()),
            )

            self.assertEqual(snapshot.summary.source_disk_count, len(MIXED_IDENTITY_DISKS))
            self.assertEqual(snapshot.summary.unplaced_disk_count, 0)

    def test_side_by_side_predecessor_and_successor_prove_retention(self) -> None:
        """Retention is proven by totals, not by enclosure or slot equality."""
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="system-a",
                default_profile_id="supermicro-cse-946-top-60",
                truenas=TrueNASConfig(platform="core"),
            )
            settings = Settings(systems=[system])
            predecessor = build_snapshot(
                build_service(settings, system, temp_dir),
                raw_data_for(MIXED_IDENTITY_DISKS, enclosures=synthetic_enclosure_rows()),
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            successor = build_snapshot(
                build_service(settings, system, temp_dir),
                raw_data_for(MIXED_IDENTITY_DISKS),
            )

        self.assertNotEqual(predecessor.summary.enclosure_count, successor.summary.enclosure_count)
        self.assertEqual(
            predecessor.summary.source_disk_count,
            successor.summary.source_disk_count,
        )
        self.assertEqual(
            predecessor.summary.rendered_unique_disk_count,
            successor.summary.rendered_unique_disk_count,
        )
        self.assertEqual(predecessor.summary.unplaced_disk_count, 0)
        self.assertEqual(successor.summary.unplaced_disk_count, 0)


class VirtualFallbackWarningAndSourceStatusTests(unittest.TestCase):
    """Source health is tested separately from the virtual-rendering warning."""

    def test_virtual_fallback_warns_about_location_without_claiming_a_fetch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            service = build_service(Settings(systems=[system]), system, temp_dir)

            snapshot = build_snapshot(service, raw_data_for(MIXED_IDENTITY_DISKS))

            self.assertIn(
                inventory_module.VIRTUAL_INVENTORY_PHYSICAL_LOCATION_WARNING,
                snapshot.warnings,
            )
            self.assertIn(
                "does not indicate a source fetch failure",
                inventory_module.VIRTUAL_INVENTORY_PHYSICAL_LOCATION_WARNING,
            )
            for value in ("SYNTH", "da0", "system-a"):
                self.assertNotIn(value, inventory_module.VIRTUAL_INVENTORY_PHYSICAL_LOCATION_WARNING)

    def test_virtual_fallback_does_not_degrade_healthy_source_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            service = build_service(Settings(systems=[system]), system, temp_dir)

            snapshot = build_snapshot(service, raw_data_for(MIXED_IDENTITY_DISKS))

            self.assertTrue(snapshot.sources["api"].enabled)
            self.assertTrue(snapshot.sources["api"].ok)
            self.assertIsNone(snapshot.sources["api"].message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
