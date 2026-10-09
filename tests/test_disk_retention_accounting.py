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
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import MultipathMember, MultipathView, SlotView, SourceStatus
from app.services import inventory as inventory_module
from app.services.inventory import InventoryService, InventorySourceBundle
from app.services.inventory_accounting import (
    build_disk_retention_accounting,
    disk_record_identity_tokens,
    logical_disk_identity_tokens,
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


def synthetic_enclosure_rows(
    disks: list[dict[str, object]] = MIXED_IDENTITY_DISKS,
) -> list[dict[str, object]]:
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
                    for index, disk in enumerate(disks)
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
    source_bundle = AsyncMock(
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
    service._get_inventory_source_bundle = source_bundle
    snapshot = asyncio.run(service.get_snapshot(force_refresh=True))
    source_bundle.assert_awaited_once()
    return snapshot


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
            scsi_hctl="0:0:0:0",
            enclosure_id="enc-a",
        )

        self.assertEqual(slot_identity_tokens(unknown), frozenset())
        accounting = build_disk_retention_accounting(source_disks=[], slots=[unknown])
        self.assertEqual(accounting.rendered_unique_disk_count, 0)

    def test_display_labels_never_become_identity_tokens(self) -> None:
        for label in ("GPTID", "WWN", "Disk ID", "Serial/LUN ID", "da9"):
            with self.subTest(label=label):
                self.assertEqual(
                    logical_disk_identity_tokens(
                        serial="SANITIZED-RETENTION-A",
                        logical_unit_id="SANITIZED-LUN-A",
                        gptid="/dev/gptid/00000000-0000-4000-8000-000000000001",
                        persistent_id_label=label,
                        device_names=("/dev/da0",),
                    ),
                    frozenset({
                        "serial:sanitized-retention-a",
                        "lun:sanitized-lun-a",
                        "gptid:gptid/00000000-0000-4000-8000-000000000001",
                        "dev:da0",
                    }),
                )

    def test_label_only_occupied_bay_is_not_a_logical_disk(self) -> None:
        slot = self._slot(present=True, identity_state="unknown", persistent_id_label="GPTID")
        self.assertEqual(slot_identity_tokens(slot), frozenset())
        self.assertEqual(
            build_disk_retention_accounting(source_disks=[], slots=[slot]).rendered_unique_disk_count,
            0,
        )

    def test_real_persistent_aliases_still_deduplicate_without_serial_or_device(self) -> None:
        slots = [
            self._slot(
                gptid="/dev/gptid/00000000-0000-4000-8000-000000000001",
                persistent_id_label="GPTID",
            ),
            self._slot(
                slot=1,
                gptid="gptid/00000000-0000-4000-8000-000000000001",
                persistent_id_label="Disk ID",
            ),
        ]
        accounting = build_disk_retention_accounting(source_disks=[], slots=slots)
        self.assertEqual(accounting.rendered_unique_disk_count, 1)
        self.assertEqual(accounting.duplicate_disk_view_count, 1)

    def test_unknown_source_identity_remains_unplaced(self) -> None:
        accounting = build_disk_retention_accounting(
            source_disks=[SimpleNamespace()],
            slots=[self._slot(present=True, persistent_id_label="GPTID")],
        )
        self.assertEqual(accounting.source_disk_count, 1)
        self.assertEqual(accounting.unplaced_disk_count, 1)
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

    def _assert_distinct_gptid_classes(self, *, physical: bool) -> None:
        # Independent fixture oracle: da0 and da1 are different disks, even
        # though both public slot producers give them the display label GPTID.
        # Do not derive these expected classes from production token helpers.
        expected = {
            "da0": ("SANITIZED-RETENTION-A", "gptid/00000000-0000-4000-8000-000000000001"),
            "da1": (None, "gptid/00000000-0000-4000-8000-000000000002"),
        }
        disks = [
            {
                "name": name,
                "serial": serial,
                "identifier": gptid,
                "model": "Synthetic retention disk",
                "size": 1_000_000_000,
                "status": "ONLINE",
            }
            for name, (serial, gptid) in expected.items()
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="system-a",
                default_profile_id="supermicro-cse-946-top-60",
                truenas=TrueNASConfig(platform="core"),
            )
            service = build_service(Settings(systems=[system]), system, temp_dir)
            snapshot = build_snapshot(
                service,
                raw_data_for(disks, enclosures=synthetic_enclosure_rows(disks) if physical else []),
            )
            source_disks = service._build_disk_records(disks, ParsedSSHData(), {}, {})

        occupied = [slot for slot in snapshot.slots if slot.device_name]
        self.assertCountEqual([slot.device_name for slot in occupied], expected)
        for name, identity in expected.items():
            group = [slot for slot in occupied if slot.device_name == name]
            self.assertEqual(len(group), 1)
            self.assertEqual((group[0].serial, group[0].gptid), identity)
            self.assertEqual(group[0].persistent_id_label, "GPTID")
            if physical:
                self.assertFalse(group[0].raw_status.get("virtual_enclosure", False))
            else:
                self.assertTrue(group[0].raw_status["virtual_enclosure"])
                self.assertFalse(group[0].physical_location_known)
                self.assertFalse(group[0].mapping_supported)
                self.assertFalse(group[0].led_supported)
            # Each separately selected class represents only its own source
            # disk. A shared label must not give credit for the other disk.
            with self.subTest(only_rendered_class=name):
                accounting = build_disk_retention_accounting(source_disks=source_disks, slots=group)
                self.assertEqual(accounting.source_disk_count, 2)
                self.assertEqual(accounting.rendered_unique_disk_count, 1)
                self.assertEqual(accounting.duplicate_disk_view_count, 0)
                self.assertEqual(accounting.unplaced_disk_count, 1)

        summary = snapshot.summary
        self.assertEqual(
            (
                summary.source_disk_count,
                summary.rendered_unique_disk_count,
                summary.duplicate_disk_view_count,
                summary.unplaced_disk_count,
            ),
            (2, 2, 0, 0),
        )

    def test_public_physical_snapshot_keeps_shared_gptid_labels_in_distinct_classes(self) -> None:
        self._assert_distinct_gptid_classes(physical=True)

    def test_public_virtual_snapshot_keeps_shared_gptid_labels_in_distinct_classes(self) -> None:
        self._assert_distinct_gptid_classes(physical=False)

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


class SystemWideRetentionTests(unittest.TestCase):
    """#911: disks shown by another enclosure or a storage view are placed."""

    @staticmethod
    def _shelf(enclosure_id: str, disks: list[dict[str, object]]) -> dict[str, object]:
        return synthetic_enclosure_rows(disks)[0] | {
            "id": enclosure_id,
            "name": f"Shelf {enclosure_id}",
            "label": f"Shelf {enclosure_id}",
        }

    def _service(self, temp_dir: str, *, storage_views: list[dict[str, object]] | None = None):
        system = SystemConfig(
            id="system-a",
            truenas=TrueNASConfig(platform="core"),
            storage_views=storage_views or [],
        )
        service = build_service(Settings(systems=[system]), system, temp_dir)
        raw_data = raw_data_for(
            MIXED_IDENTITY_DISKS,
            enclosures=[
                self._shelf("enc-a", MIXED_IDENTITY_DISKS[:2]),
                # da3 and da4 are two paths to one disk.
                self._shelf("enc-b", MIXED_IDENTITY_DISKS[3:5]),
            ],
        )
        source_bundle = AsyncMock(
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
        service._get_inventory_source_bundle = source_bundle
        return service, source_bundle

    BOOT_VIEW = {
        "id": "boot",
        "label": "Boot",
        "kind": "boot_devices",
        "template_id": "satadom-pair-2",
        "enabled": True,
        "binding": {"mode": "serial", "device_names": ["da2"]},
    }

    def test_every_enclosure_and_storage_view_counts_as_placed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[self.BOOT_VIEW])

            async def run():
                default = await service.get_snapshot()
                retention = await service.get_system_disk_retention()
                return default, retention, await service.get_snapshot()

            default, retention, default_after = asyncio.run(run())

        # The default enclosure alone leaves the other shelf and the boot
        # view's disk unplaced; that is the per-enclosure summary.
        self.assertEqual(default.selected_enclosure_id, "enc-a")
        self.assertEqual(default.summary.unplaced_disk_count, 3)
        self.assertEqual(
            (
                retention.source_disk_count,
                retention.rendered_unique_disk_count,
                retention.duplicate_disk_view_count,
                retention.unplaced_disk_count,
            ),
            (5, 4, 1, 0),
        )
        # Building the other enclosure must not move the default page.
        self.assertEqual(default_after.selected_enclosure_id, "enc-a")

    def test_a_disk_no_enclosure_or_view_shows_stays_unplaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # No storage view claims da2.
            service, _ = self._service(temp_dir)
            retention = asyncio.run(service.get_system_disk_retention())

        self.assertEqual(retention.source_disk_count, 5)
        self.assertEqual(retention.unplaced_disk_count, 1)

    def test_system_totals_collect_sources_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, source_bundle = self._service(temp_dir, storage_views=[self.BOOT_VIEW])
            asyncio.run(service.get_system_disk_retention(force_refresh=True))

        forced = [call for call in source_bundle.await_args_list if call.kwargs.get("force_refresh")]
        self.assertEqual(len(forced), 1)

    def test_a_disabled_storage_view_does_not_place_its_disk(self) -> None:
        # A view switched off in the admin UI renders nothing, so its disk
        # must not satisfy the upgrade check.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[{**self.BOOT_VIEW, "enabled": False}])
            retention = asyncio.run(service.get_system_disk_retention())

        self.assertEqual(retention.unplaced_disk_count, 1)

    def test_a_route_snapshot_is_reused_instead_of_collecting_again(self) -> None:
        # GET /api/inventory?retention_scope=system&force=true has already
        # force-refreshed one snapshot; the system totals build on it.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, source_bundle = self._service(temp_dir, storage_views=[self.BOOT_VIEW])

            async def run():
                snapshot = await service.get_snapshot(force_refresh=True)
                return await service.get_system_disk_retention(force_refresh=True, snapshot=snapshot)

            retention = asyncio.run(run())

        forced = [call for call in source_bundle.await_args_list if call.kwargs.get("force_refresh")]
        self.assertEqual(len(forced), 1)
        self.assertEqual(retention.unplaced_disk_count, 0)


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
