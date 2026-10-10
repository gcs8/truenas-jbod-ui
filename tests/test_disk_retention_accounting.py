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
from unittest.mock import AsyncMock, patch

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


class QuantaStorNodeScopedRetentionTests(unittest.TestCase):
    """#917: device names are node-local on a QuantaStor HA pair."""

    NODES = [
        {"system_id": "node-a", "label": "Left", "host": "192.0.2.30"},
        {"system_id": "node-b", "label": "Right", "host": "192.0.2.31"},
    ]

    def _retention(
        self, disks, slots_by_node, *, other_node=None, configured=True, default_node="node-a", nodes=None,
        pool_devices=(),
    ):
        from app.config import SSHConfig
        from app.models.domain import EnclosureOption, InventorySnapshot

        system = SystemConfig(
            id="qs",
            truenas=TrueNASConfig(host="https://192.0.2.40", platform="quantastor"),
            ssh=SSHConfig(enabled=False, ha_enabled=True, ha_nodes=(nodes or self.NODES) if configured else []),
        )
        options = [EnclosureOption(id="node-a", label="Left"), EnclosureOption(id="node-b", label="Right")]
        systems = [
            {"id": "node-a", "name": "Left", "storageSystemClusterId": "c"},
            {"id": "node-b", "name": "Right", "storageSystemClusterId": "c", "isMaster": True},
        ]
        if other_node:
            # Another hardware-backed storage system in the same grid, in its
            # own cluster; the grid API lists its enclosure as an option.
            options.append(EnclosureOption(id=other_node, label="Other appliance"))
            systems.append({"id": other_node, "name": "Other", "storageSystemClusterId": "other", "isMaster": True})

        def snapshot(node):
            return InventorySnapshot(
                slots=[
                    SlotView(slot=index, slot_label=str(index), row_index=0, column_index=index,
                             present=True, enclosure_id=node, **fields)
                    for index, fields in enumerate(slots_by_node.get(node, []))
                ],
                refresh_interval_seconds=30,
                selected_enclosure_id=node,
                enclosures=options,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            service = build_service(Settings(systems=[system]), system, temp_dir)
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[], disks=disks, pools=[], disk_temperatures={}, smart_test_results=[],
                        systems=systems, pool_devices=list(pool_devices),
                    ),
                    ssh_outputs={}, ssh_collected=False, warnings=[],
                    sources={"api": SourceStatus(enabled=True, ok=True)},
                    scale_ses_data=ParsedSSHData(), quantastor_ses_data=ParsedSSHData(),
                )
            )

            async def get_snapshot(**kwargs):
                return snapshot(kwargs.get("selected_enclosure_id") or default_node)

            async def get_snapshot_result(**kwargs):
                return SimpleNamespace(value=snapshot(kwargs["selected_enclosure_id"]))

            service.get_snapshot = get_snapshot
            service._get_snapshot_result = get_snapshot_result
            return asyncio.run(service.get_system_disk_retention())

    @staticmethod
    def _disk(node, path, serial=None):
        disk = {"id": f"{node}-{path}", "storageSystemId": node, "devicePath": f"/dev/{path}", "size": 1000}
        if serial:
            disk["serialNumber"] = serial
        return disk

    def test_the_same_device_name_on_two_nodes_is_two_disks(self) -> None:
        # Each node has a different disk at /dev/sda; node-b's is shown nowhere.
        retention = self._retention(
            [self._disk("node-a", "sda", "SYNTH-QS-A"), self._disk("node-b", "sda", "SYNTH-QS-B")],
            {"node-a": [{"device_name": "sda", "serial": "SYNTH-QS-A"}]},
        )
        self.assertEqual(retention.source_disk_count, 2)
        self.assertEqual(retention.unplaced_disk_count, 1)

    def test_a_shared_disk_seen_from_both_nodes_is_one_placed_disk(self) -> None:
        retention = self._retention(
            [self._disk("node-a", "sdc", "SYNTH-QS-SHARED"), self._disk("node-b", "sdf", "SYNTH-QS-SHARED")],
            {"node-b": [{"device_name": "sdf", "serial": "SYNTH-QS-SHARED"}]},
        )
        self.assertEqual(retention.unplaced_disk_count, 0)
        self.assertEqual(retention.rendered_unique_disk_count, 1)

    def test_disks_of_a_storage_system_outside_the_pair_are_not_counted(self) -> None:
        # The QuantaStor grid can hold another storage system (here node-c)
        # whose disks this system never shows; they are not its disks.
        retention = self._retention(
            [self._disk("node-a", "sda", "SYNTH-QS-A"), self._disk("node-c", "sdz", "SYNTH-QS-C")],
            {"node-a": [{"device_name": "sda", "serial": "SYNTH-QS-A"}]},
        )
        self.assertEqual(retention.source_disk_count, 1)
        self.assertEqual(retention.unplaced_disk_count, 0)

    def test_another_storage_system_in_the_grid_is_left_out(self) -> None:
        # Its unshown disk must not fail the check, and its enclosures must not
        # count toward the enclosure limit. Found from configured HA nodes or,
        # without them, from the selected node's cluster.
        disks = [self._disk("node-a", "sda", "SYNTH-QS-A"), self._disk("node-x", "sdb", "SYNTH-QS-X")]
        for configured in (True, False):
            with self.subTest(configured=configured), patch.object(
                inventory_module, "SYSTEM_RETENTION_MAX_ENCLOSURES", 2
            ):
                retention = self._retention(
                    disks,
                    {"node-a": [{"device_name": "sda", "serial": "SYNTH-QS-A"}]},
                    other_node="node-x",
                    configured=configured,
                )
                self.assertEqual(retention.source_disk_count, 1)
                self.assertEqual(retention.unplaced_disk_count, 0)

    def test_a_default_page_outside_the_configured_pair_is_left_out(self) -> None:
        # The grid-wide default can land on another storage system; with the
        # pair configured, that system's disks and slots are still not counted.
        retention = self._retention(
            [self._disk("node-a", "sda", "SYNTH-QS-A"), self._disk("node-x", "sdb", "SYNTH-QS-X")],
            {"node-a": [{"device_name": "sda", "serial": "SYNTH-QS-A"}]},
            other_node="node-x",
            default_node="node-x",
        )
        self.assertEqual(retention.source_disk_count, 1)
        self.assertEqual(retention.unplaced_disk_count, 0)

    def test_stale_configured_node_ids_fall_back_to_the_cluster(self) -> None:
        # Configured ids that own no enclosure in the grid (a replaced node)
        # must not empty the pair; the selected node's cluster is used instead.
        stale = [
            {"system_id": "gone-1", "label": "Left", "host": "192.0.2.30"},
            {"system_id": "gone-2", "label": "Right", "host": "192.0.2.31"},
        ]
        retention = self._retention(
            [self._disk("node-a", "sda", "SYNTH-QS-A"), self._disk("node-x", "sdb", "SYNTH-QS-X")],
            {"node-a": [{"device_name": "sda", "serial": "SYNTH-QS-A"}]},
            other_node="node-x",
            nodes=stale,
        )
        self.assertEqual(retention.source_disk_count, 1)
        self.assertEqual(retention.unplaced_disk_count, 0)

    def test_a_device_name_still_matches_within_its_own_node(self) -> None:
        retention = self._retention(
            [self._disk("node-a", "sdb"), self._disk("node-b", "sdb")],
            {"node-a": [{"device_name": "sdb"}], "node-b": [{"device_name": "sdb"}]},
        )
        self.assertEqual(retention.unplaced_disk_count, 0)
        self.assertEqual(retention.rendered_unique_disk_count, 2)

    # #921: an HA pair with encrypted multipath disks. Each shared disk is
    # reported by both nodes as a raw by-id record, a dm-uuid-mpath record and
    # a dm-name-enc (dm-crypt) record; each node also has two boot disks. The
    # pool (owned by node-a) names node-a's crypt device, so on node-b every
    # record of a shared disk also carries node-a's dm-N number, which node-b
    # uses for a different disk's multipath device.
    SHARED_DISKS = 9
    # node-b's dm-N for disk i's multipath device is node-a's crypt dm-N for
    # disk PERMUTATION[i]: one three-disk cycle and three swapped pairs.
    PERMUTATION = (1, 2, 0, 4, 3, 6, 5, 8, 7)

    @classmethod
    def _dm_shaped_pair(cls):
        disks, pool_devices = [], []
        dm_numbers = {
            "node-a": {"mpath": lambda i: 3 + i, "crypt": lambda i: 12 + i},
            "node-b": {"mpath": lambda i: 12 + cls.PERMUTATION[i], "crypt": lambda i: 3 + i},
        }
        for i in range(cls.SHARED_DISKS):
            serial = f"TESTSER{i + 1:04d}"
            lun = f"35000c5000000{i + 1:04d}"
            by_id = f"scsi-SSYNTH_MODEL_{serial}"
            for node, numbers in dm_numbers.items():
                common = {"storageSystemId": node, "serialNumber": serial, "scsiId": lun, "size": 1000}
                sd = f"sd{chr(ord('c') + i)}"
                disks += [
                    common | {"id": f"{node}-raw-{i}", "devicePath": f"/dev/disk/by-id/{by_id}",
                              "altDevicePath": f"/dev/{sd}", "name": f"{sd} ({by_id})"},
                    common | {"id": f"{node}-mpath-{i}", "devicePath": f"/dev/disk/by-id/dm-uuid-mpath-{lun}",
                              "altDevicePath": f"/dev/dm-{numbers['mpath'](i)}",
                              "name": f"dm-uuid-mpath-{lun}"},
                    common | {"id": f"{node}-crypt-{i}",
                              "devicePath": f"/dev/disk/by-id/dm-name-enc-dm-uuid-mpath-{lun}",
                              "altDevicePath": f"/dev/dm-{numbers['crypt'](i)}",
                              "name": f"dm-name-enc-dm-uuid-mpath-{lun}"},
                ]
            crypt_path = f"/dev/disk/by-id/dm-name-enc-dm-uuid-mpath-{lun}"
            pool_devices.append({
                "id": f"pool-device-{i}", "storageSystemId": "node-a", "slot": i,
                "name": crypt_path.rsplit("/", 1)[-1], "devicePath": crypt_path,
                "physicalDiskSerialNumber": serial, "physicalDiskScsiId": lun,
                "physicalDiskObj": {"name": crypt_path.rsplit("/", 1)[-1], "devicePath": crypt_path,
                                    "altDevicePath": f"/dev/dm-{12 + i}", "serialNumber": serial,
                                    "scsiId": lun},
            })
        for node, boot_serials in (("node-a", ("TESTSER0101", "TESTSER0102")),
                                   ("node-b", ("TESTSER0201", "TESTSER0202"))):
            for k, serial in enumerate(boot_serials):
                disks.append({
                    "id": f"{node}-boot-{k}", "storageSystemId": node, "serialNumber": serial, "size": 100,
                    "devicePath": f"/dev/disk/by-id/ata-SYNTH_SATADOM_{serial}", "altDevicePath": f"/dev/sd{'ab'[k]}",
                })
        return disks, pool_devices

    def _dm_shaped_slots(self, disks, pool_devices):
        """Each node's bays and boot views, built from that node's raw by-id records."""
        system = SystemConfig(id="qs", truenas=TrueNASConfig(platform="quantastor"))
        raw = TrueNASRawData(enclosures=[], disks=disks, pools=[], disk_temperatures={}, smart_test_results=[],
                             pool_devices=pool_devices)
        slots_by_node = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            service = build_service(Settings(systems=[system]), system, temp_dir)
            for node in ("node-a", "node-b"):
                slots_by_node[node] = [
                    {"device_name": record.device_name, "serial": record.serial,
                     "logical_unit_id": record.lunid, "gptid": record.identifier,
                     "smart_device_names": list(record.smart_devices)}
                    for record in service._build_quantastor_disk_records(raw, node)
                    if record.raw.get("storageSystemId") == node
                    and not str(record.device_name).startswith("disk/by-id/dm-")
                ]
        return slots_by_node

    def test_dm_n_aliases_do_not_merge_distinct_disks_of_an_encrypted_multipath_pair(self) -> None:
        disks, pool_devices = self._dm_shaped_pair()
        slots_by_node = self._dm_shaped_slots(disks, pool_devices)
        self.assertEqual(len(disks), 58)
        self.assertEqual(len({disk["serialNumber"] for disk in disks}), 13)
        self.assertEqual([len(slots) for slots in slots_by_node.values()], [11, 11])
        # The fixture reproduces the collision: on node-b one dm-N name is
        # held by records of two different disks.
        holders: dict[str, set[str | None]] = {}
        system = SystemConfig(id="qs", truenas=TrueNASConfig(platform="quantastor"))
        raw = TrueNASRawData(enclosures=[], disks=disks, pools=[], disk_temperatures={}, smart_test_results=[],
                             pool_devices=pool_devices)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = build_service(Settings(systems=[system]), system, temp_dir)
            for record in service._build_quantastor_disk_records(raw, "node-b"):
                for name in record.smart_devices:
                    if name.startswith("dm-") and name[3:].isdigit():
                        holders.setdefault(name, set()).add(record.serial)
        self.assertTrue(any(len(serials) > 1 for serials in holders.values()))

        retention = self._retention(disks, slots_by_node, pool_devices=pool_devices)

        self.assertEqual(
            (
                retention.source_disk_count,
                retention.rendered_unique_disk_count,
                retention.duplicate_disk_view_count,
                retention.unplaced_disk_count,
            ),
            (58, 13, 9, 0),
        )

    def test_a_missing_disk_of_the_pair_is_not_hidden_by_a_dm_n_alias(self) -> None:
        # Neither node shows TESTSER0002 any more. Before #921 its records
        # shared a node-b dm-N name with a shown disk and counted as placed.
        disks, pool_devices = self._dm_shaped_pair()
        slots_by_node = {
            node: [slot for slot in slots if slot["serial"] != "TESTSER0002"]
            for node, slots in self._dm_shaped_slots(disks, pool_devices).items()
        }

        retention = self._retention(disks, slots_by_node, pool_devices=pool_devices)

        # Six records describe the missing disk: raw, mpath and crypt per node.
        self.assertEqual(retention.unplaced_disk_count, 6)
        self.assertEqual(retention.rendered_unique_disk_count, 12)


class IdentityConflictTests(unittest.TestCase):
    """#921: a shared node-local name never joins two different stable identities."""

    @staticmethod
    def _record(**fields):
        return SimpleNamespace(**fields)

    @staticmethod
    def _slot(**fields) -> SlotView:
        return SlotView(**({"slot": 0, "slot_label": "0", "row_index": 0, "column_index": 0} | fields))

    def test_bare_device_mapper_names_are_not_identity(self) -> None:
        self.assertEqual(
            logical_disk_identity_tokens(device_names=("dm-16", "/dev/dm-3", "DM-7"), gptid="/dev/dm-4"),
            frozenset(),
        )
        # Stable device-mapper names keep working.
        self.assertEqual(
            logical_disk_identity_tokens(device_names=("/dev/disk/by-id/dm-uuid-mpath-35000c50000000001",)),
            frozenset({"dev:disk/by-id/dm-uuid-mpath-35000c50000000001"}),
        )

    def test_a_shared_device_name_does_not_join_two_serials(self) -> None:
        sources = [
            self._record(serial="TESTSER0001", device_name="sdc"),
            self._record(serial="TESTSER0002", device_name="sdc"),
        ]
        slots = [self._slot(serial="TESTSER0001", device_name="sdc")]
        for scopes in (None, ["node-a", "node-a"]):
            with self.subTest(scopes=scopes):
                accounting = build_disk_retention_accounting(
                    source_disks=sources, slots=slots, source_scopes=scopes,
                    slot_scopes=None if scopes is None else ["node-a"],
                )
                self.assertEqual(accounting.rendered_unique_disk_count, 1)
                self.assertEqual(accounting.unplaced_disk_count, 1)

    def test_a_record_without_a_serial_cannot_bridge_two_serials(self) -> None:
        sources = [
            self._record(serial="TESTSER0001", device_name="sdc"),
            self._record(serial="TESTSER0002", device_name="sdd"),
            # One record naming both devices, with no identity of its own.
            self._record(device_name="sdc", path_device_name="sdd"),
        ]
        for order in (sources, sources[::-1]):
            with self.subTest(reversed=order is not sources):
                accounting = build_disk_retention_accounting(
                    source_disks=order,
                    slots=[self._slot(serial="TESTSER0001", device_name="sdc")],
                )
                self.assertEqual(accounting.rendered_unique_disk_count, 1)
                # TESTSER0002 is shown nowhere, and the bridging record
                # cannot be credited to either disk: both stay unplaced.
                self.assertEqual(accounting.unplaced_disk_count, 2)

    def test_a_shared_device_name_does_not_join_two_lun_ids(self) -> None:
        sources = [
            self._record(lunid="5000c50000000001", device_name="dm-name-a"),
            self._record(lunid="5000c50000000002", device_name="dm-name-a"),
        ]
        accounting = build_disk_retention_accounting(
            source_disks=sources, slots=[self._slot(logical_unit_id="5000c50000000001")],
        )
        self.assertEqual(accounting.rendered_unique_disk_count, 1)
        self.assertEqual(accounting.unplaced_disk_count, 1)

    def test_a_lun_id_reported_with_two_serials_identifies_neither(self) -> None:
        # A RAID volume id carried by both member disks is not a disk identity.
        sources = [
            self._record(serial="TESTSER0001", lunid="5000c50000000009", device_name="sdc"),
            self._record(serial="TESTSER0002", lunid="5000c50000000009", device_name="sdd"),
        ]
        accounting = build_disk_retention_accounting(
            source_disks=sources,
            slots=[self._slot(serial="TESTSER0001", logical_unit_id="5000c50000000009", device_name="sdc")],
        )
        self.assertEqual(accounting.rendered_unique_disk_count, 1)
        self.assertEqual(accounting.unplaced_disk_count, 1)

    def test_one_serial_reported_with_two_lun_formats_is_one_disk(self) -> None:
        sources = [
            self._record(serial="TESTSER0001", lunid="5000c50000000001", device_name="da0"),
            self._record(serial="TESTSER0001", lunid="naa.5000c50000000001", device_name="da1"),
        ]
        accounting = build_disk_retention_accounting(
            source_disks=sources, slots=[self._slot(serial="TESTSER0001", device_name="da0")],
        )
        self.assertEqual(accounting.rendered_unique_disk_count, 1)
        self.assertEqual(accounting.unplaced_disk_count, 0)

    def test_core_multipath_paths_still_join_without_a_serial_on_every_path(self) -> None:
        sources = [
            self._record(serial="TESTSER0001", device_name="multipath/disk1", path_device_name="da3",
                         multipath_name="multipath/disk1", multipath_member="da3"),
            self._record(device_name="da4", multipath_name="multipath/disk1", multipath_member="da4"),
        ]
        slots = [
            self._slot(
                serial="TESTSER0001",
                device_name="multipath/disk1",
                multipath=MultipathView(
                    name="disk1",
                    device_name="multipath/disk1",
                    members=[MultipathMember(device_name="da3"), MultipathMember(device_name="da4")],
                ),
            ),
            self._slot(slot=1, device_name="da4"),
        ]
        accounting = build_disk_retention_accounting(source_disks=sources, slots=slots)
        self.assertEqual(
            (accounting.rendered_unique_disk_count, accounting.duplicate_disk_view_count,
             accounting.unplaced_disk_count),
            (1, 1, 0),
        )


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

    def test_a_second_concurrent_system_check_is_refused_as_busy(self) -> None:
        # One system-wide check per system at a time; a caller that repeats
        # forced requests cannot stack enclosure builds on the worker.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[self.BOOT_VIEW])

            async def run():
                release = asyncio.Event()
                original = service.get_storage_view_runtime

                async def held(**kwargs):
                    await release.wait()
                    return await original(**kwargs)

                service.get_storage_view_runtime = held
                first = asyncio.create_task(service.get_system_disk_retention(force_refresh=True))
                await asyncio.sleep(0)
                with self.assertRaises(inventory_module.SystemRetentionBusyError):
                    await service.get_system_disk_retention(force_refresh=True)
                release.set()
                return await first

            self.assertEqual(asyncio.run(run()).unplaced_disk_count, 0)

    def test_a_refused_forced_request_collects_nothing(self) -> None:
        # The route's own forced snapshot is admitted together with the
        # system totals, so a request turned away as busy never refreshes.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, source_bundle = self._service(temp_dir, storage_views=[self.BOOT_VIEW])

            async def run():
                release = asyncio.Event()
                original = service.get_storage_view_runtime

                async def held(**kwargs):
                    await release.wait()
                    return await original(**kwargs)

                service.get_storage_view_runtime = held
                first = asyncio.create_task(service.get_snapshot_with_system_retention(force_refresh=True))
                await asyncio.sleep(0)
                forced_before = sum(1 for call in source_bundle.await_args_list if call.kwargs.get("force_refresh"))
                with self.assertRaises(inventory_module.SystemRetentionBusyError):
                    await service.get_snapshot_with_system_retention(force_refresh=True)
                forced_after = sum(1 for call in source_bundle.await_args_list if call.kwargs.get("force_refresh"))
                release.set()
                snapshot, retention = await first
                return forced_before, forced_after, snapshot, retention

            forced_before, forced_after, snapshot, retention = asyncio.run(run())

        self.assertEqual(forced_after, forced_before)
        self.assertEqual(snapshot.selected_enclosure_id, "enc-a")
        self.assertEqual(retention.unplaced_disk_count, 0)

    def test_a_forced_request_for_another_enclosure_collects_sources_once(self) -> None:
        # A cold service also collects once to discover its enclosures; warm it
        # so this measures only the forced request itself.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, source_bundle = self._service(temp_dir, storage_views=[self.BOOT_VIEW])

            async def run():
                await service.get_snapshot()
                source_bundle.reset_mock()
                return await service.get_snapshot_with_system_retention(
                    force_refresh=True, selected_enclosure_id="enc-b",
                )

            snapshot, retention = asyncio.run(run())

        forced = [call for call in source_bundle.await_args_list if call.kwargs.get("force_refresh")]
        self.assertEqual(len(forced), 1)
        self.assertEqual(snapshot.selected_enclosure_id, "enc-b")
        self.assertEqual(retention.unplaced_disk_count, 0)

    def test_too_many_enclosures_are_refused_before_building_them(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(inventory_module, "SYSTEM_RETENTION_MAX_ENCLOSURES", 1),
        ):
            service, _ = self._service(temp_dir, storage_views=[self.BOOT_VIEW])
            original = service._get_snapshot_result
            built: list[str | None] = []

            async def recording(*args, **kwargs):
                built.append(kwargs.get("selected_enclosure_id"))
                return await original(*args, **kwargs)

            service._get_snapshot_result = recording
            with self.assertRaises(inventory_module.SystemRetentionTooLargeError):
                asyncio.run(service.get_system_disk_retention())

        self.assertNotIn("enc-b", built)

    def test_a_disabled_storage_view_does_not_place_its_disk(self) -> None:
        # A view switched off in the admin UI renders nothing, so its disk
        # must not satisfy the upgrade check.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[{**self.BOOT_VIEW, "enabled": False}])
            retention = asyncio.run(service.get_system_disk_retention())

        self.assertEqual(retention.unplaced_disk_count, 1)

    def test_a_view_hidden_from_the_main_page_does_not_place_its_disk(self) -> None:
        # The main page lists only enabled views shown in the main UI; a
        # maintenance-only view is not part of the inventory an operator sees.
        hidden = {**self.BOOT_VIEW, "render": {"show_in_main_ui": False}}
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[hidden])
            retention = asyncio.run(service.get_system_disk_retention())

        self.assertEqual(retention.unplaced_disk_count, 1)

    def test_an_excluded_view_with_a_stale_target_does_not_break_the_check(self) -> None:
        # A QuantaStor view kept disabled or hidden after its HA node was
        # removed still names that node. It is not operator-visible, so the
        # check must not resolve its target and fail with UnknownEnclosureError.
        stale = {
            "id": "stale-node-boot",
            "label": "Removed node boot",
            "kind": "boot_devices",
            "template_id": "satadom-pair-2",
            "binding": {"mode": "serial", "device_names": ["da2"]},
        }
        for excluded in ({"enabled": False}, {"render": {"show_in_main_ui": False}}):
            with self.subTest(excluded=excluded), tempfile.TemporaryDirectory() as temp_dir:
                service, _ = self._service(temp_dir, storage_views=[self.BOOT_VIEW, {**stale, **excluded}])
                original = service._storage_view_target_system_id

                def target(storage_view, snapshot, *, original=original):
                    if storage_view.id == "stale-node-boot":
                        return "removed-node"
                    return original(storage_view, snapshot)

                service._storage_view_target_system_id = target
                retention = asyncio.run(service.get_system_disk_retention())

                self.assertEqual(retention.unplaced_disk_count, 0)

        # A visible view with the same stale target still fails the check.
        with tempfile.TemporaryDirectory() as temp_dir:
            service, _ = self._service(temp_dir, storage_views=[self.BOOT_VIEW, stale])
            original = service._storage_view_target_system_id
            service._storage_view_target_system_id = (
                lambda storage_view, snapshot: "removed-node"
                if storage_view.id == "stale-node-boot"
                else original(storage_view, snapshot)
            )
            with self.assertRaises(inventory_module.UnknownEnclosureError):
                asyncio.run(service.get_system_disk_retention())

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
