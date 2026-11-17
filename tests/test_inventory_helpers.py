from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

from tests.mapping_fixtures import write_v1_mappings
from tests.inventory_fixtures import build_inventory_service
from app.config import (
    BMCConfig,
    SSHConfig,
    Settings,
    StorageViewBindingConfig,
    StorageViewConfig,
    SystemConfig,
    TrueNASConfig,
)
from app.models.domain import (
    EnclosureOption,
    EnclosureProfileView,
    InventorySummary,
    InventorySnapshot,
    ManualMapping,
    SlotState,
    SlotView,
    SourceStatus,
)
from app.services import inventory as inventory_module
from app.services.inventory import (
    DiskRecord,
    InventoryService,
    InventorySourceBundle,
    build_lunid_alias_tiers,
    build_lunid_aliases,
    effective_main_view_order,
    index_disks_by_sas,
    infer_slot_count_from_layout,
    lunid_alias_tier_sets,
)
from app.services.mapping_store import MappingStore
from app.services.parsers import (
    ParsedSSHData,
    SESMapEnclosure,
    SESMapSlot,
    build_slot_candidates_from_ses_enclosures,
    canonicalize_ssh_command,
)
from app.services.storage_views import storage_view_slot_label
from app.services.profile_registry import ProfileRegistry
from app.services.profile_registry import (
    ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
    SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
    SUPERMICRO_FATTWIN_REAR_2_PROFILE_ID,
)
from app.services.ssh_probe import SSHCommandResult, SSHLocalFailure
from app.services.storage_views import resolve_system_storage_views
from app.services.storage_view_templates import get_storage_view_template, list_storage_view_templates
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData


class InventoryHelpersTests(unittest.TestCase):
    def test_effective_main_view_order_uses_disk_counts_and_tie_rules(self) -> None:
        order = effective_main_view_order(
            enclosure_counts=[("enc-a", 4), ("enc-b", 9), ("enc-c", 4)],
            view_counts=[("primary-chassis", 9), ("boot", 2), ("nvme", 4)],
        )

        self.assertEqual(
            order,
            [
                "enclosure:enc-b",
                "view:primary-chassis",
                "enclosure:enc-a",
                "enclosure:enc-c",
                "view:nvme",
                "view:boot",
            ],
        )

    def test_effective_main_view_order_applies_partial_saved_order_and_skips_stale_entries(self) -> None:
        order = effective_main_view_order(
            enclosure_counts=[("enc-a", 4), ("enc-b", 9)],
            view_counts=[("boot", 2), ("nvme", 4)],
            saved_order=["view:boot", "enclosure:retired", "view:boot", "enclosure:enc-a"],
        )

        self.assertEqual(
            order,
            ["view:boot", "enclosure:enc-a", "enclosure:enc-b", "view:nvme"],
        )

    def test_effective_main_view_order_prefers_primary_chassis_only_for_automatic_ha_order(self) -> None:
        automatic = effective_main_view_order(
            enclosure_counts=[("node-a", 9), ("node-b", 9)],
            view_counts=[("primary-chassis", 9), ("boot", 2)],
            ha_primary_view_id="primary-chassis",
            ha_owner_enclosure_id="node-b",
        )
        explicit = effective_main_view_order(
            enclosure_counts=[("node-a", 9), ("node-b", 9)],
            view_counts=[("primary-chassis", 9), ("boot", 2)],
            saved_order=["enclosure:node-a"],
            ha_primary_view_id="primary-chassis",
            ha_owner_enclosure_id="node-b",
        )
        without_primary = effective_main_view_order(
            enclosure_counts=[("node-a", 9), ("node-b", 9)],
            view_counts=[("boot", 2)],
            ha_owner_enclosure_id="node-b",
        )

        self.assertEqual(automatic[0], "view:primary-chassis")
        self.assertEqual(explicit[0], "enclosure:node-a")
        self.assertEqual(without_primary[0], "enclosure:node-b")

    def test_view_order_is_display_only_for_inventory_cache_adoption(self) -> None:
        original = SystemConfig(id="example", label="Example", view_order=["enclosure:enc-a"])
        reordered = original.model_copy(update={"view_order": ["view:primary-chassis"]})

        self.assertEqual(
            inventory_module._system_without_display_fields(original),
            inventory_module._system_without_display_fields(reordered),
        )

    def test_quantastor_floating_alias_is_never_a_node_host_but_bonds_and_vlans_are(self) -> None:
        extract = InventoryService._extract_quantastor_gateway_port_host
        port = {"name": "eno1", "ipAddress": "192.0.2.31", "gateway": "192.0.2.1"}
        self.assertEqual(extract(port), "192.0.2.31")
        # QuantaStor marks the grid VIP and HA addresses as alias interfaces.
        floating = {**port, "name": "eno1:gm", "ipAddress": "192.0.2.40", "isVirtualInterface": True, "isVirtualPort": True}
        self.assertIsNone(extract(floating))
        # isVirtualPort alone marks a bond or VLAN, which stays on its node.
        for name, kind in (("bond0", "isBond"), ("bond0.1337", "isVlan")):
            with self.subTest(port=name):
                routed = {**port, "name": name, "isVirtualInterface": False, "isVirtualPort": True, kind: True}
                self.assertEqual(extract(routed), "192.0.2.31")

    def test_first_ses_overlay_preserves_secondary_path_unplaced_binding_warning(self) -> None:
        overlay = ParsedSSHData(
            ses_enclosures=[
                SESMapEnclosure(
                    ses_device="/dev/sg84",
                    ses_devices=["/dev/sg84", "/dev/sg85"],
                    enclosure_id="enc-a",
                    slots={0: SESMapSlot(slot_number=0, slot_number_source="ses_device_slot_number")},
                    unplaced_sysfs_bindings_by_ses_device={"/dev/sg85": 2},
                )
            ]
        )

        merged = InventoryService._merge_ses_overlay_data(ParsedSSHData(), overlay)
        _, selected = build_slot_candidates_from_ses_enclosures(
            merged.ses_enclosures,
            1,
            None,
            enclosures_are_merged=True,
        )

        self.assertEqual(
            merged.ses_enclosures[0].unplaced_sysfs_bindings_by_ses_device,
            {"/dev/sg85": 2},
        )
        self.assertEqual(
            [warning for warning in selected["warnings"] if "Kernel enclosure bindings" in warning],
            [
                "Kernel enclosure bindings for /dev/sg85 could not be placed: "
                "SES reported no device slot numbers for 2 bound devices."
            ],
        )

    def test_subsequent_ses_overlay_combines_unplaced_binding_counts_per_path(self) -> None:
        base = ParsedSSHData(
            ses_enclosures=[
                SESMapEnclosure(
                    ses_device="/dev/sg84",
                    enclosure_id="enc-a",
                    slots={0: SESMapSlot(slot_number=0, slot_number_source="ses_device_slot_number")},
                    unplaced_sysfs_bindings_by_ses_device={"/dev/sg84": 1},
                )
            ]
        )
        overlay = ParsedSSHData(
            ses_enclosures=[
                SESMapEnclosure(
                    ses_device="/dev/sg85",
                    enclosure_id="enc-a",
                    slots={0: SESMapSlot(slot_number=0, slot_number_source="ses_device_slot_number")},
                    unplaced_sysfs_bindings_by_ses_device={
                        "/dev/sg84": 2,
                        "/dev/sg85": 3,
                    },
                )
            ]
        )

        merged = InventoryService._merge_ses_overlay_data(base, overlay)
        _, selected = build_slot_candidates_from_ses_enclosures(
            merged.ses_enclosures,
            1,
            None,
            enclosures_are_merged=True,
        )

        self.assertEqual(
            merged.ses_enclosures[0].unplaced_sysfs_bindings_by_ses_device,
            {"/dev/sg84": 3, "/dev/sg85": 3},
        )
        self.assertEqual(
            [warning for warning in selected["warnings"] if "Kernel enclosure bindings" in warning],
            [
                "Kernel enclosure bindings for /dev/sg84 could not be placed: "
                "SES reported no device slot numbers for 3 bound devices.",
                "Kernel enclosure bindings for /dev/sg85 could not be placed: "
                "SES reported no device slot numbers for 3 bound devices.",
            ],
        )

    def test_all_platform_correlators_route_through_shared_scaffolding(self) -> None:
        self.assertTrue(hasattr(inventory_module, "_LayoutFrame"))
        self.assertTrue(hasattr(InventoryService, "_resolve_layout_frame"))
        self.assertTrue(hasattr(inventory_module, "_slot_grid_position"))
        self.assertTrue(hasattr(inventory_module, "_warn_unmatched_mapping"))

        correlators = (
            "_correlate",
            "_correlate_linux_host",
            "_correlate_esxi_host",
            "_correlate_bmc_host",
            "_correlate_scale_linux",
            "_correlate_quantastor",
        )
        for method_name in correlators:
            with self.subTest(method=method_name):
                source = inspect.getsource(getattr(InventoryService, method_name))
                self.assertIn("self._resolve_layout_frame(", source)
                self.assertIn("_slot_grid_position(", source)
                self.assertIn("_warn_unmatched_mapping(", source)
                self.assertIn("frame.result(", source)

        for method_name in ("_correlate", "_correlate_scale_linux", "_correlate_esxi_host"):
            with self.subTest(shared_disk_index=method_name):
                source = inspect.getsource(getattr(InventoryService, method_name))
                self.assertIn("_index_disk_records(", source)

        for method_name in (
            "_correlate_linux_host",
            "_correlate_bmc_host",
            "_correlate_quantastor",
        ):
            with self.subTest(bespoke_disk_index=method_name):
                source = inspect.getsource(getattr(InventoryService, method_name))
                self.assertNotIn("_index_disk_records(", source)

    def test_resolve_layout_frame_copies_layout_and_builds_unchanged_result_tuple(self) -> None:
        service = object.__new__(InventoryService)
        service.settings = Settings(systems=[SystemConfig(id="system-a")])
        service.system = service.settings.systems[0]
        selected_option = EnclosureOption(
            id="enc-a",
            label="Enclosure A",
            name="Shelf A",
            rows=2,
            columns=2,
            slot_count=3,
            slot_layout=[[2, None], [0, 1]],
        )
        selected_profile = EnclosureProfileView(
            id="profile-a",
            label="Profile A",
            rows=2,
            columns=2,
            slot_count=3,
            slot_layout=[[2, None], [0, 1]],
        )
        service.profile_registry = MagicMock()
        service.profile_registry.resolve_for_enclosure.return_value = selected_profile
        service.mapping_store = MagicMock()
        service.mapping_store.load_all.return_value = {}

        warnings: list[str] = []
        frame = service._resolve_layout_frame(
            [selected_option],
            "enc-a",
            warnings,
        )

        self.assertIsInstance(frame, inventory_module._LayoutFrame)
        self.assertIs(frame.selected_option, selected_option)
        self.assertIs(frame.selected_profile, selected_profile)
        self.assertEqual(frame.selected_meta, {"id": "enc-a", "label": "Enclosure A", "name": "Shelf A"})
        self.assertEqual(frame.layout_rows, [[2, None], [0, 1]])
        self.assertIsNot(frame.layout_rows, selected_profile.slot_layout)
        self.assertEqual(frame.layout_slot_count, 3)
        self.assertEqual(frame.layout_columns, 2)
        self.assertEqual(frame.slot_positions, {2: (0, 0), 0: (1, 0), 1: (1, 1)})
        self.assertEqual(inventory_module._slot_grid_position(2, frame.slot_positions, 2), (0, 0))
        self.assertEqual(inventory_module._slot_grid_position(3, frame.slot_positions, 2), (1, 1))
        self.assertEqual(
            frame.result([]),
            ([], [selected_option], frame.selected_meta, [[2, None], [0, 1]], 3, 2),
        )
        self.assertEqual(warnings, [])

    def test_unmatched_mapping_helper_preserves_all_platform_warning_text(self) -> None:
        expected_by_platform = {
            "core": "Manual mapping for slot 03 did not match any current disk.",
            "scale": "Manual mapping for slot 03 did not match any current disk.",
            "linux": "Manual mapping for slot 03 did not match any current Linux device.",
            "esxi": "Manual mapping for slot 03 did not match any current ESXi StorCLI member.",
            "ipmi": "Manual mapping for slot 03 did not match any current BMC disk.",
            "quantastor": "Manual mapping for slot 03 did not match any current Quantastor disk.",
        }
        target_by_platform = {
            "core": "disk",
            "scale": "disk",
            "linux": "Linux device",
            "esxi": "ESXi StorCLI member",
            "ipmi": "BMC disk",
            "quantastor": "Quantastor disk",
        }

        for platform, expected in expected_by_platform.items():
            with self.subTest(platform=platform):
                warnings: list[str] = []
                inventory_module._warn_unmatched_mapping(
                    warnings,
                    MagicMock(),
                    None,
                    3,
                    target_by_platform[platform],
                )
                self.assertEqual(warnings, [expected])

        warnings = []
        inventory_module._warn_unmatched_mapping(warnings, None, None, 3, "disk")
        inventory_module._warn_unmatched_mapping(warnings, MagicMock(), MagicMock(), 3, "disk")
        self.assertEqual(warnings, [])

    def test_index_disk_records_matches_core_and_scale_lookup_shapes(self) -> None:
        disk = self._make_disk_record("sda", "5000c5003e8253a7")
        disk.enclosure_id = "enc-a"
        disk.slot = 2
        disk.lookup_keys = {"sda", "serial-a"}

        for platform in ("core", "scale"):
            with self.subTest(platform=platform):
                disks_by_key, disks_by_slot, disks_by_sas = inventory_module._index_disk_records(
                    [disk],
                    platform,
                )
                self.assertIs(disks_by_key["sda"], disk)
                self.assertIs(disks_by_key["serial-a"], disk)
                self.assertIs(disks_by_slot[("enc-a", 2)], disk)
                self.assertIs(disks_by_slot[(None, 2)], disk)
                self.assertIs(disks_by_sas["5000c5003e8253a7"], disk)

    def test_legacy_mapping_fallback_requires_one_system_and_one_enclosure(self) -> None:
        system_a = SystemConfig(id="system-a")
        service = object.__new__(InventoryService)
        service.settings = Settings(systems=[system_a])
        one_enclosure = [EnclosureOption(id="enc-a", label="Enclosure A")]

        self.assertTrue(service._legacy_mapping_fallback_allowed(one_enclosure))

        service.settings = Settings(
            systems=[system_a, SystemConfig(id="system-b")]
        )
        self.assertFalse(service._legacy_mapping_fallback_allowed(one_enclosure))

        service.settings = Settings(systems=[system_a])
        self.assertFalse(
            service._legacy_mapping_fallback_allowed([
                *one_enclosure,
                EnclosureOption(id="enc-b", label="Enclosure B"),
            ])
        )
        self.assertTrue(
            service._legacy_mapping_fallback_allowed([
                EnclosureOption(
                    id="enc-a::dell-md1280-drawer-top-42",
                    label="Drawer A",
                ),
                EnclosureOption(
                    id="enc-a::dell-md1280-drawer-bottom-42",
                    label="Drawer B",
                ),
            ])
        )
        self.assertFalse(service._legacy_mapping_fallback_allowed([]))

    def _legacy_fallback_service(
        self,
        temp_dir: str,
        systems: list[SystemConfig],
    ) -> InventoryService:
        service = object.__new__(InventoryService)
        service.settings = Settings(systems=systems)
        service.system = service.settings.systems[0]
        service.mapping_store = MappingStore(str(Path(temp_dir) / "slot_mappings.json"))
        service.profile_registry = MagicMock()
        service.profile_registry.resolve_for_enclosure.return_value = None
        return service

    def test_single_system_without_detected_enclosures_denies_legacy_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._legacy_fallback_service(temp_dir, [SystemConfig(id="system-a")])
            write_v1_mappings(service.mapping_store, {
                "default:5": ManualMapping(slot=5, serial="SYNTH-LEGACY-5"),
            })

            warnings: list[str] = []
            frame = service._resolve_layout_frame(
                [],
                None,
                warnings,
                require_selected_option=False,
                require_profile=False,
            )

            self.assertIsInstance(frame, inventory_module._LayoutFrame)
            self.assertFalse(frame.allow_legacy_mapping_fallback)
            resolved = service.mapping_store.get_mapping(
                "system-a",
                None,
                5,
                allow_legacy_fallback=frame.allow_legacy_mapping_fallback,
            )
            self.assertIsNone(resolved)
            self.assertEqual(len(warnings), 1)
            self.assertIn("no known enclosure", warnings[0])
            self.assertIn("save the assignment again", warnings[0])

    def test_zero_enclosure_disks_use_system_virtual_inventory_without_legacy_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="system-a",
                default_profile_id="supermicro-cse-946-top-60",
                truenas=TrueNASConfig(platform="core"),
            )
            settings = Settings(systems=[system])
            settings.layout.rows = 2
            settings.layout.columns = 4
            settings.layout.slot_count = 8
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            write_v1_mappings(service.mapping_store, {
                "default:0": ManualMapping(slot=0, serial="SYNTH-DISK-B"),
            })
            raw_data = TrueNASRawData(
                enclosures=[],
                disks=[
                    {
                        "name": "da0",
                        "serial": "SYNTH-DISK-A",
                        "model": "Synthetic A",
                        "size": 1_000_000_000,
                        "status": "ONLINE",
                    },
                    {
                        "name": "da1",
                        "serial": "SYNTH-DISK-B",
                        "model": "Synthetic B",
                        "size": 2_000_000_000,
                        "status": "ONLINE",
                    },
                ],
                pools=[],
                disk_temperatures={"da0": 30, "da1": 31},
                smart_test_results=[],
            )
            warnings: list[str] = []

            slots, enclosures, selected_meta, rows, slot_count, columns = service._correlate(
                raw_data,
                ParsedSSHData(),
                warnings,
            )

            self.assertEqual(len(enclosures), 1)
            virtual_enclosure = enclosures[0]
            self.assertEqual(virtual_enclosure.id, "virtual-system:system-a")
            self.assertEqual(virtual_enclosure.label, "System disk inventory (virtual)")
            self.assertEqual(virtual_enclosure.kind, "virtual")
            self.assertEqual((virtual_enclosure.rows, virtual_enclosure.columns), (1, 2))
            self.assertEqual(virtual_enclosure.slot_count, 2)
            self.assertEqual(selected_meta["id"], virtual_enclosure.id)
            self.assertEqual(rows, [[0, 1]])
            self.assertEqual((slot_count, columns), (2, 2))
            self.assertEqual([slot.device_name for slot in slots], ["da0", "da1"])
            self.assertEqual([slot.slot_label for slot in slots], ["Disk 1", "Disk 2"])
            self.assertTrue(all(slot.enclosure_id == virtual_enclosure.id for slot in slots))
            self.assertTrue(all(slot.mapping_source == "system-inventory" for slot in slots))
            self.assertTrue(all(slot.raw_status.get("virtual_enclosure") is True for slot in slots))
            self.assertTrue(all(slot.raw_status.get("physical_location_known") is False for slot in slots))
            self.assertTrue(all(slot.physical_location_known is False for slot in slots))
            self.assertTrue(all(slot.led_supported is False for slot in slots))
            self.assertTrue(all(slot.mapping_supported is False for slot in slots))
            self.assertTrue(all("no known enclosure" in slot.mapping_reason for slot in slots))
            # The virtual fallback states that physical location is unavailable
            # separately from the legacy-mapping warning, and neither one claims
            # a source fetch failure.
            self.assertEqual(len(warnings), 2)
            self.assertEqual(warnings[0], inventory_module.VIRTUAL_INVENTORY_PHYSICAL_LOCATION_WARNING)
            self.assertIn("1 saved bay assignment", warnings[1])
            self.assertIn("no known enclosure", warnings[1])
            self.assertIn("shown without bay positions", warnings[1])
            self.assertIn("save the assignment again", warnings[1])
            self.assertNotIn("SYNTH", warnings[1])
            self.assertNotIn("system-a", warnings[1])

            capabilities = service._build_platform_capabilities(
                slots=slots,
                available_enclosures=enclosures,
                summary=InventorySummary(
                    disk_count=2,
                    enclosure_count=0,
                    mapped_slot_count=0,
                ),
                sources={"api": SourceStatus(enabled=True, ok=True)},
                platform_context={},
            )
            self.assertEqual(capabilities["inventory"].status, "available")
            self.assertEqual(capabilities["physical_slots"].status, "unavailable")

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
            snapshot = asyncio.run(service._build_snapshot())
            self.assertEqual(snapshot.summary.disk_count, 2)
            self.assertEqual(snapshot.summary.enclosure_count, 0)
            self.assertEqual(snapshot.summary.mapped_slot_count, 0)
            self.assertEqual(snapshot.capabilities["physical_slots"].status, "unavailable")
            self.assertIsNone(snapshot.selected_profile)
            self.assertEqual(snapshot.layout_rows, [[0, 1]])
            self.assertEqual(snapshot.layout_slot_count, 2)

    def test_identity_free_rows_do_not_consume_virtual_inventory_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            raw_data = TrueNASRawData(
                enclosures=[],
                disks=[
                    {},
                    {"model": "metadata only", "status": "ONLINE"},
                    {"name": "da0", "model": "Synthetic disk", "status": "ONLINE"},
                ],
                pools=[],
                disk_temperatures={},
                smart_test_results=[],
            )

            slots, enclosures, _selected_meta, rows, slot_count, columns = service._correlate(
                raw_data,
                ParsedSSHData(),
                [],
            )

            self.assertEqual([slot.device_name for slot in slots], ["da0"])
            self.assertEqual(enclosures[0].slot_count, 1)
            self.assertEqual(rows, [[0]])
            self.assertEqual((slot_count, columns), (1, 1))

    def test_disk_record_normalization_rejects_raw_inventory_above_the_supported_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="system-a", truenas=TrueNASConfig(platform="core"))
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            disks = [{"name": f"da{index}"} for index in range(4097)]

            with patch("app.services.inventory.DiskRecord", wraps=DiskRecord) as disk_record:
                with self.assertRaisesRegex(TrueNASAPIError, "4096"):
                    service._build_disk_records(disks, ParsedSSHData(), {}, {})

            disk_record.assert_not_called()

    def test_multi_enclosure_snapshot_warns_once_about_unapplied_legacy_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._legacy_fallback_service(temp_dir, [SystemConfig(id="system-a")])
            write_v1_mappings(service.mapping_store, {
                "default:1": ManualMapping(slot=1, serial="SYNTH-LEGACY-1"),
                "default:2": ManualMapping(slot=2, serial="SYNTH-LEGACY-2"),
                service.mapping_store._slot_key("system-a", "enc-a", 3): ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=3,
                    serial="SYNTH-SCOPED-3",
                ),
            })
            enclosures = [
                EnclosureOption(id="enc-a", label="Enclosure A", rows=2, columns=4, slot_count=8),
                EnclosureOption(id="enc-b", label="Enclosure B", rows=2, columns=4, slot_count=8),
            ]

            warnings: list[str] = []
            frame = service._resolve_layout_frame(
                enclosures,
                "enc-a",
                warnings,
                require_profile=False,
            )

            self.assertIsInstance(frame, inventory_module._LayoutFrame)
            self.assertFalse(frame.allow_legacy_mapping_fallback)
            self.assertEqual(len(warnings), 1)
            self.assertIn("2 saved bay assignments", warnings[0])
            self.assertNotIn("SYNTH", warnings[0])
            self.assertNotIn("enc-a", warnings[0])

    def test_legacy_mapping_warning_scopes_drawer_sub_views_to_the_base_enclosure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._legacy_fallback_service(temp_dir, [SystemConfig(id="system-a")])
            write_v1_mappings(service.mapping_store, {
                "default:1": ManualMapping(slot=1, serial="SYNTH-LEGACY-1"),
                "default:2": ManualMapping(slot=2, serial="SYNTH-LEGACY-2"),
                service.mapping_store._slot_key("system-a", "enc-a", 1): ManualMapping(
                    system_id="system-a",
                    enclosure_id="enc-a",
                    slot=1,
                    serial="SYNTH-SCOPED-1",
                ),
            })
            enclosures = [
                EnclosureOption(
                    id="enc-a::dell-md1280-drawer-top-42",
                    label="Drawer A",
                    rows=2,
                    columns=4,
                    slot_count=8,
                ),
                EnclosureOption(
                    id="enc-a::dell-md1280-drawer-bottom-42",
                    label="Drawer B",
                    rows=2,
                    columns=4,
                    slot_count=8,
                ),
                EnclosureOption(id="enc-b", label="Enclosure B", rows=2, columns=4, slot_count=8),
            ]

            warnings: list[str] = []
            frame = service._resolve_layout_frame(
                enclosures,
                "enc-a::dell-md1280-drawer-bottom-42",
                warnings,
                require_profile=False,
            )

            self.assertIsInstance(frame, inventory_module._LayoutFrame)
            self.assertFalse(frame.allow_legacy_mapping_fallback)
            self.assertEqual(
                frame.selected_meta["id"],
                "enc-a::dell-md1280-drawer-bottom-42",
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("1 saved bay assignment ", warnings[0])
            self.assertNotIn("2 saved", warnings[0])

    def test_legacy_mapping_warning_checks_the_selected_drawer_slot_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._legacy_fallback_service(temp_dir, [SystemConfig(id="system-a")])
            write_v1_mappings(service.mapping_store, {
                "default:42": ManualMapping(slot=42, serial="SYNTH-LEGACY-42"),
            })
            bottom_layout: list[list[int | None]] = [
                list(range(42, 56)),
                list(range(56, 70)),
                list(range(70, 84)),
            ]
            enclosures = [
                EnclosureOption(
                    id="enc-a::dell-md1280-drawer-bottom-42",
                    label="Bottom drawer",
                    rows=3,
                    columns=14,
                    slot_count=42,
                    slot_layout=bottom_layout,
                ),
                EnclosureOption(id="enc-b", label="Enclosure B", rows=2, columns=4, slot_count=8),
            ]

            warnings: list[str] = []
            frame = service._resolve_layout_frame(
                enclosures,
                "enc-a::dell-md1280-drawer-bottom-42",
                warnings,
                require_profile=False,
            )

            self.assertIsInstance(frame, inventory_module._LayoutFrame)
            self.assertFalse(frame.allow_legacy_mapping_fallback)
            self.assertEqual(set(frame.slot_positions), set(range(42, 84)))
            self.assertEqual(len(warnings), 1)
            self.assertIn("1 saved bay assignment ", warnings[0])

    def test_storage_view_slot_label_honors_profile_slot_number_base(self) -> None:
        storage_view = StorageViewConfig.model_validate(
            {
                "id": "shelf-view",
                "label": "Shelf",
                "kind": "ses_enclosure",
                "template_id": "ses-auto",
                "profile_id": "one-based-shelf",
            }
        )

        def profile(slot_number_base: int | None) -> EnclosureProfileView:
            return EnclosureProfileView(
                id="one-based-shelf",
                label="One-based shelf",
                rows=3,
                columns=14,
                slot_number_base=slot_number_base,
            )

        # Live SES slots label as f"{slot + base:02d}"; the storage-view
        # fallback for slots outside the snapshot must agree (issue #186).
        self.assertEqual(storage_view_slot_label(storage_view, 0, selected_profile=profile(1)), "01")
        self.assertEqual(storage_view_slot_label(storage_view, 41, selected_profile=profile(1)), "42")
        self.assertEqual(storage_view_slot_label(storage_view, 0, selected_profile=profile(None)), "00")
        self.assertEqual(storage_view_slot_label(storage_view, 7, selected_profile=profile(0)), "07")

    def test_ordered_storage_view_candidates_do_not_mutate_shared_payloads(self) -> None:
        service = object.__new__(InventoryService)
        storage_view = StorageViewConfig.model_validate(
            {
                "id": "boot-devices",
                "label": "Boot devices",
                "kind": "boot_devices",
                "template_id": "boot-satadom-2",
                "binding": {
                    "mode": "hybrid",
                    "device_names": ["ada1"],
                    "pool_names": ["boot-pool"],
                },
            }
        )
        candidates = [
            {
                "candidate_id": "disk-a",
                "device_names": ["ada0"],
                "serial": "SER-A",
                "pool_name": "boot-pool",
            },
            {
                "candidate_id": "disk-b",
                "device_names": ["ada1"],
                "serial": "SER-B",
                "pool_name": "boot-pool",
            },
        ]

        ordered = service._ordered_storage_view_candidates(storage_view, candidates, set())

        self.assertEqual([candidate["candidate_id"] for candidate in ordered], ["disk-b", "disk-a"])
        self.assertEqual(ordered[0]["_placement_key"], "device match: ada1")
        self.assertEqual(ordered[1]["_placement_key"], "pool match: boot-pool")
        self.assertTrue(all("_placement_key" not in candidate for candidate in candidates))

    def test_infer_slot_count_from_layout_counts_visible_slots_instead_of_max_id(self) -> None:
        self.assertEqual(infer_slot_count_from_layout([[9, None], [None, 3]]), 2)
        self.assertEqual(infer_slot_count_from_layout([[0, 1, 2, 38]], fallback=4), 4)

    def test_create_app_bounds_unknown_profile_startup_warnings(self) -> None:
        from admin_service.config import AdminSettings
        from app import main as app_main

        private_marker = "/private/config/path/PRIVATE-MARKER"
        settings = Settings(
            config_file=private_marker,
            systems=[
                SystemConfig(
                    id=f"system-{index}-" + ("s" * 512),
                    default_profile_id=f"missing-default-{index}-" + ("p" * 512),
                    enclosure_profiles={
                        f"enclosure-{index}-" + ("e" * 512): f"missing-enclosure-{index}-" + ("q" * 512)
                    },
                )
                for index in range(80)
            ],
        )

        with (
            patch.object(app_main, "get_settings", return_value=settings),
            patch.object(app_main, "load_read_ui_auth_settings", return_value=AdminSettings()),
            patch.object(app_main, "configure_logging"),
            self.assertLogs("app.main", level="WARNING") as captured,
        ):
            created = app_main.create_app()

        self.assertIsNotNone(created)
        self.assertEqual(len(captured.records), 50)
        self.assertTrue(all(len(record.getMessage()) <= 512 for record in captured.records))
        self.assertNotIn(private_marker, "\n".join(record.getMessage() for record in captured.records))

    def test_build_lunid_aliases_core_keeps_narrow_match_window(self) -> None:
        aliases = build_lunid_aliases("5000c5003e8253a7", "core")

        self.assertIn("5000c5003e8253a7", aliases)
        self.assertIn("5000c5003e8253a8", aliases)
        self.assertNotIn("5000c5003e8253a5", aliases)

    def test_build_lunid_aliases_scale_allows_two_count_offset(self) -> None:
        aliases = build_lunid_aliases("5000c5003e8253a7", "scale")

        self.assertIn("5000c5003e8253a5", aliases)
        self.assertIn("5000c5003e8253a6", aliases)
        self.assertIn("5000c5003e8253a7", aliases)
        self.assertIn("5000c5003e8253a8", aliases)
        self.assertIn("5000c5003e8253a9", aliases)

    def test_build_lunid_aliases_quantastor_allows_two_count_offset(self) -> None:
        aliases = build_lunid_aliases("5002538b103e5ee0", "quantastor")

        self.assertIn("5002538b103e5ede", aliases)
        self.assertIn("5002538b103e5edf", aliases)
        self.assertIn("5002538b103e5ee0", aliases)
        self.assertIn("5002538b103e5ee1", aliases)
        self.assertIn("5002538b103e5ee2", aliases)

    def test_build_lunid_alias_tiers_splits_exact_identity_from_shifted_neighbors(self) -> None:
        exact, shifted = build_lunid_alias_tiers("5000c5003e8253a7", "scale")

        self.assertEqual(exact, "5000c5003e8253a7")
        self.assertEqual(
            shifted,
            {
                "5000c5003e8253a5",
                "5000c5003e8253a6",
                "5000c5003e8253a8",
                "5000c5003e8253a9",
            },
        )

    def _make_disk_record(self, device_name: str, lunid: str) -> DiskRecord:
        return DiskRecord(
            raw={},
            device_name=device_name,
            path_device_name=device_name,
            multipath_name=None,
            multipath_member=None,
            serial=None,
            model=None,
            size_bytes=None,
            identifier=None,
            health=None,
            pool_name=None,
            lunid=lunid,
            bus=None,
            temperature_c=None,
            last_smart_test_type=None,
            last_smart_test_status=None,
            last_smart_test_lifetime_hours=None,
            logical_block_size=None,
            physical_block_size=None,
            enclosure_id=None,
            slot=None,
            smart_devices=[],
            lookup_keys=set(),
        )

    def test_index_disks_by_sas_exact_identity_beats_neighbor_shifted_alias(self) -> None:
        # Same-batch disks often carry near-sequential lunids. With the wider
        # scale window, each disk's shifted aliases land on the other disk's
        # exact identity; the exact identity must always win the lookup.
        disk_a = self._make_disk_record("sdaa", "5000c5003e825300")
        disk_b = self._make_disk_record("sdab", "5000c5003e825302")

        index = index_disks_by_sas(
            (disk, *lunid_alias_tier_sets(disk.lunid, "scale"))
            for disk in (disk_a, disk_b)
        )

        self.assertIs(index["5000c5003e825300"], disk_a)
        self.assertIs(index["5000c5003e825302"], disk_b)
        # The alias between the two identities is claimed by both disks, so it
        # must be dropped instead of resolving to whichever disk came last.
        self.assertNotIn("5000c5003e825301", index)
        # Unshared shifted aliases keep matching their only claimant.
        self.assertIs(index["5000c5003e8252fe"], disk_a)
        self.assertIs(index["5000c5003e825304"], disk_b)

    def test_index_disks_by_sas_keeps_all_aliases_for_a_single_disk(self) -> None:
        disk = self._make_disk_record("sdaa", "5000c5003e8253a7")

        index = index_disks_by_sas(
            [(disk, *lunid_alias_tier_sets(disk.lunid, "scale"))]
        )

        for alias in build_lunid_aliases("5000c5003e8253a7", "scale"):
            self.assertIs(index[alias], disk)

    def test_quantastor_backoff_warnings_are_collapsed_across_cli_and_ses(self) -> None:
        warnings = [
            "Quantastor SSH CLI enrichment failed on 10.13.37.31 (retry_backoff; exit 255; retry after 2026-06-12T15:07:44.593780+00:00). REST data is still being used.",
            "Quantastor SSH CLI enrichment failed on 10.13.37.30 (retry_backoff; exit 255; retry after 2026-06-12T15:07:41.513217+00:00). REST data is still being used.",
            "Quantastor SSH SES discovery failed on 10.13.37.31 (retry_backoff; exit 255; retry after 2026-06-12T15:07:44.593780+00:00).",
            "Quantastor SSH SES discovery failed on 10.13.37.30 (retry_backoff; exit 255; retry after 2026-06-12T15:07:41.513217+00:00).",
            "IO fencing is off on this QuantaStor cluster.",
        ]

        collapsed = InventoryService._collapse_quantastor_optional_ssh_backoff_warnings(warnings)

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(collapsed[1], "IO fencing is off on this QuantaStor cluster.")
        self.assertIn("is paused after a connection failure", collapsed[0])
        self.assertIn("10.13.37.30", collapsed[0])
        self.assertIn("10.13.37.31", collapsed[0])
        self.assertIn("Data still comes from the QuantaStor API", collapsed[0])
        self.assertNotIn("SES discovery", "\n".join(collapsed))
        self.assertNotIn("disk inventory", "\n".join(collapsed))

    @staticmethod
    def _capability_service(platform: str, *, ssh_enabled: bool = True, bmc_enabled: bool = False) -> InventoryService:
        service = object.__new__(InventoryService)
        service.system = SystemConfig(
            id=f"{platform}-system",
            truenas=TrueNASConfig(platform=platform),
            ssh=SSHConfig(enabled=ssh_enabled),
            bmc=BMCConfig(enabled=bmc_enabled),
        )
        return service

    @staticmethod
    def _capability_slot(*, led_supported: bool = False, device_name: str = "da0") -> SlotView:
        return SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            device_name=device_name,
            led_supported=led_supported,
        )

    def test_platform_capabilities_core_exposes_deep_diagnostics_and_identify(self) -> None:
        service = self._capability_service("core", ssh_enabled=True)
        caps = service._build_platform_capabilities(
            slots=[self._capability_slot(led_supported=True, device_name="da0")],
            available_enclosures=[EnclosureOption(id="enc0", label="Front")],
            summary=InventorySummary(disk_count=1, enclosure_count=1, mapped_slot_count=1),
            sources={
                "api": SourceStatus(enabled=True, ok=True),
                "ssh": SourceStatus(enabled=True, ok=True),
            },
            platform_context={},
            selected_profile=object(),
        )

        self.assertEqual(caps["inventory"].status, "available")
        self.assertEqual(caps["physical_slots"].status, "available")
        self.assertEqual(caps["identify"].status, "available")
        self.assertEqual(caps["diagnostics"].status, "available")
        self.assertIn("mprutil", caps["diagnostics"].sources)

    def test_platform_capabilities_scale_reports_linux_ses_fabric_as_partial_diagnostics(self) -> None:
        service = self._capability_service("scale", ssh_enabled=True)
        slot = self._capability_slot(led_supported=True, device_name="sdc")
        slot.ssh_ses_device = "/dev/sg27"
        slot.ssh_ses_targets = [{"ses_device": "/dev/sg27", "ses_element_id": 0}]
        caps = service._build_platform_capabilities(
            slots=[slot],
            available_enclosures=[EnclosureOption(id="sg27", label="Front 24")],
            summary=InventorySummary(disk_count=1, pool_count=1, enclosure_count=1, mapped_slot_count=1),
            sources={
                "api": SourceStatus(enabled=True, ok=True),
                "ssh": SourceStatus(enabled=True, ok=True),
            },
            platform_context={},
            selected_profile=object(),
        )

        self.assertEqual(caps["physical_slots"].status, "available")
        self.assertEqual(caps["smart_detail"].status, "available")
        self.assertEqual(caps["identify"].status, "available")
        self.assertEqual(caps["diagnostics"].status, "partial")
        self.assertIn("sg_ses AES/EC", caps["diagnostics"].sources)
        self.assertIn("smartmontools", caps["smart_detail"].requirements)

    def test_platform_capabilities_quantastor_keeps_ha_context_and_ses_identify(self) -> None:
        service = self._capability_service("quantastor", ssh_enabled=True)
        caps = service._build_platform_capabilities(
            slots=[self._capability_slot(led_supported=True, device_name="sda")],
            available_enclosures=[EnclosureOption(id="node-a", label="Node A")],
            summary=InventorySummary(disk_count=1, pool_count=1, enclosure_count=1, mapped_slot_count=1),
            sources={
                "api": SourceStatus(enabled=True, ok=True),
                "ssh": SourceStatus(enabled=True, ok=True),
            },
            platform_context={"selected_view_label": "Node A", "master_label": "Node B"},
            selected_profile=object(),
        )

        self.assertEqual(caps["inventory"].status, "available")
        self.assertIn("Quantastor REST", caps["inventory"].sources)
        self.assertEqual(caps["platform_details"].status, "available")
        self.assertEqual(caps["identify"].status, "available")
        self.assertEqual(caps["diagnostics"].status, "unsupported")

    def test_platform_capabilities_linux_marks_profile_dependent_storage_as_available(self) -> None:
        service = self._capability_service("linux", ssh_enabled=True)
        caps = service._build_platform_capabilities(
            slots=[self._capability_slot(device_name="nvme0n1")],
            available_enclosures=[],
            summary=InventorySummary(disk_count=1, mapped_slot_count=1),
            sources={"ssh": SourceStatus(enabled=True, ok=True)},
            platform_context={},
            selected_profile=object(),
        )

        self.assertEqual(caps["inventory"].status, "available")
        self.assertEqual(caps["physical_slots"].status, "available")
        self.assertEqual(caps["smart_detail"].status, "available")
        self.assertEqual(caps["identify"].status, "partial")
        self.assertEqual(caps["diagnostics"].status, "unsupported")

    def test_platform_capabilities_esxi_keeps_identify_unsupported(self) -> None:
        service = self._capability_service("esxi", ssh_enabled=True)
        caps = service._build_platform_capabilities(
            slots=[self._capability_slot(device_name="13:0")],
            available_enclosures=[],
            summary=InventorySummary(disk_count=1, mapped_slot_count=1),
            sources={"ssh": SourceStatus(enabled=True, ok=True)},
            platform_context={"sections": [{"title": "Controller"}]},
            selected_profile=object(),
        )

        self.assertEqual(caps["inventory"].status, "available")
        self.assertEqual(caps["physical_slots"].status, "available")
        self.assertEqual(caps["smart_detail"].status, "available")
        self.assertIn("ESXCLI", caps["smart_detail"].sources)
        self.assertEqual(caps["identify"].status, "unsupported")
        self.assertEqual(caps["platform_details"].status, "available")
        self.assertEqual(caps["diagnostics"].status, "unsupported")

    def test_smart_candidate_devices_ignores_placeholder_hctl_labels(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            device_name="7:0:0:0",
        )

        self.assertEqual(InventoryService._smart_candidate_devices(slot), [])

    def test_smart_candidate_devices_ignores_bmc_slot_placeholders(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            smart_device_names=["bmc-slot:2"],
            device_name="bmc-slot:2",
        )

        self.assertEqual(InventoryService._smart_candidate_devices(slot), [])

    def test_resolve_system_storage_views_can_use_runtime_inferred_fattwin_profile(self) -> None:
        settings = Settings()
        system = SystemConfig(
            id="f629-node",
            truenas=TrueNASConfig(platform="ipmi"),
        )

        views = resolve_system_storage_views(
            system,
            ProfileRegistry(settings),
            inferred_profile_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
        )

        self.assertEqual([view.id for view in views], ["primary-chassis", "fat-twin-rear"])
        self.assertEqual(views[1].binding.device_names, ["bmc-slot:6", "bmc-slot:7"])

    def test_fattwin_rear_template_uses_stacked_slot_order(self) -> None:
        template = get_storage_view_template("fat-twin-rear-2")

        self.assertIsNotNone(template)
        assert template is not None
        self.assertEqual(template.rows, 2)
        self.assertEqual(template.columns, 1)
        self.assertEqual(template.slot_layout, [[1], [0]])

    def test_passive_m2_carrier_template_is_visible_with_auto_binding(self) -> None:
        template = get_storage_view_template("m2-carrier-passive-2")

        self.assertIsNotNone(template)
        assert template is not None
        self.assertEqual(template.kind, "nvme_carrier")
        self.assertEqual(template.slot_count, 2)
        self.assertEqual(template.slot_layout, [[1], [0]])
        self.assertEqual(template.default_slot_labels, {0: "M2-1", 1: "M2-2"})
        self.assertTrue(template.default_render.show_in_main_ui)
        self.assertEqual(template.default_binding, StorageViewBindingConfig(mode="auto"))
        self.assertEqual(template.platform_binding_defaults, {})
        self.assertFalse(template.supports_led)
        self.assertNotIn("ESXi", f"{template.summary} {template.notes or ''}")

    def test_aoc_template_keeps_esxi_member_ids_in_the_esxi_entry_only(self) -> None:
        template = get_storage_view_template("aoc-slg4-2h8m2-2")

        self.assertIsNotNone(template)
        assert template is not None
        self.assertTrue(template.default_render.show_in_main_ui)
        self.assertEqual(template.default_binding, StorageViewBindingConfig(mode="hybrid"))
        self.assertEqual(list(template.platform_binding_defaults), ["esxi"])
        esxi_binding = template.platform_binding_defaults["esxi"]
        self.assertEqual(esxi_binding.mode, "hybrid")
        self.assertEqual(esxi_binding.device_names, ["13:0", "13:1"])
        self.assertEqual(esxi_binding.pcie_addresses, ["C0 x4", "C1 x4"])
        self.assertNotIn("ESXi", f"{template.summary} {template.notes or ''}")

    def test_card_and_rear_group_templates_name_their_chassis_profile(self) -> None:
        # #952: Add a view offers this hardware once, as the template.
        named = {
            template.id: template.same_hardware_profile_id
            for template in list_storage_view_templates()
            if template.same_hardware_profile_id
        }
        self.assertEqual(
            named,
            {
                "aoc-slg4-2h8m2-2": ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
                "fat-twin-rear-2": SUPERMICRO_FATTWIN_REAR_2_PROFILE_ID,
            },
        )
        registry = ProfileRegistry(Settings())
        for template_id, profile_id in named.items():
            with self.subTest(template=template_id):
                self.assertNotEqual(get_storage_view_template(template_id).kind, "ses_enclosure")
                self.assertIsNotNone(registry.get(profile_id))

    def test_every_template_shows_new_views_and_names_known_platforms(self) -> None:
        platforms = set(get_args(TrueNASConfig.model_fields["platform"].annotation))
        for template in list_storage_view_templates():
            with self.subTest(template=template.id):
                self.assertTrue(template.default_render.show_in_main_ui)
                self.assertLessEqual(set(template.platform_binding_defaults), platforms)
                for field in ("device_names", "pcie_addresses"):
                    self.assertNotIn("13:0", getattr(template.default_binding, field))
                    self.assertNotIn("C0 x4", getattr(template.default_binding, field))

    def test_summarize_ssh_failures_collapses_transport_level_batch_failure(self) -> None:
        command_results = [
            SSHCommandResult(
                command=command,
                ok=False,
                stderr="Authentication failed.",
                exit_code=255,
                local_failure=SSHLocalFailure("auth_failed", "AuthenticationException"),
            )
            for command in (
                "esxcli storage core adapter list",
                "esxcli storage core device list",
                "/opt/lsi/storcli64/storcli64 /c0 show all J",
            )
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(command_results, {})

        self.assertEqual(
            warnings,
            [
                "Could not log in over SSH (auth_failed). Bay details from SSH are missing for this refresh."
            ],
        )
        self.assertEqual(status_message, "SSH login failed (auth_failed).")

    def test_summarize_ssh_failures_collapses_missing_storcli_batch(self) -> None:
        command_results = [
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0 show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0/vall show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(
            command_results,
            {"esxcli storage core adapter list": "vmhba64 iscsi_vmk"},
        )

        self.assertEqual(
            warnings,
            [
                "StorCLI is not available on this ESXi host, so drive details behind the RAID controller "
                "are missing for this refresh."
            ],
        )
        self.assertEqual(status_message, "StorCLI commands unavailable.")

    def test_summarize_ssh_failures_mentions_broadcom_lsi_stack_when_storcli_missing(self) -> None:
        command_results = [
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0 show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0/vall show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
            SSHCommandResult(
                command="/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J",
                ok=False,
                stderr="sh: /opt/lsi/storcli64/storcli64: not found",
                exit_code=127,
            ),
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(
            command_results,
            {
                "esxcli software vib list": (
                    "lsi-mr3 7.727.02.00-1OEM.700.1.0.15843807 BCM VMwareCertified\n"
                    "lsuv2-lsiv2-drivers-plugin 1.0.0-12vmw.703.0.50.20036589 VMware VMwareCertified\n"
                ),
            },
        )

        self.assertEqual(
            warnings,
            [
                "StorCLI is not installed on this ESXi host, so drive details behind the RAID "
                "controller are missing. The Broadcom driver packages alone (lsi-mr3, "
                "lsuv2-lsiv2-drivers-plugin) do not provide them; install the Broadcom StorCLI VIB."
            ],
        )
        self.assertEqual(status_message, "StorCLI commands unavailable.")

    def test_summarize_ssh_failures_collapses_sas_fabric_enrichment_failures_with_debug_details(self) -> None:
        command_results = [
            SSHCommandResult(command="sudo -n /usr/sbin/mprutil show adapters", ok=True, stdout="/dev/mpr10 SAS3816 Broadcom 9500-16e 28.00.00.00"),
            SSHCommandResult(
                command="sudo -n /usr/sbin/mprutil -u 10 show iocfacts",
                ok=False,
                stderr="mprutil: ioctl MPRIO_READ_CFG_HEADER: Device not configured",
                exit_code=1,
            ),
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(
            command_results,
            {"sudo -n /usr/sbin/mprutil show adapters": command_results[0].stdout},
        )
        details = InventoryService._ssh_failure_details(command_results)

        self.assertEqual(
            warnings,
            [
                "Some SSH commands failed (IOC facts). The bay map still works; "
                "see Debug for failure context."
            ],
        )
        self.assertEqual(status_message, "Some optional SSH reads failed (IOC facts). The bay map is complete.")
        self.assertEqual(details[0]["reason"], "ssh_command_failed")
        self.assertEqual(details[0]["controller"], "mpr10")
        self.assertEqual(details[0]["context"], "sas_fabric_mprutil_iocfacts")

    def test_linux_storage_enrichment_probe_adds_guarded_nvme_subsystem_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="offsite-scale",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            command_results = [
                SSHCommandResult(command="/usr/bin/lsblk -OJ", ok=True, stdout='{"blockdevices": []}'),
                SSHCommandResult(command="/usr/bin/lsscsi -g -t", ok=True, stdout=""),
            ]

            commands = service._linux_storage_enrichment_probe_commands(command_results)

            self.assertEqual(len(commands), 2)
            self.assertIn("nvme list-subsys -o json", commands[0])
            self.assertIn("|| true", commands[0])
            self.assertEqual(canonicalize_ssh_command(commands[0]), "nvme list-subsys -o json")
            self.assertIn("/sys/class/enclosure", commands[1])
            self.assertEqual(canonicalize_ssh_command(commands[1]), "enclosure sysfs map")

    def test_linux_nvme_subsystem_failure_is_classified_as_enrichment(self) -> None:
        command_results = [
            SSHCommandResult(command="/usr/bin/lsblk -OJ", ok=True, stdout='{"blockdevices": []}'),
            SSHCommandResult(
                command="/usr/sbin/nvme list-subsys -o json",
                ok=False,
                stderr="nvme: command not found",
                exit_code=127,
            ),
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(
            command_results,
            {"/usr/bin/lsblk -OJ": command_results[0].stdout},
        )
        details = InventoryService._ssh_failure_details(command_results)

        self.assertEqual(
            warnings,
            [
                "Some SSH commands failed (Linux NVMe subsystem detail). The bay map still works; "
                "see Debug for failure context."
            ],
        )
        self.assertEqual(
            status_message,
            "Some optional SSH reads failed (Linux NVMe subsystem detail). The bay map is complete.",
        )
        self.assertEqual(details[0]["reason"], "command_not_found")
        self.assertEqual(details[0]["context"], "storage_fabric_linux_nvme_subsystems")
        self.assertEqual(details[0]["criticality"], "enrichment")
        self.assertEqual(details[0]["exit_code"], 127)
        self.assertNotIn("stderr", details[0])

    def test_ssh_failure_warnings_and_details_omit_command_and_remote_text(self) -> None:
        command_results = [
            SSHCommandResult(
                command="/usr/bin/qs disk-list --json '--server=localhost,jbodmap,super-secret'",
                ok=False,
                stderr="permission denied",
                exit_code=27,
            )
        ]

        warnings, status_message = InventoryService._summarize_ssh_failures(command_results, {})
        details = InventoryService._ssh_failure_details(command_results)

        self.assertEqual(status_message, "Some SSH commands failed.")
        self.assertEqual(
            warnings,
            ["SSH command failed (SSH command 1; permission_denied; exit 27)."],
        )
        self.assertEqual(
            details,
            [
                {
                    "reason": "permission_denied",
                    "exit_code": 27,
                    "context": "ssh_command_1",
                    "context_label": "SSH command 1",
                    "criticality": "inventory",
                }
            ],
        )

    def test_suppress_scale_configured_sg_ses_failures_keeps_other_ssh_warnings(self) -> None:
        sg_ses_failure = (
            "SSH command failed (SG SES additional element status; ssh_command_failed; exit 5)."
        )
        smart_failure = "SSH command failed (SMART detail; ssh_command_failed; exit 2)."

        warnings, failures = InventoryService._suppress_scale_configured_sg_ses_failures(
            [sg_ses_failure, smart_failure],
            [sg_ses_failure, smart_failure],
        )

        self.assertEqual(warnings, [smart_failure])
        self.assertEqual(failures, [smart_failure])

    def test_esxi_storcli_runtime_warning_reports_zero_visible_controllers(self) -> None:
        warning = InventoryService._esxi_storcli_runtime_warning(
            {
                "esxcli software vib list": (
                    "lsi-mr3 7.727.02.00-1OEM.700.1.0.15843807 BCM VMwareCertified\n"
                    "lsuv2-lsiv2-drivers-plugin 1.0.0-12vmw.703.0.50.20036589 VMware VMwareCertified\n"
                    "vmware-storcli64 007.2705.0000.0000-01 BCM PartnerSupported\n"
                ),
                "/opt/lsi/storcli64/storcli64 /c0 show all J": (
                    "{\n"
                    "\"Controllers\":[{\n"
                    "\"Command Status\":{\"Status\":\"Failure\",\"Description\":\"Controller 0 not found\"}\n"
                    "}]\n"
                    "}\n"
                ),
                "/opt/lsi/storcli64/storcli64 show J": (
                    "{\n"
                    "\"Controllers\":[{\n"
                    "\"Response Data\":{\"Number of Controllers\" : 0}\n"
                    "}]\n"
                    "}\n"
                ),
            }
        )

        self.assertEqual(
            warning,
            "StorCLI is installed on this ESXi host, but it currently reports no visible "
            "MegaRAID controllers. Broadcom/LSI packages such as lsi-mr3, lsuv2-lsiv2-drivers-plugin, "
            "and vmware-storcli64 are present, but no compatible controller is being surfaced to StorCLI, "
            "so drive details behind the RAID controller are missing for this refresh.",
        )

    def test_esxi_storcli_runtime_warning_reports_pci_passthrough_when_detected(self) -> None:
        warning = InventoryService._esxi_storcli_runtime_warning(
            {
                "esxcli software vib list": (
                    "lsi-mr3 7.727.02.00-1OEM.700.1.0.15843807 BCM VMwareCertified\n"
                    "vmware-storcli64 007.2705.0000.0000-01 BCM PartnerSupported\n"
                ),
                "/opt/lsi/storcli64/storcli64 /c0 show all J": (
                    "{\n"
                    "\"Controllers\":[{\n"
                    "\"Command Status\":{\"Status\":\"Failure\",\"Description\":\"Controller 0 not found\"}\n"
                    "}]\n"
                    "}\n"
                ),
                "/opt/lsi/storcli64/storcli64 show J": (
                    "{\n"
                    "\"Controllers\":[{\n"
                    "\"Response Data\":{\"Number of Controllers\" : 0}\n"
                    "}]\n"
                    "}\n"
                ),
                "esxcli hardware pci pcipassthru list 2>&1 || true": (
                    "Device ID     Enabled\n"
                    "------------  -------\n"
                    "0000:3b:00.0     true\n"
                ),
                "lspci 2>&1 || true": (
                    "0000:3b:00.0 RAID bus controller: Broadcom MegaRAID SAS Invader Controller [vmhba2]\n"
                ),
            }
        )

        self.assertEqual(
            warning,
            "StorCLI is installed on this ESXi host, but the Broadcom MegaRAID controller is "
            "currently configured for PCI passthrough (0000:3b:00.0). ESXi will not bind that "
            "device to lsi_mr3 or expose it to StorCLI until passthrough is disabled and the host "
            "is rebooted, so drive details behind the RAID controller are missing for this refresh.",
        )

    def test_extract_block_sizes_from_scale_disk_metadata(self) -> None:
        disk = {
            "size": 14000519643136,
            "blocks": 27344764928,
            "sectorsize": 4096,
        }

        self.assertEqual(InventoryService._extract_logical_block_size(disk, disk["size"]), 512)
        self.assertEqual(InventoryService._extract_physical_block_size(disk), 4096)

    def test_fallback_smart_summary_surfaces_block_sizes(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            logical_block_size=512,
            physical_block_size=4096,
            logical_unit_id="5000cca264d473d4",
            sas_address="5000cca264d473d5",
        )

        summary = InventoryService._fallback_smart_summary(slot, "Detailed SMART JSON is unavailable.")

        self.assertTrue(summary.available)
        self.assertEqual(summary.logical_block_size, 512)
        self.assertEqual(summary.physical_block_size, 4096)
        self.assertEqual(summary.logical_unit_id, "5000cca264d473d4")
        self.assertEqual(summary.sas_address, "5000cca264d473d5")

    def test_correlate_esxi_host_maps_storcli_physical_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="cryo-esxi",
                label="CryoStorage ESXi",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="10.88.88.20", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            loaded_mappings: dict[str, ManualMapping] = {}
            service.mapping_store.load_lookup_snapshot = MagicMock(return_value=loaded_mappings)  # type: ignore[method-assign]
            service.mapping_store.get_mapping = MagicMock(return_value=None)  # type: ignore[method-assign]
            ssh_data = ParsedSSHData(
                esxi_storcli_virtual_drives=[
                    {
                        "vd_id": "0",
                        "name": "ESXi",
                        "raid": "RAID1",
                        "state": "Optl",
                        "physical_drives": [
                            {"slot_key": "13:0", "slot": 0, "enclosure_id": "13"},
                            {"slot_key": "13:1", "slot": 1, "enclosure_id": "13"},
                        ],
                    },
                    {
                        "vd_id": "1",
                        "name": "VMs",
                        "raid": "RAID1",
                        "state": "Optl",
                        "physical_drives": [
                            {"slot_key": "13:0", "slot": 0, "enclosure_id": "13"},
                            {"slot_key": "13:1", "slot": 1, "enclosure_id": "13"},
                        ],
                    },
                ],
                esxi_storcli_physical_drives=[
                    {
                        "slot_key": "13:0",
                        "enclosure_id": "13",
                        "slot": 0,
                        "controller_id": "c0",
                        "state": "Onln",
                        "size": "1.818 TB",
                        "interface": "NVMe",
                        "sector_size": "512B",
                        "model": "Samsung SSD 970 EVO 2TB",
                        "serial": "SERIAL0",
                        "firmware": "2B2QEXE7",
                        "temperature_c": 34,
                        "media_errors": 60,
                        "predictive_errors": 0,
                        "smart_alert": "No",
                        "connector_name": "C0 x4",
                        "connected_port": "0(path0)",
                        "link_speed": "8.0GT/s",
                    },
                    {
                        "slot_key": "13:1",
                        "enclosure_id": "13",
                        "slot": 1,
                        "controller_id": "c0",
                        "state": "Onln",
                        "size": "1.818 TB",
                        "interface": "NVMe",
                        "sector_size": "512B",
                        "model": "Samsung SSD 970 EVO 2TB",
                        "serial": "SERIAL1",
                        "firmware": "2B2QEXE7",
                        "temperature_c": 35,
                        "media_errors": 262,
                        "predictive_errors": 0,
                        "smart_alert": "No",
                        "connector_name": "C1 x4",
                        "connected_port": "1(path0)",
                        "link_speed": "8.0GT/s",
                    },
                ],
            )

            slots, enclosures, selected_meta, layout_rows, slot_count, columns = service._correlate_esxi_host(
                ssh_data,
                [],
                None,
            )

            self.assertEqual(enclosures[0].profile_id, ESXI_AOC_SLG4_2H8M2_PROFILE_ID)
            self.assertEqual(selected_meta["id"], ESXI_AOC_SLG4_2H8M2_PROFILE_ID)
            self.assertEqual(layout_rows, [[1], [0]])
            self.assertEqual(slot_count, 2)
            self.assertEqual(columns, 1)
            self.assertEqual(slots[0].device_name, "13:0")
            self.assertEqual(slots[0].pool_name, "ESXi + VMs")
            self.assertEqual(slots[0].temperature_c, 34)
            service.mapping_store.load_lookup_snapshot.assert_called_once_with()
            self.assertEqual(service.mapping_store.get_mapping.call_count, slot_count)
            self.assertTrue(
                all(call.kwargs["loaded_entries"] is loaded_mappings for call in service.mapping_store.get_mapping.call_args_list)
            )
            self.assertFalse(slots[0].led_supported)
            self.assertEqual(slots[0].led_reason, "Bay lights cannot be controlled on ESXi hosts.")

            smart = service._build_esxi_smart_summary(slots[0])
            self.assertTrue(smart.available)
            self.assertEqual(smart.media_errors, 60)
            self.assertEqual(smart.firmware_version, "2B2QEXE7")
            self.assertEqual(smart.negotiated_link_rate, "8.0GT/s")
            self.assertEqual(smart.transport_protocol, "NVMe")
            self.assertEqual(smart.form_factor, "M.2")
            self.assertEqual(smart.message, "SMART data comes from the RAID controller (StorCLI).")

    def test_build_esxi_smart_summary_does_not_force_nvme_wording_for_sas_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="esxi-ft-node-2",
                label="esxi-ft-node-2",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="192.0.2.121", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            slot = SlotView(
                slot=2,
                slot_label="02",
                row_index=0,
                column_index=0,
                health="JBOD",
                temperature_c=34,
                logical_block_size=512,
                physical_block_size=512,
                raw_status={
                    "storcli_physical_drive": {
                        "interface": "SAS",
                        "firmware": "A3A0",
                        "link_speed": "6.0Gb/s",
                        "media_errors": 0,
                        "predictive_errors": 0,
                        "other_errors": 0,
                        "smart_alert": "No",
                    }
                },
            )

            smart = service._build_esxi_smart_summary(slot)

            self.assertTrue(smart.available)
            self.assertEqual(smart.transport_protocol, "SAS")
            self.assertIsNone(smart.form_factor)
            self.assertNotIn("NVMe SMART", smart.message or "")
            self.assertEqual(smart.message, "SMART data comes from the RAID controller (StorCLI).")

    def test_build_esxi_smart_summary_does_not_assume_m2_for_generic_nvme_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="generic-esxi-nvme",
                label="generic-esxi-nvme",
                truenas=TrueNASConfig(platform="esxi"),
                default_profile_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
                ssh=SSHConfig(enabled=True, host="192.0.2.122", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
                health="ONLINE",
                temperature_c=31,
                logical_block_size=4096,
                physical_block_size=4096,
                raw_status={
                    "storcli_physical_drive": {
                        "interface": "NVMe",
                        "firmware": "1.0",
                        "link_speed": "8.0GT/s",
                        "media_errors": 0,
                        "predictive_errors": 0,
                        "other_errors": 0,
                        "smart_alert": "No",
                    }
                },
            )

            smart = service._build_esxi_smart_summary(slot)

            self.assertEqual(smart.transport_protocol, "NVMe")
            self.assertEqual(smart.rotation_rate_rpm, 0)
            self.assertIsNone(smart.form_factor)

    def test_build_esxi_disk_records_links_local_sas_jbod_member_to_host_device(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="esxi-ft-node-2",
                label="esxi-ft-node-2",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="192.0.2.121", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            ssh_data = ParsedSSHData(
                esxi_storage_devices=[
                    {
                        "id": "naa.5000cca07316765c",
                        "display_name": "Local HGST Disk (naa.5000cca07316765c)",
                        "devfs_path": "/vmfs/devices/disks/naa.5000cca07316765c",
                        "other_uids": "vml.02000000005000cca07316765c483732343041",
                        "is_local": "true",
                        "drive_type": "physical",
                        "raid_level": "NA",
                    }
                ],
                esxi_storage_paths=[
                    {
                        "id": "sas.50030480252d8100-sas.5000cca07316765d-naa.5000cca07316765c",
                        "runtime_name": "vmhba2:C0:T27:L0",
                        "device": "naa.5000cca07316765c",
                        "target": "27",
                        "transport": "sas",
                        "target_identifier": "sas.5000cca07316765d",
                        "state": "active",
                    }
                ],
                esxi_storcli_physical_drives=[
                    {
                        "slot_key": "252:2",
                        "enclosure_id": "252",
                        "slot": 2,
                        "controller_id": "c0",
                        "device_id": "27",
                        "state": "JBOD",
                        "size": "3.638 TB",
                        "interface": "SAS",
                        "sector_size": "512B",
                        "model": "H7240AS60SUN4.0T",
                        "serial": "001518EDAYRX        PEGDAYRX",
                        "firmware": "A3A0",
                        "temperature_c": 34,
                        "media_errors": 0,
                        "predictive_errors": 0,
                        "other_errors": 0,
                        "smart_alert": "No",
                        "connector_name": "Port 0 - 3 x1",
                        "connected_port": "0(path0)",
                        "link_speed": "6.0Gb/s",
                    }
                ],
            )

            records = service._build_esxi_disk_records(ssh_data)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].pool_name, "ESXi local JBOD")
            self.assertEqual(records[0].health, "ONLINE")
            self.assertEqual(records[0].lunid, "naa.5000cca07316765c")
            self.assertEqual(records[0].smart_devices, ["naa.5000cca07316765c", "vml.02000000005000cca07316765c483732343041"])
            self.assertEqual(records[0].raw.get("esxi_runtime_name"), "vmhba2:C0:T27:L0")
            self.assertEqual(records[0].raw.get("esxi_drive_type"), "physical")
            self.assertEqual(records[0].raw.get("esxi_raid_level"), "NA")

    def test_build_esxi_slot_smart_summary_merges_host_smart_for_local_jbod(self) -> None:
        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                if command == "esxcli storage core device smart get -d naa.5000cca07316765c":
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            "Parameter                 Value         Threshold  Worst  Raw\n"
                            "------------------------  ------------  ---------  -----  ---\n"
                            "Health Status             OK            N/A        N/A    N/A\n"
                            "Write Error Count         0             N/A        N/A    N/A\n"
                            "Read Error Count          444578        N/A        N/A    N/A\n"
                            "Power Cycle Count         29            N/A        N/A    N/A\n"
                            "Reallocated Sector Count  0             N/A        N/A    N/A\n"
                            "Drive Temperature         34            N/A        N/A    N/A\n"
                            "Write Sectors TOT Count   224246729061  N/A        N/A    N/A\n"
                            "Read Sectors TOT Count    49378996762   N/A        N/A    N/A\n"
                            "Program Fail Count        0             N/A        N/A    N/A\n"
                            "Erase Fail Count          0             N/A        N/A    N/A\n"
                        ),
                        exit_code=0,
                    )
                return SSHCommandResult(command=command, ok=False, stderr="unexpected command", exit_code=1)

        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="esxi-ft-node-2",
                label="esxi-ft-node-2",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="192.0.2.121", user="root"),
            )
            settings = Settings(systems=[system])
            probe = DummySSHProbe()
            service = build_inventory_service(settings, system, MagicMock(), probe, temp_dir)
            slot = SlotView(
                slot=2,
                slot_label="02",
                row_index=0,
                column_index=0,
                health="JBOD",
                temperature_c=34,
                logical_block_size=512,
                physical_block_size=512,
                smart_device_names=["naa.5000cca07316765c"],
                raw_status={
                    "storcli_physical_drive": {
                        "interface": "SAS",
                        "firmware": "A3A0",
                        "link_speed": "6.0Gb/s",
                        "media_errors": 0,
                        "predictive_errors": 0,
                        "other_errors": 0,
                        "smart_alert": "No",
                        "esxi_device_id": "naa.5000cca07316765c",
                        "esxi_other_uids": "vml.02000000005000cca07316765c483732343041",
                        "esxi_drive_type": "physical",
                        "esxi_raid_level": "NA",
                        "logical_block_size": 512,
                    }
                },
            )

            smart = asyncio.run(service._build_esxi_slot_smart_summary(slot))

            self.assertTrue(smart.available)
            self.assertEqual(smart.transport_protocol, "SAS")
            self.assertEqual(smart.smart_health_status, "PASSED")
            self.assertEqual(smart.power_cycle_count, 29)
            self.assertEqual(smart.read_error_count, 444578)
            self.assertEqual(smart.write_error_count, 0)
            self.assertIsNone(smart.uncorrected_read_errors)
            self.assertIsNone(smart.uncorrected_write_errors)
            self.assertEqual(smart.bytes_written, 114814325279232)
            self.assertEqual(smart.bytes_read, 25282046342144)
            self.assertEqual(
                smart.message,
                "SMART data comes from ESXi, with drive health from the RAID controller (StorCLI).",
            )
            self.assertEqual(
                probe.commands,
                [
                    "esxcli storage core device smart get -d naa.5000cca07316765c",
                    "esxcli storage core device smart get -d vml.02000000005000cca07316765c483732343041",
                ],
            )

    def test_build_esxi_topology_members_uses_jbod_class_for_direct_member(self) -> None:
        member = InventoryService._build_esxi_topology_members(
            [
                DiskRecord(
                    raw={
                        "storcli_slot": "252:2",
                        "storcli_state": "JBOD",
                        "virtual_drive_names": [],
                    },
                    device_name="252:2",
                    path_device_name=None,
                    multipath_name=None,
                    multipath_member=None,
                    serial="SERIAL0",
                    model="H7240AS60SUN4.0T",
                    size_bytes=3638000000000,
                    identifier="SERIAL0",
                    health="ONLINE",
                    pool_name="ESXi local JBOD",
                    lunid="naa.5000cca07316765c",
                    bus="SAS",
                    temperature_c=34,
                    last_smart_test_type=None,
                    last_smart_test_status=None,
                    last_smart_test_lifetime_hours=None,
                    logical_block_size=512,
                    physical_block_size=512,
                    enclosure_id="supermicro-fat-twin-front-6",
                    slot=2,
                    smart_devices=["naa.5000cca07316765c"],
                    lookup_keys={"252:2", "serial0"},
                )
            ]
        )

        resolved = member["252:2"]
        self.assertEqual(resolved.pool_name, "ESXi local JBOD")
        self.assertEqual(resolved.vdev_class, "JBOD")
        self.assertEqual(resolved.topology_label, "ESXi local Enc > slot 252:2 > direct disk")
        self.assertEqual(resolved.health, "ONLINE")

    def test_build_esxi_disk_records_uses_selected_enclosure_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="generic-esxi",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                esxi_storcli_physical_drives=[
                    {
                        "slot_key": "252:0",
                        "enclosure_id": "252",
                        "slot": 0,
                        "controller_id": "c0",
                        "state": "JBOD",
                    }
                ]
            )

            records = service._build_esxi_disk_records(
                ssh_data,
                enclosure_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
            )

            self.assertEqual(records[0].enclosure_id, SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID)
            candidates = service._build_storage_view_candidate_records(
                TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                ),
                ssh_data,
                SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
            )
            self.assertEqual(candidates[0].enclosure_id, SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID)

    def test_build_esxi_topology_members_uses_parsed_virtual_drive_raid_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="generic-esxi",
                truenas=TrueNASConfig(platform="esxi"),
                default_profile_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                esxi_storcli_virtual_drives=[
                    {
                        "vd_id": "0",
                        "name": "data",
                        "raid": "RAID5",
                        "physical_drives": [
                            {"slot_key": "252:0", "enclosure_id": "252", "slot": 0},
                        ],
                    }
                ],
                esxi_storcli_physical_drives=[
                    {
                        "slot_key": "252:0",
                        "enclosure_id": "252",
                        "slot": 0,
                        "controller_id": "c0",
                        "state": "Onln",
                    }
                ],
            )

            record = service._build_esxi_disk_records(
                ssh_data,
                enclosure_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
            )[0]
            members = service._build_esxi_topology_members([record])

            self.assertEqual(record.raw["virtual_drive_raid_levels"], ["RAID5"])
            self.assertEqual(members["252:0"].vdev_class, "raid5")
            self.assertEqual(members["252:0"].topology_label, "data > RAID5 member > slot 252:0")

    def test_build_esxi_topology_members_does_not_invent_unknown_raid_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="generic-esxi",
                truenas=TrueNASConfig(platform="esxi"),
                default_profile_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            record = service._build_esxi_disk_records(
                ParsedSSHData(
                    esxi_storcli_physical_drives=[
                        {
                            "slot_key": "252:0",
                            "enclosure_id": "252",
                            "slot": 0,
                            "controller_id": "c0",
                            "state": "Onln",
                        }
                    ]
                ),
                enclosure_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
            )[0]

            member = service._build_esxi_topology_members([record])["252:0"]

            self.assertEqual(member.vdev_class, "raid")
            self.assertEqual(member.topology_label, "ESXi local RAID > RAID member > slot 252:0")

    def test_build_esxi_disk_records_qualifies_virtual_membership_by_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="multi-controller-esxi",
                truenas=TrueNASConfig(platform="esxi"),
                default_profile_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                esxi_storcli_virtual_drives=[
                    {
                        "controller_id": "c0",
                        "vd_id": "0",
                        "name": "boot",
                        "raid": "RAID1",
                        "physical_drives": [
                            {"controller_id": "c0", "slot_key": "252:0", "slot": 0},
                        ],
                    },
                    {
                        "controller_id": "c1",
                        "vd_id": "0",
                        "name": "data",
                        "raid": "RAID5",
                        "physical_drives": [
                            {"controller_id": "c1", "slot_key": "252:0", "slot": 0},
                        ],
                    },
                ],
                esxi_storcli_physical_drives=[
                    {
                        "controller_id": "c0",
                        "slot_key": "252:0",
                        "enclosure_id": "252",
                        "slot": 0,
                        "state": "Onln",
                    },
                    {
                        "controller_id": "c1",
                        "slot_key": "252:0",
                        "enclosure_id": "252",
                        "slot": 0,
                        "state": "Onln",
                    },
                ],
            )

            records = service._build_esxi_disk_records(
                ssh_data,
                enclosure_id=SUPERMICRO_FATTWIN_FRONT_6_PROFILE_ID,
            )
            by_controller = {record.raw["controller_id"]: record for record in records}

            self.assertEqual(by_controller["c0"].pool_name, "boot")
            self.assertEqual(by_controller["c0"].raw["virtual_drive_raid_levels"], ["RAID1"])
            self.assertEqual(by_controller["c1"].pool_name, "data")
            self.assertEqual(by_controller["c1"].raw["virtual_drive_raid_levels"], ["RAID5"])
            topology = service._build_esxi_topology_members(records)
            self.assertEqual(topology["c0:252:0"].pool_name, "boot")
            self.assertEqual(topology["c1:252:0"].pool_name, "data")

    def test_live_ses_empty_evidence_demotes_manual_mapping_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="linux-host",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            disk = service._build_disk_records(
                [
                    {
                        "name": "sda",
                        "devname": "sda",
                        "identifier": "{serial}SER-STALE",
                        "serial": "SER-STALE",
                        "model": "Synthetic Disk",
                    }
                ],
                ParsedSSHData(),
                {},
                {},
            )[0]
            mapping = ManualMapping(
                system_id=system.id,
                enclosure_id="enc-a",
                slot=0,
                serial="SER-STALE",
                gptid="gptid/stale",
                notes="operator calibration",
            )
            service.mapping_store.save_mapping(mapping)

            resolution = service._resolve_disk_for_slot(
                0,
                "enc-a",
                mapping,
                {"ser-stale": disk},
                {},
                {},
                {
                    "present": False,
                    "presence_source": "sg_ses_aes",
                    "ses_device": "/dev/sg4",
                    "sas_address_hint": "0",
                },
                ParsedSSHData(),
            )

            self.assertIsNone(resolution.disk)
            self.assertEqual(resolution.source, "ses-empty")
            self.assertTrue(resolution.stale_manual_mapping)
            persisted_mapping = service.mapping_store.get_mapping(system.id, "enc-a", 0)
            self.assertIsNotNone(persisted_mapping)
            self.assertEqual(persisted_mapping.serial if persisted_mapping else None, "SER-STALE")
            self.assertEqual(persisted_mapping.gptid if persisted_mapping else None, "gptid/stale")
            self.assertEqual(persisted_mapping.notes if persisted_mapping else None, "operator calibration")

            slot = service._build_slot_view(
                slot=0,
                row_index=0,
                column_index=0,
                enclosure_meta={"id": "enc-a", "label": "Shelf", "name": "Shelf"},
                raw_slot_status={
                    "present": False,
                    "presence_source": "sg_ses_aes",
                    "ses_device": "/dev/sg4",
                    "sas_address_hint": "0",
                },
                disk=resolution.disk,
                mapping=mapping,
                ssh_data=ParsedSSHData(),
                api_topology_members={},
                api_enclosure_ids=set(),
                resolution_source=resolution.source,
                stale_manual_mapping=resolution.stale_manual_mapping,
            )

            self.assertFalse(slot.present)
            self.assertEqual(slot.state, SlotState.empty)
            self.assertIsNone(slot.serial)
            self.assertIsNone(slot.gptid)
            self.assertEqual(slot.mapping_source, "ses-empty")
            self.assertIn("assigned by hand, but the enclosure reports it empty", slot.notes or "")
            self.assertTrue(slot.raw_status["stale_manual_mapping"])

    def test_disk_resolution_reports_the_evidence_tier_that_matched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="linux-host",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                Settings(systems=[system]),
                system,
                MagicMock(),
                MagicMock(),
                temp_dir,
            )
            disk = service._build_disk_records(
                [
                    {
                        "name": "sda",
                        "devname": "sda",
                        "identifier": "{serial}SER-A",
                        "serial": "SER-A",
                        "gptid": "gptid/disk-a",
                        "model": "Synthetic Disk",
                    }
                ],
                ParsedSSHData(),
                {},
                {},
            )[0]
            by_key = {key: disk for key in disk.lookup_keys}
            by_key.update({"sda": disk, "ser-a": disk, "gptid/disk-a": disk})
            cases = [
                (
                    "manual",
                    ManualMapping(slot=0, serial="SER-A"),
                    {},
                    {},
                    {},
                    ParsedSSHData(),
                    "manual",
                ),
                ("api-slot", None, {("enc-a", 0): disk}, {}, {}, ParsedSSHData(), "api-slot"),
                (
                    "enclosure-sysfs",
                    None,
                    {},
                    {},
                    {"device_names": ["sda"], "device_names_source": "enclosure_sysfs"},
                    ParsedSSHData(),
                    "enclosure-sysfs",
                ),
                ("device-hint", None, {}, {}, {"device_hint": "sda"}, ParsedSSHData(), "device-hint"),
                ("serial-hint", None, {}, {}, {"serial_hint": "SER-A"}, ParsedSSHData(), "serial-hint"),
                ("gptid-hint", None, {}, {}, {"gptid_hint": "gptid/disk-a"}, ParsedSSHData(), "gptid-hint"),
                (
                    "sas-address",
                    None,
                    {},
                    {"5000000000000001": disk},
                    {"sas_address_hint": "5000000000000001"},
                    ParsedSSHData(),
                    "sas-address",
                ),
                (
                    "ses-slot-map",
                    None,
                    {},
                    {},
                    {},
                    ParsedSSHData(ses_slot_to_device={0: "sda"}),
                    "ses-slot-map",
                ),
            ]

            for name, mapping, by_slot, by_sas, raw_status, ssh_data, expected in cases:
                with self.subTest(name=name):
                    resolution = service._resolve_disk_for_slot(
                        0,
                        "enc-a",
                        mapping,
                        by_key,
                        by_slot,
                        by_sas,
                        raw_status,
                        ssh_data,
                    )
                    self.assertIs(resolution.disk, disk)
                    self.assertEqual(resolution.source, expected)
                    self.assertFalse(resolution.stale_manual_mapping)

    def test_correlate_esxi_host_keeps_aoc_specific_storcli_warning_on_aoc_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="cryo-esxi",
                label="CryoStorage ESXi",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="10.88.88.20", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            warnings: list[str] = []

            _slots, _enclosures, _selected_meta, _layout_rows, _slot_count, _columns = service._correlate_esxi_host(
                ParsedSSHData(),
                warnings,
                None,
            )

            self.assertTrue(
                any(
                    "StorCLI did not return drive details, so the M.2 slots on this RAID card cannot be mapped."
                    in warning
                    for warning in warnings
                )
            )

    def test_build_esxi_platform_context_surfaces_useful_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="cryo-esxi",
                label="CryoStorage ESXi",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="10.88.88.20", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            ssh_data = ParsedSSHData(
                esxi_filesystems=[
                    {
                        "mount_point": "/vmfs/volumes/663fea18-16bffe1c-3a8c-ac1f6b406486",
                        "volume_name": "CryoStore-Local",
                        "uuid": "663fea18-16bffe1c-3a8c-ac1f6b406486",
                        "mounted": "true",
                        "type": "VMFS-6",
                        "size": "1892201529344",
                        "free": "415834832896",
                    }
                ],
                esxi_vmfs_extents=[
                    {
                        "volume_name": "CryoStore-Local",
                        "vmfs_uuid": "663fea18-16bffe1c-3a8c-ac1f6b406486",
                        "extent_number": "0",
                        "device_name": "naa.60030480208ba5992dd29c62f9ffa8f1",
                        "partition": "1",
                    }
                ],
                esxi_storcli_controller={
                    "Basics": {
                        "Model": "SAS 3808",
                        "Serial Number": "HA243S005874",
                        "SAS Address": "50030480208ba599",
                        "PCI Address": "00:03:00:00",
                    },
                    "Version": {
                        "Firmware Package Build": "52.28.0-5308",
                        "Firmware Version": "5.280.01-3972",
                        "Driver Name": "lsi_mr3",
                        "Driver Version": "7.727.02.00",
                    },
                    "Bus": {
                        "Host Interface": "PCI-E",
                        "Device Interface": "SAS-12G",
                    },
                    "Status": {
                        "Controller Status": "Optimal",
                    },
                },
                esxi_storcli_virtual_drives=[
                    {
                        "vd_id": "0",
                        "name": "ESXi",
                        "raid": "RAID1",
                        "state": "Optl",
                        "size": "100.000 GB",
                        "scsi_naa": "60030480208ba5992dd29badeb9f9403",
                        "physical_drives": [
                            {"slot_key": "13:0", "slot": 0, "enclosure_id": "13"},
                            {"slot_key": "13:1", "slot": 1, "enclosure_id": "13"},
                        ],
                        "detail": {
                            "Strip Size": "64 KB",
                            "Write Cache(initial setting)": "WriteThrough",
                            "Disk Cache Policy": "Disk's Default",
                            "Creation Date": "11-05-2024",
                            "Creation Time": "09:12:13 PM",
                            "Unmap Enabled": "No",
                        },
                    }
                ],
                esxi_storcli_physical_drives=[
                    {
                        "slot_key": "13:0",
                        "enclosure_id": "13",
                        "slot": 0,
                        "model": "Samsung SSD 970 EVO 2TB",
                        "firmware": "2B2QEXE7",
                        "connector_name": "C0 x4",
                        "connected_port": "0(path0)",
                        "link_speed": "8.0GT/s",
                        "unmap_capable": "Yes",
                        "detail": {
                            "WWN": "3025385981B2633B",
                            "Drive position": "DriveGroup:0, Span:0, Row:0",
                            "Write Cache": "Disabled",
                            "Max lane width": "4",
                            "FDE Type": "TCG Opal",
                            "SED Capable": "Yes",
                            "SED Enabled": "No",
                            "Secured": "No",
                            "Cryptographic Erase Capable": "Yes",
                            "Sanitize Support": "Not supported",
                            "Unmap Capable for LDs": "No",
                        },
                    }
                ],
            )

            context = service._build_esxi_platform_context(ssh_data)

            self.assertEqual(context["details"]["title"], "Platform Details")
            section_ids = [section["id"] for section in context["details"]["sections"]]
            self.assertEqual(
                section_ids,
                ["overview", "virtual-drives", "datastores", "member-capabilities"],
            )
            overview_entries = {entry["label"]: entry["value"] for entry in context["details"]["sections"][0]["entries"]}
            self.assertEqual(overview_entries["Controller Status"], "Optimal")
            self.assertEqual(overview_entries["Firmware Package"], "52.28.0-5308")
            virtual_drive_group = context["details"]["sections"][1]["groups"][0]
            self.assertEqual(virtual_drive_group["title"], "ESXi")
            self.assertIn("RAID1", virtual_drive_group["summary"])
            member_group = context["details"]["sections"][3]["groups"][0]
            self.assertEqual(member_group["title"], "Slot 00 (13:0)")
            member_entries = {entry["label"]: entry["value"] for entry in member_group["entries"]}
            self.assertEqual(member_entries["WWN"], "3025385981B2633B")
            self.assertEqual(member_entries["FDE Type"], "TCG Opal")

    def test_build_esxi_platform_context_hides_remote_san_and_falls_back_to_local_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="esxi-ft-node-2",
                label="esxi-ft-node-2",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True, host="192.0.2.121", user="root"),
            )
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            ssh_data = ParsedSSHData(
                esxi_storage_devices=[
                    {
                        "id": "t10.ATA_____SATADOM_LOCAL",
                        "display_name": "Local ATA Disk (t10.ATA_____SATADOM_LOCAL)",
                        "is_local": "true",
                        "is_ssd": "true",
                        "is_boot_device": "false",
                        "devfs_path": "/vmfs/devices/disks/t10.ATA_____SATADOM_LOCAL",
                        "vendor": "ATA",
                        "model": "SATADOM-SL 3TE7",
                        "revision": "04B",
                        "size": "114473",
                        "device_type": "Direct-Access",
                        "multipath_plugin": "HPP",
                        "status": "on",
                    },
                    {
                        "id": "naa.6589cfc000000c31f0073f88a08ea329",
                        "display_name": "TrueNAS iSCSI Disk (naa.6589cfc000000c31f0073f88a08ea329)",
                        "is_local": "false",
                        "is_ssd": "false",
                        "devfs_path": "/vmfs/devices/disks/naa.6589cfc000000c31f0073f88a08ea329",
                        "vendor": "TrueNAS",
                        "model": "iSCSI Disk",
                        "size": "8388608",
                        "device_type": "Direct-Access",
                        "multipath_plugin": "NMP",
                    },
                    {
                        "id": "naa.5006016844602c9d",
                        "display_name": "DGC Fibre Channel Disk (naa.5006016844602c9d)",
                        "is_local": "false",
                        "devfs_path": "/vmfs/devices/disks/naa.5006016844602c9d",
                        "vendor": "DGC",
                        "model": "VRAID",
                        "size": "4194304",
                        "device_type": "Direct-Access",
                        "multipath_plugin": "NMP",
                    },
                ],
                esxi_filesystems=[
                    {
                        "mount_point": "/vmfs/volumes/local-store",
                        "volume_name": "Local-Store",
                        "mounted": "true",
                        "type": "VMFS-6",
                        "size": "104857600",
                        "free": "52428800",
                    },
                    {
                        "mount_point": "/vmfs/volumes/san-store",
                        "volume_name": "SAN-Store",
                        "mounted": "true",
                        "type": "VMFS-6",
                        "size": "209715200",
                        "free": "104857600",
                    },
                    {
                        "mount_point": "/vmfs/volumes/fc-store",
                        "volume_name": "FC-Store",
                        "mounted": "true",
                        "type": "VMFS-6",
                        "size": "209715200",
                        "free": "104857600",
                    },
                ],
                esxi_vmfs_extents=[
                    {
                        "volume_name": "Local-Store",
                        "device_name": "t10.ATA_____SATADOM_LOCAL",
                        "partition": "1",
                    },
                    {
                        "volume_name": "SAN-Store",
                        "device_name": "naa.6589cfc000000c31f0073f88a08ea329",
                        "partition": "1",
                    },
                    {
                        "volume_name": "FC-Store",
                        "device_name": "naa.5006016844602c9d",
                        "partition": "1",
                    },
                ],
            )

            context = service._build_esxi_platform_context(ssh_data)

            self.assertEqual(context["details"]["title"], "Platform Details")
            self.assertIn("StorCLI physical-member detail is unavailable", context["details"]["summary"])
            self.assertIn("Remote iSCSI / FC presentations are hidden.", context["details"]["summary"])
            section_ids = [section["id"] for section in context["details"]["sections"]]
            self.assertEqual(section_ids, ["datastores", "local-devices"])
            datastore_section = context["details"]["sections"][0]
            self.assertEqual(datastore_section["summary"], "1 local shown · 2 remote SAN omitted · Local-Store")
            self.assertEqual([group["title"] for group in datastore_section["groups"]], ["Local-Store"])
            local_devices_section = context["details"]["sections"][1]
            self.assertEqual(local_devices_section["summary"], "1 local shown · 2 remote SAN omitted")
            self.assertEqual(len(local_devices_section["groups"]), 1)
            self.assertEqual(
                local_devices_section["groups"][0]["title"],
                "Local ATA Disk (t10.ATA_____SATADOM_LOCAL)",
            )

    def test_esxi_inferred_aoc_storage_view_reuses_storcli_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(id="cryo-esxi", label="CryoStorage ESXi", truenas=TrueNASConfig(platform="esxi"))
            settings = Settings(systems=[system])
            service = build_inventory_service(settings, system, MagicMock(), MagicMock(), temp_dir)
            profile = service.profile_registry.get(ESXI_AOC_SLG4_2H8M2_PROFILE_ID)
            storage_views = resolve_system_storage_views(system, service.profile_registry)
            snapshot = InventorySnapshot(
                slots=[],
                layout_rows=[[1], [0]],
                layout_slot_count=2,
                layout_columns=1,
                refresh_interval_seconds=30,
                selected_system_id=system.id,
                selected_system_label=system.label,
                selected_system_platform="esxi",
                selected_enclosure_id=ESXI_AOC_SLG4_2H8M2_PROFILE_ID,
                selected_enclosure_label="AOC-SLG4-2H8M2",
                selected_profile=profile,
            )
            candidates = [
                {
                    "candidate_id": "SERIAL0",
                    "serial": "SERIAL0",
                    "device_names": ["13:0"],
                    "transport_address": "C0 x4",
                    "snapshot_slot": 0,
                },
                {
                    "candidate_id": "SERIAL1",
                    "serial": "SERIAL1",
                    "device_names": ["13:1"],
                    "transport_address": "C1 x4",
                    "snapshot_slot": 1,
                },
            ]

            runtime_views = service._serialize_storage_views_runtime(
                snapshot,
                storage_views,
                {snapshot.selected_enclosure_id: snapshot},
                {snapshot.selected_enclosure_id: candidates},
            )
            aoc_view = next(view for view in runtime_views if view.id == "aoc-slg4-2h8m2")

            self.assertEqual(aoc_view.template_id, "aoc-slg4-2h8m2-2")
            self.assertFalse(aoc_view.render.show_in_main_ui)
            self.assertEqual(aoc_view.slot_layout, [[1], [0]])
            self.assertEqual(aoc_view.matched_count, 2)
            self.assertEqual([slot.slot_label for slot in aoc_view.slots], ["M2-1", "M2-2"])
            self.assertEqual([slot.snapshot_slot for slot in aoc_view.slots], [0, 1])
