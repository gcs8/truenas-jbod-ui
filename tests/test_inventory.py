from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import (
    InventorySnapshot,
    LedAction,
    ManualMapping,
    MappingBundle,
    MultipathView,
    SlotState,
    SlotView,
    SmartSummaryView,
    StorageViewRuntimePayload,
    StorageViewRuntimeSlot,
    StorageViewRuntimeView,
)
from app.services.inventory import DiskRecord, InventoryService, InventorySourceBundle, build_lunid_aliases, infer_slot_count_from_layout, resolve_persistent_id
from app.services.mapping_store import MappingStore
from app.services.parsers import (
    ParsedSSHData,
    SESMapEnclosure,
    SESMapSlot,
    ZpoolMember,
    build_slot_candidates_from_ses_enclosures,
    parse_ssh_outputs,
)
from app.services.profile_registry import ProfileRegistry
from app.services.profile_registry import (
    CORE_CSE_946_PROFILE_ID,
    SCALE_SSG_FRONT_24_PROFILE_ID,
    UNIFI_UNVR_FRONT_4_PROFILE_ID,
    UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
)
from app.services.ssh_probe import SSHCommandResult
from app.services.slot_detail_store import SlotDetailCacheEntry, SlotDetailStore
from app.services.truenas_ws import TrueNASAPIError, TrueNASRawData


def build_inventory_service(
    settings: Settings,
    system: SystemConfig,
    truenas_client,
    ssh_probe,
    temp_dir: str,
) -> InventoryService:
    return InventoryService(
        settings,
        system,
        truenas_client,
        ssh_probe,
        MappingStore(f"{temp_dir}\\slot_mappings.json"),
        ProfileRegistry(settings),
        SlotDetailStore(f"{temp_dir}\\slot_detail_cache.json"),
    )


class InventoryHelpersTests(unittest.TestCase):
    def test_infer_slot_count_from_layout_ignores_gap_cells(self) -> None:
        layout_rows = [
            [88, 89, 90, 91, 92, 93, None, None, 94, 95, 96, 97, 98, 99],
            [76, 77, 78, 79, 80, 81, None, None, 82, 83, 84, 85, 86, 87],
            [64, 65, 66, 67, 68, 69, None, None, 70, 71, 72, 73, 74, 75],
            [52, 53, 54, 55, 56, 57, None, None, 58, 59, 60, 61, 62, 63],
            [40, 41, 42, 43, 44, 45, None, None, 46, 47, 48, 49, 50, 51],
            list(range(28, 42)),
            list(range(14, 28)),
            list(range(0, 14)),
        ]

        self.assertEqual(infer_slot_count_from_layout(layout_rows), 100)

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

    def test_smart_candidate_devices_ignores_placeholder_hctl_labels(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            device_name="7:0:0:0",
        )

        self.assertEqual(InventoryService._smart_candidate_devices(slot), [])

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


class InventoryStorageViewCandidateTests(unittest.TestCase):
    def test_get_storage_view_candidates_excludes_disks_already_tied_to_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )

            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[
                        SlotView(
                            slot=0,
                            slot_label="00",
                            row_index=0,
                            column_index=0,
                            device_name="da0",
                            serial="SER-DA0",
                        )
                    ],
                    refresh_interval_seconds=30,
                    selected_system_id="archive-core",
                    selected_system_label="Archive CORE",
                )
            )
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[],
                        disks=[],
                        pools=[],
                        disk_temperatures={},
                        smart_test_results=[],
                    ),
                    ssh_outputs={},
                    ssh_collected=True,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            )
            service._build_storage_view_candidate_records = MagicMock(
                return_value=[
                    DiskRecord(
                        raw={},
                        device_name="da0",
                        path_device_name="da0",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-DA0",
                        model="Front Disk",
                        size_bytes=10_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="archive",
                        lunid=None,
                        bus="SATA",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["da0"],
                        lookup_keys={"da0", "ser-da0"},
                    ),
                    DiskRecord(
                        raw={"transport_address": "0000:5e:00.0"},
                        device_name="nvd0",
                        path_device_name="nvd0",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-NVME-1",
                        model="Samsung 970 EVO",
                        size_bytes=2_000_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="fast",
                        lunid=None,
                        bus="NVME",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["nvd0"],
                        lookup_keys={"nvd0", "ser-nvme-1", "0000:5e:00.0"},
                    ),
                ]
            )

            candidates = asyncio.run(service.get_storage_view_candidates())

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["serial"], "SER-NVME-1")
        self.assertEqual(candidates[0]["transport_address"], "0000:5e:00.0")
        self.assertEqual(candidates[0]["recommended_binding"]["serials"], ["SER-NVME-1"])
        self.assertEqual(candidates[0]["recommended_binding"]["device_names"], ["nvd0"])

    def test_get_storage_view_runtime_orders_internal_matches_by_saved_binding_hints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                storage_views=[
                    {
                        "id": "nvme-carrier",
                        "label": "4x NVMe Carrier Card",
                        "kind": "nvme_carrier",
                        "template_id": "nvme-carrier-4",
                        "enabled": True,
                        "order": 20,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "serial",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": ["nvd2", "nvd0"],
                        },
                        "layout_overrides": {
                            "slot_sizes": {
                                0: "2280",
                                1: "2280",
                                2: "2280",
                                3: "22110",
                            }
                        },
                    },
                    {
                        "id": "boot-doms",
                        "label": "Boot SATADOMs",
                        "kind": "boot_devices",
                        "template_id": "satadom-pair-2",
                        "enabled": True,
                        "order": 30,
                        "render": {
                            "show_in_main_ui": False,
                            "show_in_admin_ui": True,
                            "default_collapsed": True,
                        },
                        "binding": {
                            "mode": "serial",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": ["ada0", "ada1"],
                        },
                    },
                ],
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )

            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[],
                    refresh_interval_seconds=30,
                    selected_system_id="archive-core",
                    selected_system_label="Archive CORE",
                    selected_enclosure_id="ses-combined-60",
                    selected_enclosure_label="LSI-F SAS3x48Front 0c04 + LSI-R SAS3x48Rear 0c04",
                )
            )
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[],
                        disks=[],
                        pools=[],
                        disk_temperatures={},
                        smart_test_results=[],
                    ),
                    ssh_outputs={},
                    ssh_collected=True,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            )
            service._build_storage_view_candidate_records = MagicMock(
                return_value=[
                    DiskRecord(
                        raw={"transport_address": "0000:5f:00.0"},
                        device_name="nvd0",
                        path_device_name="nvd0",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-NVME-0",
                        model="Samsung NVMe 0",
                        size_bytes=2_000_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="flash",
                        lunid=None,
                        bus="NVME",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["nvd0"],
                        lookup_keys={"nvd0", "ser-nvme-0"},
                    ),
                    DiskRecord(
                        raw={"transport_address": "0000:5e:00.0"},
                        device_name="nvd2",
                        path_device_name="nvd2",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-NVME-2",
                        model="Samsung NVMe 2",
                        size_bytes=2_000_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="flash",
                        lunid=None,
                        bus="NVME",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["nvd2"],
                        lookup_keys={"nvd2", "ser-nvme-2"},
                    ),
                    DiskRecord(
                        raw={},
                        device_name="ada0",
                        path_device_name="ada0",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-DOM-0",
                        model="SATADOM 0",
                        size_bytes=64_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="freenas-boot",
                        lunid=None,
                        bus="SATA",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["ada0"],
                        lookup_keys={"ada0", "ser-dom-0"},
                    ),
                    DiskRecord(
                        raw={},
                        device_name="ada1",
                        path_device_name="ada1",
                        multipath_name=None,
                        multipath_member=None,
                        serial="SER-DOM-1",
                        model="SATADOM 1",
                        size_bytes=64_000_000_000,
                        identifier=None,
                        health="ONLINE",
                        pool_name="freenas-boot",
                        lunid=None,
                        bus="SATA",
                        temperature_c=None,
                        last_smart_test_type=None,
                        last_smart_test_status=None,
                        last_smart_test_lifetime_hours=None,
                        logical_block_size=None,
                        physical_block_size=None,
                        enclosure_id=None,
                        slot=None,
                        smart_devices=["ada1"],
                        lookup_keys={"ada1", "ser-dom-1"},
                    ),
                ]
            )

            runtime = asyncio.run(service.get_storage_view_runtime())

        self.assertEqual(runtime.system_id, "archive-core")
        self.assertEqual(len(runtime.views), 2)
        nvme_view = next(view for view in runtime.views if view.id == "nvme-carrier")
        self.assertEqual(nvme_view.slot_layout, [[3], [2], [1], [0]])
        self.assertEqual([slot.slot_label for slot in nvme_view.slots[:2]], ["M2-1", "M2-2"])
        self.assertEqual([slot.device_name for slot in nvme_view.slots[:2]], ["nvd2", "nvd0"])
        self.assertEqual([slot.placement_key for slot in nvme_view.slots[:2]], ["device match: nvd2", "device match: nvd0"])
        self.assertEqual([slot.slot_size for slot in nvme_view.slots], ["2280", "2280", "2280", "22110"])
        self.assertEqual(nvme_view.backing_enclosure_id, "ses-combined-60")
        self.assertEqual(nvme_view.backing_enclosure_label, "LSI-F SAS3x48Front 0c04 + LSI-R SAS3x48Rear 0c04")
        satadom_view = next(view for view in runtime.views if view.id == "boot-doms")
        self.assertEqual([slot.slot_label for slot in satadom_view.slots], ["DOM-A", "DOM-B"])
        self.assertEqual([slot.device_name for slot in satadom_view.slots], ["ada0", "ada1"])

    def test_get_storage_view_runtime_uses_saved_ses_profile_instead_of_live_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                storage_views=[
                    {
                        "id": "front-24",
                        "label": "Front 24 Bay",
                        "kind": "ses_enclosure",
                        "template_id": "ses-auto",
                        "profile_id": "generic-front-24-1x24",
                        "enabled": True,
                        "order": 10,
                        "render": {
                            "show_in_main_ui": True,
                            "show_in_admin_ui": True,
                            "default_collapsed": False,
                        },
                        "binding": {
                            "mode": "auto",
                            "enclosure_ids": [],
                            "pool_names": [],
                            "serials": [],
                            "pcie_addresses": [],
                            "device_names": [],
                        },
                    }
                ],
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            live_profile = service.profile_registry.get(CORE_CSE_946_PROFILE_ID)

            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[
                        SlotView(
                            slot=0,
                            slot_label="00",
                            row_index=0,
                            column_index=0,
                            present=True,
                            state=SlotState.healthy,
                            device_name="da0",
                        ),
                        SlotView(
                            slot=23,
                            slot_label="23",
                            row_index=0,
                            column_index=23,
                            present=True,
                            state=SlotState.healthy,
                            device_name="da23",
                        ),
                    ],
                    refresh_interval_seconds=30,
                    selected_system_id="archive-core",
                    selected_system_label="Archive CORE",
                    selected_enclosure_id="ses-combined-60",
                    selected_enclosure_label="Combined 60 Bay",
                    selected_profile=live_profile,
                )
            )
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[],
                        disks=[],
                        pools=[],
                        disk_temperatures={},
                        smart_test_results=[],
                    ),
                    ssh_outputs={},
                    ssh_collected=True,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            )
            service._build_storage_view_candidate_records = MagicMock(return_value=[])

            runtime = asyncio.run(service.get_storage_view_runtime())

        saved_view = next(view for view in runtime.views if view.id == "front-24")
        self.assertEqual(saved_view.profile_id, "generic-front-24-1x24")
        self.assertEqual(saved_view.profile_label, "Generic Front 24")
        self.assertEqual(saved_view.slot_count, 24)
        self.assertEqual(saved_view.slot_layout, [list(range(24))])
        self.assertEqual(saved_view.slots[0].slot_label, "00")
        self.assertEqual(saved_view.slots[-1].slot_label, "23")
        self.assertIn("Generic Front 24", saved_view.notes[0])

    def test_get_storage_view_slot_smart_summary_reuses_snapshot_slot_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            service.get_storage_view_runtime = AsyncMock(
                return_value=StorageViewRuntimePayload(
                    system_id="archive-core",
                    views=[
                        StorageViewRuntimeView(
                            id="primary-chassis",
                            label="Primary Chassis",
                            kind="ses_enclosure",
                            template_id="ses-auto",
                            source="selected_enclosure_snapshot",
                            backing_enclosure_id="enc-a",
                            slots=[
                                StorageViewRuntimeSlot(
                                    slot_index=0,
                                    slot_label="00",
                                    occupied=True,
                                    source="snapshot_slot",
                                    snapshot_slot=12,
                                    device_name="da12",
                                )
                            ],
                        )
                    ],
                )
            )
            expected_summary = SmartSummaryView(available=True, power_on_hours=1200)
            service.get_slot_smart_summary = AsyncMock(return_value=expected_summary)

            summary = asyncio.run(
                service.get_storage_view_slot_smart_summary(
                    "primary-chassis",
                    0,
                    selected_enclosure_id="enc-a",
                    allow_stale_cache=True,
                )
            )

        self.assertIs(summary, expected_summary)
        service.get_slot_smart_summary.assert_awaited_once_with(
            12,
            selected_enclosure_id="enc-a",
            allow_stale_cache=True,
        )

    def test_get_storage_view_slot_smart_summary_builds_synthetic_slot_for_inventory_bound_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            service.get_storage_view_runtime = AsyncMock(
                return_value=StorageViewRuntimePayload(
                    system_id="archive-core",
                    views=[
                        StorageViewRuntimeView(
                            id="boot-doms",
                            label="Boot SATADOMs",
                            kind="boot_devices",
                            template_id="satadom-pair-2",
                            source="inventory_binding",
                            backing_enclosure_id="enc-a",
                            slots=[
                                StorageViewRuntimeSlot(
                                    slot_index=0,
                                    slot_label="DOM-A",
                                    occupied=True,
                                    source="inventory_candidate",
                                    assignment_rank=1,
                                    placement_key="device match: ada0",
                                    device_name="ada0",
                                    smart_device_names=["ada0"],
                                    serial="SER-DOM-0",
                                    pool_name="freenas-boot",
                                    model="SATADOM 0",
                                    size_bytes=64_000_000_000,
                                    size_human="59.6 GiB",
                                    gptid="gptid/dom-a",
                                    health="ONLINE",
                                    temperature_c=31,
                                    logical_block_size=512,
                                    physical_block_size=4096,
                                    slot_size="dom",
                                )
                            ],
                        )
                    ],
                )
            )
            expected_summary = SmartSummaryView(available=True, temperature_c=31, power_on_hours=24)
            service._get_slot_smart_summary_for_slot_view = AsyncMock(return_value=expected_summary)

            summary = asyncio.run(
                service.get_storage_view_slot_smart_summary(
                    "boot-doms",
                    0,
                    selected_enclosure_id="enc-a",
                    allow_stale_cache=True,
                )
            )

        self.assertIs(summary, expected_summary)
        synthetic_slot = service._get_slot_smart_summary_for_slot_view.await_args.args[0]
        self.assertEqual(synthetic_slot.enclosure_id, "storage-view:boot-doms")
        self.assertEqual(synthetic_slot.enclosure_label, "Boot SATADOMs")
        self.assertEqual(synthetic_slot.device_name, "ada0")
        self.assertEqual(synthetic_slot.smart_device_names, ["ada0"])
        self.assertEqual(synthetic_slot.pool_name, "freenas-boot")
        self.assertEqual(synthetic_slot.gptid, "gptid/dom-a")
        self.assertEqual(synthetic_slot.logical_block_size, 512)
        self.assertEqual(synthetic_slot.physical_block_size, 4096)

    def test_resolve_storage_view_slot_history_target_reuses_snapshot_slot_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            service.get_storage_view_runtime = AsyncMock(
                return_value=StorageViewRuntimePayload(
                    system_id="archive-core",
                    views=[
                        StorageViewRuntimeView(
                            id="primary-chassis",
                            label="Primary Chassis",
                            kind="ses_enclosure",
                            template_id="ses-auto",
                            source="selected_enclosure_snapshot",
                            backing_enclosure_id="enc-a",
                            slots=[
                                StorageViewRuntimeSlot(
                                    slot_index=0,
                                    slot_label="00",
                                    occupied=True,
                                    source="snapshot_slot",
                                    snapshot_slot=12,
                                    device_name="da12",
                                )
                            ],
                        )
                    ],
                )
            )

            target = asyncio.run(
                service.resolve_storage_view_slot_history_target(
                    "primary-chassis",
                    0,
                    selected_enclosure_id="enc-a",
                )
            )

        self.assertEqual(target, (12, "enc-a"))

    def test_resolve_storage_view_slot_history_target_uses_storage_view_scope_for_inventory_bound_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
            )
            service = build_inventory_service(settings, system, AsyncMock(), AsyncMock(), temp_dir)
            service.get_storage_view_runtime = AsyncMock(
                return_value=StorageViewRuntimePayload(
                    system_id="archive-core",
                    views=[
                        StorageViewRuntimeView(
                            id="boot-doms",
                            label="Boot SATADOMs",
                            kind="boot_devices",
                            template_id="satadom-pair-2",
                            source="inventory_binding",
                            backing_enclosure_id="enc-a",
                            slots=[
                                StorageViewRuntimeSlot(
                                    slot_index=0,
                                    slot_label="DOM-A",
                                    occupied=True,
                                    source="inventory_candidate",
                                    device_name="ada0",
                                    serial="SER-DOM-0",
                                )
                            ],
                        )
                    ],
                )
            )

            target = asyncio.run(
                service.resolve_storage_view_slot_history_target(
                    "boot-doms",
                    0,
                    selected_enclosure_id="enc-a",
                )
            )

        self.assertEqual(target, (0, "storage-view:boot-doms"))

    def test_resolve_persistent_id_prefers_partuuid_leaf(self) -> None:
        value, label = resolve_persistent_id("/dev/disk/by-partuuid/83672e59-1b7c-40a0-970a-15ad0776ddda")

        self.assertEqual(value, "83672e59-1b7c-40a0-970a-15ad0776ddda")
        self.assertEqual(label, "PARTUUID")

    def test_resolve_persistent_id_preserves_wwn_leaf(self) -> None:
        value, label = resolve_persistent_id("/dev/disk/by-id/wwn-0x5000c5003e8253a7")

        self.assertEqual(value, "wwn-0x5000c5003e8253a7")
        self.assertEqual(label, "WWN")

    def test_resolve_persistent_id_preserves_eui_leaf(self) -> None:
        value, label = resolve_persistent_id("eui.000000000000001000a075012b91c7cf")

        self.assertEqual(value, "eui.000000000000001000a075012b91c7cf")
        self.assertEqual(label, "EUI64")

    def test_extract_quantastor_slot_normalizes_mixed_zero_padded_values(self) -> None:
        self.assertEqual(InventoryService._extract_quantastor_slot({"slot": "01"}), 0)
        self.assertEqual(InventoryService._extract_quantastor_slot({"slot": "08"}), 7)
        self.assertEqual(InventoryService._extract_quantastor_slot({"slot": "0"}), 0)
        self.assertEqual(InventoryService._extract_quantastor_slot({"slot": "2"}), 2)
        self.assertEqual(InventoryService._extract_quantastor_slot({"slot": "12"}), 12)

    def test_status_contains_ignores_nested_key_names_when_value_is_false(self) -> None:
        raw_status = {
            "status": "Ready (RDY)",
            "disk_raw": {
                "isFaulty": False,
                "smartHealthTest": "OK",
            },
        }

        self.assertFalse(InventoryService._status_contains(raw_status, "fault"))

    def test_build_linux_disk_records_prefers_primary_namespace_persistent_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="gpu-server",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "nvme0n1",
                        "serial": "20452B91C7CF",
                        "model": "Micron_9300_MTFDHAL7T6TDP",
                        "size": "256G",
                        "log-sec": 4096,
                        "phy-sec": 4096,
                        "tran": "nvme",
                        "wwn": "eui.000000000000000300a075012b91c7cf",
                        "children": [],
                    },
                    {
                        "name": "nvme0n2",
                        "serial": "20452B91C7CF",
                        "model": "Micron_9300_MTFDHAL7T6TDP",
                        "size": "1.7T",
                        "log-sec": 4096,
                        "phy-sec": 4096,
                        "tran": "nvme",
                        "wwn": "eui.000000000000001000a075012b91c7cf",
                        "children": [
                            {
                                "name": "md1",
                                "type": "raid1",
                                "children": [
                                    {
                                        "name": "md5",
                                        "type": "raid0",
                                        "mountpoint": "/mnt/nvme_raid",
                                    }
                                ],
                            }
                        ],
                    },
                ],
                linux_nvme_subsystems={
                    "nvme0": {
                        "transport": "pcie",
                        "address": "10000:01:00.0",
                    }
                },
            )

            records = service._build_linux_disk_records(ssh_data)

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.device_name, "nvme0")
            self.assertEqual(record.path_device_name, "nvme0n2")
            self.assertEqual(record.identifier, "eui.000000000000001000a075012b91c7cf")
            self.assertEqual(record.pool_name, "/mnt/nvme_raid")
            self.assertEqual(record.smart_devices[0], "nvme0n2")

    def test_build_linux_disk_records_supports_sata_disks_with_hctl_lookup_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sda",
                        "type": "disk",
                        "path": "/dev/sda",
                        "hctl": "0:0:0:0",
                        "serial": "ZC14D9W1",
                        "model": "ST4000NM0115-1YZ107",
                        "size": "3.7T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "wwn": "0x5000c500abcd0001",
                        "children": [
                            {
                                "name": "md3",
                                "type": "raid5",
                                "mountpoint": "/volume1",
                            }
                        ],
                    },
                    {
                        "name": "sdb",
                        "type": "disk",
                        "path": "/dev/sdb",
                        "hctl": "2:0:0:0",
                        "serial": "ZC14DAGA",
                        "model": "ST4000NM0115-1YZ107",
                        "size": "3.7T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "wwn": "0x5000c500abcd0002",
                        "children": [
                            {
                                "name": "md3",
                                "type": "raid5",
                                "mountpoint": "/volume1",
                            }
                        ],
                    },
                ],
            )

            records = service._build_linux_disk_records(ssh_data)

            self.assertEqual(len(records), 2)
            first = records[0]
            self.assertEqual(first.device_name, "sda")
            self.assertEqual(first.path_device_name, "sda")
            self.assertEqual(first.pool_name, "/volume1")
            self.assertEqual(first.bus, "SATA")
            self.assertEqual(first.smart_devices, ["sda"])
            self.assertIn("0:0:0:0", first.lookup_keys)
            self.assertIn("ZC14D9W1".lower(), first.lookup_keys)

    def test_build_linux_disk_records_prefers_largest_data_volume_over_boot_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="linux-md-lvm",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sda",
                        "type": "disk",
                        "path": "/dev/sda",
                        "hctl": "0:0:0:0",
                        "serial": "LINUXDATA01",
                        "model": "GenericDisk",
                        "size": "1.9T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [
                            {
                                "name": "sda1",
                                "type": "part",
                                "path": "/dev/sda1",
                                "size": "1G",
                                "mountpoint": "/boot",
                            },
                            {
                                "name": "sda2",
                                "type": "part",
                                "path": "/dev/sda2",
                                "size": "8G",
                                "fstype": "swap",
                                "mountpoint": "[SWAP]",
                            },
                            {
                                "name": "sda3",
                                "type": "part",
                                "path": "/dev/sda3",
                                "size": "1.8T",
                                "children": [
                                    {
                                        "name": "vg0-data",
                                        "type": "lvm",
                                        "path": "/dev/mapper/vg0-data",
                                        "size": "1.8T",
                                        "mountpoint": "/srv/media",
                                    }
                                ],
                            },
                        ],
                    }
                ],
            )

            records = service._build_linux_disk_records(ssh_data)

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.pool_name, "/srv/media")
            self.assertEqual(record.raw["top_array_name"], "vg0-data")
            self.assertEqual(record.raw["top_mountpoint"], "/srv/media")
            self.assertEqual(record.raw["top_role"], "data")

    def test_build_linux_disk_records_preserves_ubntstorage_vendor_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr-pro",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sda",
                        "type": "disk",
                        "path": "/dev/sda",
                        "hctl": "5:0:0:0",
                        "serial": "Y5F2A056FJKH",
                        "model": "TOSHIBA_MG09ACA16TE",
                        "size": "14.6T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [],
                    }
                ],
                ubntstorage_disks=[
                    {
                        "node": "sda",
                        "slot": 2,
                        "healthy": "optimal",
                        "model": "TOSHIBA MG09ACA16TE",
                        "serial": "Y5F2A056FJKH",
                    }
                ],
            )

            records = service._build_linux_disk_records(ssh_data)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].raw["vendor_slot"], 1)
            self.assertEqual(records[0].health, "optimal")

    def test_correlate_linux_host_uses_ubntstorage_slots_and_marks_nodisk_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr-pro",
                label="UniFi UNVR Pro",
                default_profile_id=UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sda",
                        "type": "disk",
                        "path": "/dev/sda",
                        "hctl": "5:0:0:0",
                        "serial": "Y5F2A056FJKH",
                        "model": "TOSHIBA_MG09ACA16TE",
                        "size": "14.6T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [],
                    },
                    {
                        "name": "sdb",
                        "type": "disk",
                        "path": "/dev/sdb",
                        "hctl": "7:0:0:0",
                        "serial": "Y5F2A056FJKK",
                        "model": "TOSHIBA_MG09ACA16TE",
                        "size": "14.6T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [],
                    },
                ],
                ubntstorage_disks=[
                    {"node": "sdb", "slot": 1, "healthy": "optimal", "state": "ready", "size": 16000000000000},
                    {"node": "sda", "slot": 2, "healthy": "optimal", "state": "ready", "size": 16000000000000},
                    {"slot": 3, "healthy": "none", "state": "nodisk"},
                    {"slot": 4, "healthy": "none", "state": "nodisk"},
                    {"slot": 5, "healthy": "none", "state": "nodisk"},
                    {"slot": 6, "healthy": "none", "state": "nodisk"},
                    {"slot": 7, "healthy": "none", "state": "nodisk"},
                ],
            )
            warnings: list[str] = []

            slot_views, _available, _meta, _rows, slot_count, _columns = service._correlate_linux_host(
                ssh_data,
                warnings,
                None,
            )

            self.assertEqual(slot_count, 7)
            self.assertEqual(slot_views[0].device_name, "sdb")
            self.assertEqual(slot_views[1].device_name, "sda")
            self.assertFalse(slot_views[2].present)
            self.assertEqual(slot_views[2].state.value, "empty")
            self.assertEqual(slot_views[2].mapping_source, "ssh")
            self.assertTrue(any("UniFi UNVR Pro LED control is experimental." in warning for warning in warnings))

    def test_correlate_linux_host_enables_unvr_led_backend_and_gpio_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr",
                label="UniFi UNVR",
                default_profile_id=UNIFI_UNVR_FRONT_4_PROFILE_ID,
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True, user="root"),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sdd",
                        "type": "disk",
                        "path": "/dev/sdd",
                        "hctl": "6:0:0:0",
                        "serial": "ZC14DAYS",
                        "model": "ST4000NM0115-1YZ107",
                        "size": "3.6T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [],
                    }
                ],
                ubntstorage_disks=[
                    {"node": "sdd", "slot": 1, "healthy": "good", "state": "normal", "size": 4000787030016},
                    {"slot": 2, "healthy": "none", "state": "nodisk"},
                    {"slot": 3, "healthy": "none", "state": "nodisk"},
                    {"slot": 4, "healthy": "none", "state": "nodisk"},
                ],
                unifi_led_states={0: True},
            )

            slot_views, _available, _meta, _rows, slot_count, _columns = service._correlate_linux_host(
                ssh_data,
                [],
                None,
            )

            self.assertEqual(slot_count, 4)
            self.assertEqual(slot_views[0].device_name, "sdd")
            self.assertEqual(slot_views[0].led_backend, "unifi_fault")
            self.assertTrue(slot_views[0].led_supported)
            self.assertTrue(slot_views[0].identify_active)

    def test_correlate_linux_host_enables_unvr_pro_led_backend_as_experimental(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr-pro",
                label="UniFi UNVR Pro",
                default_profile_id=UNIFI_UNVR_PRO_FRONT_7_PROFILE_ID,
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True, user="root"),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                linux_blockdevices=[
                    {
                        "name": "sdb",
                        "type": "disk",
                        "path": "/dev/sdb",
                        "hctl": "7:0:0:0",
                        "serial": "Y5E2A0BSFJKH",
                        "model": "TOSHIBA_MG09ACA16TE",
                        "size": "14.0T",
                        "log-sec": 512,
                        "phy-sec": 4096,
                        "tran": "sata",
                        "children": [],
                    }
                ],
                ubntstorage_disks=[
                    {"node": "sdb", "slot": 1, "healthy": "good", "state": "normal", "size": 16000900661248},
                    {"slot": 2, "healthy": "none", "state": "nodisk"},
                    {"slot": 3, "healthy": "none", "state": "nodisk"},
                    {"slot": 4, "healthy": "none", "state": "nodisk"},
                    {"slot": 5, "healthy": "none", "state": "nodisk"},
                    {"slot": 6, "healthy": "none", "state": "nodisk"},
                    {"slot": 7, "healthy": "none", "state": "nodisk"},
                ],
                unifi_led_states={0: True},
            )
            warnings: list[str] = []

            slot_views, _available, _meta, _rows, slot_count, _columns = service._correlate_linux_host(
                ssh_data,
                warnings,
                None,
            )

            self.assertEqual(slot_count, 7)
            self.assertEqual(slot_views[0].device_name, "sdb")
            self.assertEqual(slot_views[0].led_backend, "unifi_fault")
            self.assertTrue(slot_views[0].led_supported)
            self.assertTrue(slot_views[0].identify_active)
            self.assertTrue(slot_views[0].raw_status.get("experimental_led"))
            self.assertTrue(any("UniFi UNVR Pro LED control is experimental." in warning for warning in warnings))

    def test_build_disk_records_adds_camcontrol_peer_aliases_to_lookup_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            records = service._build_disk_records(
                [
                    {
                        "name": "da92",
                        "devname": "da92",
                        "identifier": "{serial_lunid}S49PNY0M300264      _5002538b09339f30",
                        "serial": "S49PNY0M300264      ",
                        "lunid": "5002538b09339f30",
                        "model": "SAMSUNG MZILT3T8HALS/007",
                    }
                ],
                ParsedSSHData(camcontrol_peer_devices={"da92": ["da45"]}),
                {},
                {},
            )

            self.assertEqual(len(records), 1)
            self.assertIn("da45", records[0].lookup_keys)

    def test_lookup_zpool_member_matches_peer_alias_from_disk_lookup_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                camcontrol_peer_devices={"da92": ["da45"]},
                zpool_members={
                    "da45": ZpoolMember(
                        pool_name="The-Repository",
                        vdev_class="special",
                        vdev_name="mirror-0",
                        topology_label="The-Repository > mirror-0 > special",
                        health="ONLINE",
                        raw_name="da45p1",
                        raw_path="/dev/gptid/example",
                    )
                },
            )
            disk = service._build_disk_records(
                [
                    {
                        "name": "da92",
                        "devname": "da92",
                        "identifier": "{serial_lunid}S49PNY0M300264      _5002538b09339f30",
                        "serial": "S49PNY0M300264      ",
                        "lunid": "5002538b09339f30",
                        "model": "SAMSUNG MZILT3T8HALS/007",
                    }
                ],
                ssh_data,
                {},
                {},
            )[0]

            member = service._lookup_zpool_member(disk, disk.device_name, None, ssh_data, {})

            self.assertIsNotNone(member)
            self.assertEqual(member.vdev_class, "special")
            self.assertEqual(member.vdev_name, "mirror-0")

    def test_build_slot_view_falls_back_to_zpool_pool_name_when_disk_pool_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            ssh_data = ParsedSSHData(
                camcontrol_peer_devices={"da92": ["da45"]},
                zpool_members={
                    "da45": ZpoolMember(
                        pool_name="The-Repository",
                        vdev_class="special",
                        vdev_name="mirror-8",
                        topology_label="The-Repository > mirror-8 > special",
                        health="ONLINE",
                        raw_name="da45p1",
                        raw_path="/dev/gptid/example",
                    )
                },
            )
            disk = service._build_disk_records(
                [
                    {
                        "name": "da92",
                        "devname": "da92",
                        "identifier": "{serial_lunid}S49PNY0M300264      _5002538b09339f30",
                        "serial": "S49PNY0M300264      ",
                        "lunid": "5002538b09339f30",
                        "model": "SAMSUNG MZILT3T8HALS/007",
                    }
                ],
                ssh_data,
                {},
                {},
            )[0]

            slot_view = service._build_slot_view(
                slot=58,
                row_index=0,
                column_index=0,
                enclosure_meta={"id": None, "label": None, "name": None},
                raw_slot_status={"device_names": ["da45", "da92"]},
                disk=disk,
                mapping=None,
                ssh_data=ssh_data,
                api_topology_members={},
                api_enclosure_ids=set(),
            )

            self.assertEqual(slot_view.pool_name, "The-Repository")
            self.assertEqual(slot_view.vdev_class, "special")


class InventoryServiceSmartSummaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _build_core_ses_enclosures() -> list[SESMapEnclosure]:
        def make_enclosure(
            enclosure_id: str,
            enclosure_name: str,
            ses_device: str,
            slot_numbers: range,
            *,
            profile_id: str | None = None,
            rows: int | None = None,
            columns: int | None = None,
            slot_layout: list[list[int | None]] | None = None,
        ) -> SESMapEnclosure:
            slots = {
                slot_number: SESMapSlot(
                    slot_number=slot_number,
                    element_id=slot_number,
                    ses_device=ses_device,
                    description=f"Slot {slot_number:02d}",
                )
                for slot_number in slot_numbers
            }
            return SESMapEnclosure(
                ses_device=ses_device,
                enclosure_id=enclosure_id,
                enclosure_name=enclosure_name,
                profile_id=profile_id,
                layout_rows=rows,
                layout_columns=columns,
                slot_layout=slot_layout,
                slots=slots,
            )

        return [
            make_enclosure(
                "3061686369656d30",
                "AHCI SGPIO Enclosure 2.00",
                "/dev/ses0",
                range(0, 6),
            ),
            make_enclosure(
                "50030480090c4f7f",
                "LSI SAS3x40 0601",
                "/dev/ses2",
                range(0, 24),
                profile_id=SCALE_SSG_FRONT_24_PROFILE_ID,
                rows=6,
                columns=4,
                slot_layout=[
                    [5, 11, 17, 23],
                    [4, 10, 16, 22],
                    [3, 9, 15, 21],
                    [2, 8, 14, 20],
                    [1, 7, 13, 19],
                    [0, 6, 12, 18],
                ],
            ),
            make_enclosure(
                "500304801f715f3f",
                "LSI-F SAS3x48Front 0c04",
                "/dev/ses4",
                range(1, 31),
            ),
            make_enclosure(
                "500304801f5a003f",
                "LSI-R SAS3x48Rear 0c04",
                "/dev/ses5",
                range(1, 31),
            ),
        ]

    async def test_core_snapshot_exposes_extra_ssh_enclosure_as_peer_live_option(self) -> None:
        def fake_parse_ssh_outputs(_outputs, slot_count, enclosure_filter, selected_enclosure_id=None):
            enclosures = self._build_core_ses_enclosures()
            slot_candidates, selected_meta = build_slot_candidates_from_ses_enclosures(
                enclosures,
                slot_count,
                enclosure_filter,
                selected_enclosure_id,
            )
            return ParsedSSHData(
                ses_enclosures=enclosures,
                ses_slot_candidates=slot_candidates,
                ses_selected_meta=selected_meta,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[],
                        disks=[],
                        pools=[],
                        disk_temperatures={},
                        smart_test_results=[],
                    ),
                    ssh_outputs={"sesutil map": "stub"},
                    ssh_collected=True,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            )

            with patch("app.services.inventory.parse_ssh_outputs", side_effect=fake_parse_ssh_outputs):
                snapshot = await service.get_snapshot(force_refresh=True)

        self.assertEqual(snapshot.selected_enclosure_id, "500304801f715f3f+500304801f5a003f")
        self.assertEqual(snapshot.selected_profile.id, CORE_CSE_946_PROFILE_ID)
        self.assertEqual(snapshot.summary.enclosure_count, 2)
        self.assertEqual(
            [option.id for option in snapshot.enclosures],
            [
                "500304801f715f3f+500304801f5a003f",
                "50030480090c4f7f",
            ],
        )
        self.assertFalse(any(option.id == "3061686369656d30" for option in snapshot.enclosures))

    async def test_core_snapshot_selects_extra_ssh_enclosure_with_24_bay_profile(self) -> None:
        def fake_parse_ssh_outputs(_outputs, slot_count, enclosure_filter, selected_enclosure_id=None):
            enclosures = self._build_core_ses_enclosures()
            slot_candidates, selected_meta = build_slot_candidates_from_ses_enclosures(
                enclosures,
                slot_count,
                enclosure_filter,
                selected_enclosure_id,
            )
            return ParsedSSHData(
                ses_enclosures=enclosures,
                ses_slot_candidates=slot_candidates,
                ses_selected_meta=selected_meta,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(
                    host="https://archive-core.local",
                    api_key="token",
                    platform="core",
                ),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service._get_inventory_source_bundle = AsyncMock(
                return_value=InventorySourceBundle(
                    raw_data=TrueNASRawData(
                        enclosures=[],
                        disks=[],
                        pools=[],
                        disk_temperatures={},
                        smart_test_results=[],
                    ),
                    ssh_outputs={"sesutil map": "stub"},
                    ssh_collected=True,
                    warnings=[],
                    sources={},
                    scale_ses_data=ParsedSSHData(),
                    quantastor_ses_data=ParsedSSHData(),
                )
            )

            with patch("app.services.inventory.parse_ssh_outputs", side_effect=fake_parse_ssh_outputs):
                snapshot = await service.get_snapshot(
                    selected_enclosure_id="50030480090c4f7f",
                    force_refresh=True,
                )

        self.assertEqual(snapshot.selected_enclosure_id, "50030480090c4f7f")
        self.assertEqual(snapshot.selected_enclosure_label, "LSI SAS3x40 0601")
        self.assertEqual(snapshot.selected_profile.id, SCALE_SSG_FRONT_24_PROFILE_ID)
        self.assertEqual(snapshot.layout_slot_count, 24)
        self.assertEqual(
            [option.id for option in snapshot.enclosures],
            [
                "50030480090c4f7f",
                "500304801f715f3f+500304801f5a003f",
            ],
        )

    async def test_quantastor_snapshot_renders_storage_system_view_and_pool_members(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [
                    {
                        "id": "cluster",
                        "name": "Cluster View",
                        "storageSystemClusterId": "cluster-1",
                        "type": 9,
                        "disableIoFencing": True,
                    },
                    {
                        "id": "node-a",
                        "name": "Node A",
                        "storageSystemClusterId": "cluster-1",
                        "isMaster": False,
                        "disableIoFencing": False,
                    },
                    {
                        "id": "node-b",
                        "name": "Node B",
                        "storageSystemClusterId": "cluster-1",
                        "isMaster": True,
                        "disableIoFencing": False,
                    },
                ]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "storagePoolDeviceId": "pooldev-1",
                            "devicePath": "/dev/sdb",
                            "serialNumber": "QS123",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "temperature": 33,
                            "powerOnHours": 1200,
                            "bytesWritten": 12000000000000,
                            "protocol": "SAS",
                        },
                        {
                            "id": "pdisk-12",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "storagePoolDeviceId": "pooldev-12",
                            "devicePath": "/dev/sdm",
                            "serialNumber": "QS999",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "temperature": 29,
                            "powerOnHours": 2200,
                            "bytesWritten": 22000000000000,
                            "protocol": "SAS",
                        }
                    ],
                    pools=[
                        {
                            "id": "pool-1",
                            "name": "archive",
                            "primaryStorageSystemId": "node-b",
                            "status": "ONLINE",
                        }
                    ],
                    pool_devices=[
                        {
                            "physicalDiskId": "pdisk-1",
                            "storagePoolId": "pool-1",
                            "number": 0,
                            "status": "ONLINE",
                            "devicePath": "/dev/sdb",
                        },
                        {
                            "physicalDiskId": "pdisk-12",
                            "storagePoolId": "pool-1",
                            "number": 12,
                            "slot": "12",
                            "status": "ONLINE",
                            "devicePath": "/dev/sdm",
                        }
                    ],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5000cca000000001",
                            "enclosureId": "enc-a",
                        },
                        {
                            "id": "hw-b-1",
                            "physicalDiskId": "peer-pdisk-1",
                            "storageSystemId": "node-b",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5000cca000000001",
                            "enclosureId": "enc-b",
                        },
                        {
                            "id": "hw-a-12",
                            "physicalDiskId": "pdisk-12",
                            "storageSystemId": "node-a",
                            "slot": "09",
                            "serialNum": "QS999",
                            "sasAddress": "5000cca000000012",
                            "enclosureId": "enc-a",
                        },
                        {
                            "id": "hw-b-12",
                            "physicalDiskId": "peer-pdisk-12",
                            "storageSystemId": "node-b",
                            "slot": "12",
                            "serialNum": "QS999",
                            "sasAddress": "5000cca000000012",
                            "enclosureId": "enc-b",
                        },
                    ],
                    hw_enclosures=[
                        {"id": "enc-a", "storageSystemId": "node-a"},
                        {"id": "enc-b", "storageSystemId": "node-b"},
                    ],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="admin",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=False),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                AsyncMock(),
                temp_dir,
            )

            snapshot = await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertEqual(snapshot.selected_enclosure_id, "node-a")
            self.assertEqual(snapshot.selected_enclosure_label, "Node A")
            self.assertEqual(snapshot.selected_profile.id, "supermicro-ssg-2028r-shared-front-24")
            self.assertEqual(len(snapshot.enclosures), 2)
            self.assertEqual({option.id for option in snapshot.enclosures}, {"node-a", "node-b"})
            self.assertTrue(any("Cluster master is Node B; selected view is Node A." in warning for warning in snapshot.warnings))
            self.assertFalse(any("IO fencing is currently disabled" in warning for warning in snapshot.warnings))
            slot0 = next(slot for slot in snapshot.slots if slot.slot == 0)
            self.assertEqual(slot0.device_name, "sdb")
            self.assertEqual(slot0.pool_name, "archive")
            self.assertEqual(slot0.vdev_name, "member-0")
            self.assertEqual(slot0.vdev_class, "data")
            self.assertIn("active on Node B", slot0.topology_label or "")
            self.assertEqual(slot0.mapping_source, "api")
            self.assertFalse(slot0.led_supported)
            self.assertIn("REST and CLI identify operations are being rejected", slot0.led_reason or "")
            slot12 = next(slot for slot in snapshot.slots if slot.slot == 12)
            self.assertEqual(slot12.device_name, "sdm")
            self.assertEqual(slot12.pool_name, "archive")
            slot23 = next(slot for slot in snapshot.slots if slot.slot == 23)
            self.assertEqual(slot23.state.value, "empty")

    async def test_quantastor_snapshot_warns_when_real_node_reports_io_fencing_disabled(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [
                    {
                        "id": "cluster",
                        "name": "Cluster",
                        "storageSystemClusterId": "cluster-a",
                        "disableIoFencing": False,
                    },
                    {
                        "id": "node-a",
                        "name": "Node A",
                        "storageSystemClusterId": "cluster-a",
                        "isMaster": False,
                        "disableIoFencing": True,
                    },
                    {
                        "id": "node-b",
                        "name": "Node B",
                        "storageSystemClusterId": "cluster-a",
                        "isMaster": True,
                        "disableIoFencing": False,
                    },
                ]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[],
                    pools=[],
                    pool_devices=[],
                    ha_groups=[],
                    hw_disks=[],
                    hw_enclosures=[
                        {"id": "enc-a", "storageSystemId": "node-a"},
                        {"id": "enc-b", "storageSystemId": "node-b"},
                    ],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="admin",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=False),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                AsyncMock(),
                temp_dir,
            )

            snapshot = await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertTrue(any("IO fencing is currently disabled" in warning for warning in snapshot.warnings))

    async def test_quantastor_smart_summary_uses_first_pass_disk_payload(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [{"id": "node-a", "name": "Node A"}]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "storagePoolDeviceId": "pooldev-1",
                            "devicePath": "/dev/sdb",
                            "serialNumber": "QS123",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "slotNumber": 1,
                            "temperature": 33,
                            "powerOnHours": 1200,
                            "bytesRead": 330000000000000,
                            "bytesWritten": 12000000000000,
                            "protocol": "SAS",
                            "revisionLevel": "A1B2",
                            "rotationRate": 7200,
                            "smartHealthTest": "OK",
                            "trimSupported": True,
                            "blockSize": 4096,
                            "errCountNonMedium": 1,
                            "errCountUncorrectedRead": 2,
                            "errCountUncorrectedWrite": 3,
                        }
                    ],
                    pools=[{"id": "pool-1", "name": "archive"}],
                    pool_devices=[],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5000cca000000001",
                            "firmwareVersion": "A1B2",
                            "mediumErrors": 0,
                            "ssdLifeLeft": 95,
                            "predictiveErrors": 4,
                        }
                    ],
                    hw_enclosures=[{"id": "enc-a", "storageSystemId": "node-a"}],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="admin",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=False),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                AsyncMock(),
                temp_dir,
            )

            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="node-a")

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 33)
            self.assertEqual(summary.power_on_hours, 1200)
            self.assertEqual(summary.bytes_read, 330000000000000)
            self.assertEqual(summary.bytes_written, 12000000000000)
            self.assertEqual(summary.rotation_rate_rpm, 7200)
            self.assertEqual(summary.smart_health_status, "OK")
            self.assertEqual(summary.firmware_version, "A1B2")
            self.assertEqual(summary.endurance_remaining_percent, 95)
            self.assertEqual(summary.trim_supported, True)
            self.assertEqual(summary.logical_block_size, 4096)
            self.assertEqual(summary.physical_block_size, 4096)
            self.assertEqual(summary.non_medium_errors, 1)
            self.assertEqual(summary.uncorrected_read_errors, 2)
            self.assertEqual(summary.uncorrected_write_errors, 3)
            self.assertEqual(summary.predictive_errors, 4)
            self.assertEqual(summary.transport_protocol, "SAS")
            self.assertEqual(summary.sas_address, "5000cca000000001")
            self.assertIn("Quantastor REST SMART detail is first-pass", summary.message or "")

    async def test_quantastor_snapshot_uses_cli_hw_rows_for_shared_slot_truth(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [
                    {"id": "node-a", "name": "Node A", "storageSystemClusterId": "cluster-a"},
                    {"id": "node-b", "name": "Node B", "storageSystemClusterId": "cluster-a", "isMaster": True},
                ]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "devicePath": "/dev/sdb",
                            "serialNumber": "QS123",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "slotNumber": 1,
                        },
                        {
                            "id": "pdisk-12",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "storagePoolDeviceId": "pooldev-12",
                            "iofenceSystemId": "node-b",
                            "devicePath": "/dev/sdm",
                            "serialNumber": "QS999",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "slotNumber": 9,
                        },
                    ],
                    pools=[{"id": "pool-1", "name": "archive", "primaryStorageSystemId": "node-b"}],
                    pool_devices=[
                        {
                            "id": "pooldev-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "number": 0,
                            "slot": "01",
                            "status": "ONLINE",
                        },
                        {
                            "id": "pooldev-12",
                            "physicalDiskId": "pdisk-12",
                            "storageSystemId": "node-b",
                            "storagePoolId": "pool-1",
                            "number": 12,
                            "slot": "12",
                            "status": "ONLINE",
                        },
                    ],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5000cca000000001",
                            "enclosureId": "enc-a",
                        },
                        {
                            "id": "hw-a-12",
                            "physicalDiskId": "pdisk-12",
                            "storageSystemId": "node-a",
                            "slot": "09",
                            "serialNum": "QS999",
                            "sasAddress": "5000cca000000012",
                            "enclosureId": "enc-a",
                        },
                    ],
                    hw_enclosures=[{"id": "enc-a", "storageSystemId": "node-a"}],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        async def run_command(command: str) -> SSHCommandResult:
            if "hw-disk-list" in command:
                payload = [
                    {
                        "id": "hw-a-1",
                        "physicalDiskId": "pdisk-1",
                        "storageSystemId": "node-a",
                        "slot": "01",
                        "serialNum": "QS123",
                        "sasAddress": "5000cca000000001",
                        "enclosureId": "enc-a",
                    },
                    {
                        "id": "hw-a-12",
                        "physicalDiskId": "pdisk-12",
                        "storageSystemId": "node-a",
                        "slot": "09",
                        "serialNum": "QS999",
                        "sasAddress": "5000cca000000012",
                        "enclosureId": "enc-a",
                    },
                    {
                        "id": "hw-b-12",
                        "physicalDiskId": "peer-pdisk-12",
                        "storageSystemId": "node-b",
                        "slot": "09",
                        "serialNum": "QS999",
                        "sasAddress": "5000cca000000012",
                        "enclosureId": "enc-b",
                    },
                ]
            elif "disk-list" in command:
                payload = [
                    {
                        "id": "cli-1",
                        "storageSystemId": "node-a",
                        "hwDiskId": "pdisk-1",
                        "storagePoolId": "pool-1",
                        "devicePath": "/dev/sdb",
                        "serialNumber": "QS123",
                        "scsiId": "5000cca000000001",
                    },
                    {
                        "id": "cli-12",
                        "storageSystemId": "node-a",
                        "hwDiskId": "pdisk-12",
                        "storagePoolId": "pool-1",
                        "devicePath": "/dev/sdm",
                        "serialNumber": "QS999",
                        "scsiId": "5000cca000000012",
                    },
                ]
            else:
                payload = [
                    {"id": "enc-a", "storageSystemId": "node-a"},
                    {"id": "enc-b", "storageSystemId": "node-b"},
                ]
            return SSHCommandResult(command=command, ok=True, stdout=json.dumps(payload), stderr="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            ssh_probe.run_command.side_effect = run_command
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ParsedSSHData(), []))

            snapshot = await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertEqual(snapshot.sources["ssh"].message, "SSH probe and Quantastor CLI enrichment completed.")
            self.assertEqual(snapshot.platform_context["selected_view_label"], "Node A")
            self.assertEqual(snapshot.platform_context["master_label"], "Node B")
            self.assertTrue(snapshot.platform_context["io_fencing_enabled"])
            slot12 = next(slot for slot in snapshot.slots if slot.slot == 12)
            self.assertEqual(slot12.device_name, "sdm")
            self.assertEqual(slot12.pool_name, "archive")
            self.assertEqual(slot12.operator_context["pool_owner_label"], "Node B")
            self.assertEqual(slot12.operator_context["fence_owner_label"], "Node B")
            self.assertEqual(slot12.operator_context["visible_on_labels"], ["Node A", "Node B"])
            slot8 = next(slot for slot in snapshot.slots if slot.slot == 8)
            self.assertEqual(slot8.state.value, "empty")

    async def test_quantastor_snapshot_prefers_ses_truth_over_stale_slot_hint(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [
                    {"id": "node-a", "name": "Node A", "storageSystemClusterId": "cluster-a"},
                    {"id": "node-b", "name": "Node B", "storageSystemClusterId": "cluster-a", "isMaster": True},
                ]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "spare-12",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "devicePath": "/dev/sdm",
                            "serialNumber": "QS999",
                            "vendorId": "SAMSUNG",
                            "productId": "PM1643",
                            "size": 3840000000000,
                            "healthStatus": "ONLINE",
                            "slotNumber": 9,
                            "scsiId": "35002538b103e5ee0",
                        }
                    ],
                    pools=[{"id": "pool-1", "name": "archive", "primaryStorageSystemId": "node-b"}],
                    pool_devices=[
                        {
                            "id": "pooldev-12",
                            "physicalDiskId": "spare-12",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "number": 12,
                            "slot": "09",
                            "status": "AVAIL",
                            "isSpare": True,
                        }
                    ],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-12",
                            "physicalDiskId": "spare-12",
                            "storageSystemId": "node-a",
                            "slot": "09",
                            "serialNum": "QS999",
                            "sasAddress": "5002538b103e5ee0",
                            "enclosureId": "enc-a",
                        }
                    ],
                    hw_enclosures=[{"id": "enc-a", "storageSystemId": "node-a"}],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            ses_overlay = ParsedSSHData(
                ses_slot_candidates={
                    8: {
                        "present": False,
                        "sas_address_hint": "0",
                        "sas_device_type": "no SAS device attached",
                        "ses_device": "/dev/sg11",
                        "ses_element_id": 8,
                        "ses_targets": [
                            {
                                "ssh_host": "10.0.0.20",
                                "ses_device": "/dev/sg11",
                                "ses_element_id": 8,
                                "ses_slot_number": 8,
                            }
                        ],
                    },
                    12: {
                        "present": True,
                        "sas_address_hint": "5002538b103e5ee2",
                        "attached_sas_address": "5003048026b2ff7f",
                        "sas_device_type": "end device",
                        "ses_device": "/dev/sg11",
                        "ses_element_id": 12,
                        "ses_targets": [
                            {
                                "ssh_host": "10.0.0.20",
                                "ses_device": "/dev/sg11",
                                "ses_element_id": 12,
                                "ses_slot_number": 12,
                            }
                        ],
                    },
                }
            )
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ses_overlay, []))
            service._fetch_quantastor_cli_overlay = AsyncMock(
                return_value=({"cli_disks": [], "cli_hw_disks": [], "cli_hw_enclosures": []}, [])
            )

            snapshot = await service.get_snapshot(selected_enclosure_id="node-a")

            slot12 = next(slot for slot in snapshot.slots if slot.slot == 12)
            self.assertEqual(slot12.device_name, "sdm")
            self.assertEqual(slot12.serial, "QS999")
            self.assertEqual(slot12.ssh_ses_targets[0]["ssh_host"], "10.0.0.20")
            self.assertEqual(slot12.raw_status.get("attached_sas_address"), "5003048026b2ff7f")
            slot8 = next(slot for slot in snapshot.slots if slot.slot == 8)
            self.assertFalse(slot8.present)
            self.assertEqual(slot8.state.value, "empty")

    async def test_quantastor_snapshot_defaults_to_active_pool_owner_when_selection_is_empty(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [
                    {"id": "node-a", "name": "Node A", "storageSystemClusterId": "cluster-a"},
                    {"id": "node-b", "name": "Node B", "storageSystemClusterId": "cluster-a", "isMaster": True},
                ]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[],
                    pools=[
                        {"id": "pool-1", "name": "archive", "activeStorageSystemId": "node-b"},
                    ],
                    ha_groups=[],
                    hw_disks=[
                        {"id": "hw-a", "storageSystemId": "node-a"},
                        {"id": "hw-b", "storageSystemId": "node-b"},
                    ],
                    hw_enclosures=[
                        {"id": "enc-a", "storageSystemId": "node-a"},
                        {"id": "enc-b", "storageSystemId": "node-b"},
                    ],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ParsedSSHData(), []))
            service._fetch_quantastor_cli_overlay = AsyncMock(
                return_value=({"cli_disks": [], "cli_hw_disks": [], "cli_hw_enclosures": []}, [])
            )

            snapshot = await service.get_snapshot()

            self.assertEqual(snapshot.selected_enclosure_id, "node-b")
            self.assertEqual(snapshot.selected_enclosure_label, "Node B")
            self.assertEqual(snapshot.platform_context["selected_view_label"], "Node B")
            self.assertEqual(snapshot.platform_context["master_label"], "Node B")

    async def test_quantastor_snapshot_fetches_ses_overlay_before_cli_overlay(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [{"id": "node-a", "name": "Node A"}]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[],
                    pools=[],
                    ha_groups=[],
                    hw_disks=[],
                    hw_enclosures=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            order: list[str] = []

            async def fetch_ses():
                order.append("ses")
                service._quantastor_preferred_ses_host = "10.0.0.20"
                return ParsedSSHData(), []

            async def fetch_cli():
                order.append("cli")
                return {"cli_disks": [], "cli_hw_disks": [], "cli_hw_enclosures": []}, []

            service._fetch_quantastor_ses_overlay = AsyncMock(side_effect=fetch_ses)
            service._fetch_quantastor_cli_overlay = AsyncMock(side_effect=fetch_cli)

            await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertEqual(order, ["ses", "cli"])

    async def test_fetch_quantastor_cli_overlay_prefers_cached_working_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service._quantastor_preferred_ses_host = "10.0.0.20"

            async def run_command(command: str, host: str | None = None) -> SSHCommandResult:
                payload = [{"id": f"{host}-row"}]
                return SSHCommandResult(command=command, ok=True, stdout=json.dumps(payload), stderr="", exit_code=0)

            service._run_ssh_command = AsyncMock(side_effect=run_command)

            overlay, failures = await service._fetch_quantastor_cli_overlay()

            self.assertEqual(failures, [])
            self.assertEqual(service._quantastor_preferred_ses_host, "10.0.0.20")
            self.assertEqual(overlay["cli_disks"][0]["id"], "10.0.0.20-row")
            self.assertEqual(overlay["cli_hw_disks"][0]["id"], "10.0.0.20-row")
            self.assertEqual(overlay["cli_hw_enclosures"][0]["id"], "10.0.0.20-row")
            awaited_hosts = [call.args[1] for call in service._run_ssh_command.await_args_list]
            self.assertEqual(awaited_hosts, ["10.0.0.20", "10.0.0.20", "10.0.0.20"])

    async def test_fetch_quantastor_cli_overlay_falls_back_to_extra_host_and_caches_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )

            async def run_command(command: str, host: str | None = None) -> SSHCommandResult:
                if host == "10.0.0.10":
                    return SSHCommandResult(command=command, ok=False, stdout="", stderr="wrong host", exit_code=1)
                payload = [{"id": f"{host}-row"}]
                return SSHCommandResult(command=command, ok=True, stdout=json.dumps(payload), stderr="", exit_code=0)

            service._run_ssh_command = AsyncMock(side_effect=run_command)

            overlay, failures = await service._fetch_quantastor_cli_overlay()

            self.assertEqual(failures, [])
            self.assertEqual(service._quantastor_preferred_ses_host, "10.0.0.20")
            self.assertEqual(overlay["cli_disks"][0]["id"], "10.0.0.20-row")
            self.assertEqual(overlay["cli_hw_disks"][0]["id"], "10.0.0.20-row")
            self.assertEqual(overlay["cli_hw_enclosures"][0]["id"], "10.0.0.20-row")
            awaited_hosts = [call.args[1] for call in service._run_ssh_command.await_args_list]
            self.assertEqual(
                awaited_hosts,
                ["10.0.0.10", "10.0.0.10", "10.0.0.10", "10.0.0.20", "10.0.0.20", "10.0.0.20"],
            )

    async def test_get_slot_smart_summaries_deduplicates_and_filters_to_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                truenas=TrueNASConfig(platform="quantastor"),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[
                        SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="sda"),
                        SlotView(slot=1, slot_label="01", row_index=0, column_index=1, device_name="sdb"),
                    ],
                    refresh_interval_seconds=30,
                )
            )

            async def fetch_summary(slot_view: SlotView, *, allow_stale_cache: bool = False) -> SmartSummaryView:
                return SmartSummaryView(available=True, power_on_hours=100 + slot_view.slot)

            service._get_slot_smart_summary_for_slot_view = AsyncMock(side_effect=fetch_summary)

            results = await service.get_slot_smart_summaries([1, 0, 1, 99], selected_enclosure_id="node-a")

            self.assertEqual([item.slot for item in results], [1, 0])
            self.assertEqual(results[0].summary.power_on_hours, 101)
            self.assertEqual(results[1].summary.power_on_hours, 100)
            self.assertEqual(service.get_snapshot.await_count, 1)
            awaited_slots = [call.args[0].slot for call in service._get_slot_smart_summary_for_slot_view.await_args_list]
            self.assertEqual(awaited_slots, [1, 0])

    async def test_get_slot_smart_summaries_respects_max_concurrency_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            settings.app.smart_batch_max_concurrency = 8
            system = SystemConfig(
                id="archive-core",
                label="Archive CORE",
                truenas=TrueNASConfig(platform="core"),
            )
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[
                        SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="sda"),
                        SlotView(slot=1, slot_label="01", row_index=0, column_index=1, device_name="sdb"),
                        SlotView(slot=2, slot_label="02", row_index=0, column_index=2, device_name="sdc"),
                        SlotView(slot=3, slot_label="03", row_index=0, column_index=3, device_name="sdd"),
                    ],
                    refresh_interval_seconds=30,
                )
            )

            active_calls = 0
            peak_calls = 0

            async def fetch_summary(slot_view: SlotView, *, allow_stale_cache: bool = False) -> SmartSummaryView:
                nonlocal active_calls, peak_calls
                active_calls += 1
                peak_calls = max(peak_calls, active_calls)
                await asyncio.sleep(0.01)
                active_calls -= 1
                return SmartSummaryView(available=True, power_on_hours=200 + slot_view.slot)

            service._get_slot_smart_summary_for_slot_view = AsyncMock(side_effect=fetch_summary)

            results = await service.get_slot_smart_summaries(
                [0, 1, 2, 3],
                selected_enclosure_id="enc-a",
                max_concurrency=2,
            )

            self.assertEqual([item.slot for item in results], [0, 1, 2, 3])
            self.assertLessEqual(peak_calls, 2)

    async def test_quantastor_snapshot_uses_ses_overlay_for_led_control(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [{"id": "node-a", "name": "Node A"}]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "devicePath": "/dev/sdb",
                            "serialNumber": "QS123",
                            "vendorId": "SAMSUNG",
                            "productId": "PM1643",
                            "size": 3840000000000,
                            "healthStatus": "ONLINE",
                        }
                    ],
                    pools=[{"id": "pool-1", "name": "archive"}],
                    pool_devices=[],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5002538b496a5512",
                            "enclosureId": "enc-a",
                        }
                    ],
                    hw_enclosures=[{"id": "enc-a", "storageSystemId": "node-a"}],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            service._fetch_quantastor_cli_overlay = AsyncMock(
                return_value=({"cli_disks": [], "cli_hw_disks": [], "cli_hw_enclosures": []}, [])
            )

            aes_output = """  SMCDRS2U  SAS3x40           0701
  Primary enclosure logical identifier (hex): 5003048026b2ff7f
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5003048026b2ff7f
          SAS address: 0x5002538b496a5512
          phy identifier: 0x0
"""
            ec_output = """  SMCDRS2U  SAS3x40           0701
Enclosure Status diagnostic page:
  generation code: 0x0
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Overall descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Unsupported
        Ready to insert=0, RMV=0, Ident=0, Report=0
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
        Ready to insert=0, RMV=0, Ident=1, Report=0
"""
            ses_overlay = parse_ssh_outputs(
                {
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg11": aes_output,
                    "sudo -n /usr/bin/sg_ses -p ec /dev/sg11": ec_output,
                },
                60,
                None,
                None,
            )
            service._tag_ses_overlay(ses_overlay, "10.0.0.20")
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ses_overlay, []))

            snapshot = await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertEqual(snapshot.sources["ssh"].message, "SSH probe and Quantastor SES overlay completed.")
            slot0 = next(slot for slot in snapshot.slots if slot.slot == 0)
            self.assertTrue(slot0.led_supported)
            self.assertEqual(slot0.led_backend, "quantastor_sg_ses")
            self.assertTrue(slot0.identify_active)
            self.assertEqual(slot0.ssh_ses_targets[0]["ssh_host"], "10.0.0.20")
            self.assertEqual(slot0.ssh_ses_targets[0]["ses_device"], "/dev/sg11")

    async def test_scale_snapshot_uses_dynamic_ses_rediscovery_for_led_control(self) -> None:
        class DummyScaleClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="offsite-scale",
                label="Offsite SCALE",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            service = build_inventory_service(
                settings,
                system,
                DummyScaleClient(),
                ssh_probe,
                temp_dir,
            )

            aes_output = """  SMCDRS2U  SAS3x40           0701
  Primary enclosure logical identifier (hex): 5003048026b2ff7f
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5003048026b2ff7f
          SAS address: 0x5002538b496a5512
          phy identifier: 0x0
"""
            ec_output = """  SMCDRS2U  SAS3x40           0701
Enclosure Status diagnostic page:
  generation code: 0x0
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Overall descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Unsupported
        Ready to insert=0, RMV=0, Ident=0, Report=0
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
        Ready to insert=0, RMV=0, Ident=1, Report=0
"""
            ses_overlay = parse_ssh_outputs(
                {
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg27": aes_output,
                    "sudo -n /usr/bin/sg_ses -p ec /dev/sg27": ec_output,
                },
                60,
                None,
                None,
            )
            service._tag_ses_overlay(ses_overlay, "10.0.0.20")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(ses_overlay, []))

            snapshot = await service.get_snapshot()

            self.assertEqual(snapshot.sources["ssh"].message, "SSH probe and SCALE SES rediscovery completed.")
            slot0 = next(slot for slot in snapshot.slots if slot.slot == 0)
            self.assertTrue(slot0.led_supported)
            self.assertEqual(slot0.led_backend, "scale_sg_ses")
            self.assertTrue(slot0.identify_active)
            self.assertEqual(slot0.ssh_ses_targets[0]["ssh_host"], "10.0.0.20")
            self.assertEqual(slot0.ssh_ses_targets[0]["ses_device"], "/dev/sg27")

    async def test_quantastor_cli_enrichment_surfaces_in_smart_summary(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                systems = [{"id": "node-a", "name": "Node A"}]
                return TrueNASRawData(
                    enclosures=systems,
                    systems=systems,
                    disks=[
                        {
                            "id": "pdisk-1",
                            "storageSystemId": "node-a",
                            "storagePoolId": "pool-1",
                            "devicePath": "/dev/sdb",
                            "serialNumber": "QS123",
                            "vendorId": "WDC",
                            "productId": "Ultrastar",
                            "size": 14000000000000,
                            "healthStatus": "ONLINE",
                            "slotNumber": 1,
                            "protocol": "",
                        }
                    ],
                    pools=[{"id": "pool-1", "name": "archive"}],
                    pool_devices=[],
                    ha_groups=[],
                    hw_disks=[
                        {
                            "id": "hw-a-1",
                            "physicalDiskId": "pdisk-1",
                            "storageSystemId": "node-a",
                            "slot": "01",
                            "serialNum": "QS123",
                            "sasAddress": "5000cca000000001",
                        }
                    ],
                    hw_enclosures=[{"id": "enc-a", "storageSystemId": "node-a"}],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        async def run_command(command: str) -> SSHCommandResult:
            if "hw-disk-list" in command:
                payload = [
                    {
                        "id": "hw-a-1",
                        "physicalDiskId": "pdisk-1",
                        "storageSystemId": "node-a",
                        "slot": "01",
                        "serialNum": "QS123",
                        "sasAddress": "5000cca000000001",
                        "firmwareVersion": "A1B2",
                        "driveTemp": "35 C (95.00 F)",
                        "predictiveErrors": "4",
                    }
                ]
            elif "disk-list" in command:
                payload = [
                    {
                        "id": "cli-1",
                        "storageSystemId": "node-a",
                        "hwDiskId": "pdisk-1",
                        "storagePoolId": "pool-1",
                        "devicePath": "/dev/sdb",
                        "serialNumber": "QS123",
                        "driveTemp": "35 C (95.00 F)",
                        "protocol": "SAS",
                        "revisionLevel": "A1B2",
                        "smartHealthTest": "[PASSED]",
                        "ssdLifeLeft": "92%",
                        "trimSupported": "true",
                        "blockSize": "4096",
                        "errCountNonMedium": "1",
                        "errCountUncorrectedRead": "2",
                        "errCountUncorrectedWrite": "3",
                    }
                ]
            elif "smartctl" in command and "-j" in command:
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        '{'
                        '"power_on_time":{"hours":21854},'
                        '"rotation_rate":0,'
                        '"form_factor":{"name":"2.5 inches"},'
                        '"scsi_transport_protocol":{"name":"SAS"},'
                        '"logical_unit_id":"35002538b496a5510",'
                        '"scsi_sas_port_0":{"phy_0":{'
                        '"sas_address":"5002538b496a5510",'
                        '"attached_sas_address":"5003048000000001",'
                        '"negotiated_logical_link_rate":"phy enabled; 12 Gbps"'
                        '}}'
                        '}'
                    ),
                    stderr="",
                    exit_code=0,
                )
            elif "smartctl" in command:
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        "Read Cache is:        Enabled\n"
                        "Writeback Cache is:   Enabled\n"
                    ),
                    stderr="",
                    exit_code=0,
                )
            else:
                payload = [{"id": "enc-a", "storageSystemId": "node-a"}]
            return SSHCommandResult(command=command, ok=True, stdout=json.dumps(payload), stderr="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            ssh_probe = AsyncMock()
            ssh_probe.run_commands.return_value = []
            ssh_probe.run_command.side_effect = run_command
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                ssh_probe,
                temp_dir,
            )
            service._fetch_quantastor_ses_overlay = AsyncMock(return_value=(ParsedSSHData(), []))

            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="node-a")

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 35)
            self.assertEqual(summary.transport_protocol, "SAS")
            self.assertEqual(summary.firmware_version, "A1B2")
            self.assertEqual(summary.sas_address, "5000cca000000001")
            self.assertEqual(summary.attached_sas_address, "0x5003048000000001")
            self.assertEqual(summary.negotiated_link_rate, "phy enabled; 12 Gbps")
            self.assertEqual(summary.smart_health_status, "PASSED")
            self.assertEqual(summary.power_on_hours, 21854)
            self.assertEqual(summary.endurance_remaining_percent, 92)
            self.assertEqual(summary.trim_supported, True)
            self.assertEqual(summary.logical_block_size, 4096)
            self.assertEqual(summary.physical_block_size, 4096)
            self.assertEqual(summary.rotation_rate_rpm, 0)
            self.assertEqual(summary.form_factor, "2.5 inches")
            self.assertEqual(summary.non_medium_errors, 1)
            self.assertEqual(summary.uncorrected_read_errors, 2)
            self.assertEqual(summary.uncorrected_write_errors, 3)
            self.assertEqual(summary.predictive_errors, 4)
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, True)
            self.assertIn("supplemented with SSH CLI disk rows", summary.message or "")

    async def test_quantastor_smart_summary_prefers_ses_target_host(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                raise AssertionError("Snapshot is mocked in this test.")

        async def run_command(command: str, host: str | None = None) -> SSHCommandResult:
            target_host = host or "10.0.0.10"
            if target_host != "10.0.0.20":
                return SSHCommandResult(
                    command=command,
                    ok=False,
                    stdout="",
                    stderr="wrong host",
                    exit_code=1,
                )
            if command.endswith("-x /dev/sdb"):
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        "Read Cache is:        Enabled\n"
                        "Writeback Cache is:   Enabled\n"
                    ),
                    stderr="",
                    exit_code=0,
                )
            return SSHCommandResult(
                command=command,
                ok=True,
                stdout=(
                    '{'
                    '"power_on_time":{"hours":47003},'
                    '"rotation_rate":0,'
                    '"form_factor":{"name":"2.5 inches"},'
                    '"scsi_transport_protocol":{"name":"SAS (SPL-3)"},'
                    '"logical_unit_id":"0x5002538b496a53f0",'
                    '"scsi_sas_port_0":{"phy_0":{'
                    '"sas_address":"0x5002538b496a53f0",'
                    '"attached_sas_address":"0x5003048026b2ff7f"'
                    '}}'
                    '}'
                ),
                stderr="",
                exit_code=0,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                AsyncMock(),
                temp_dir,
            )
            service._run_ssh_command = AsyncMock(side_effect=run_command)
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                device_name="sdb",
                serial="QS123",
                logical_block_size=4096,
                raw_status={
                    "disk_raw": {
                        "smartHealthTest": "[PASSED]",
                        "protocol": "SAS",
                        "sasAddress": "5002538b496a53f0",
                    }
                },
                ssh_ses_targets=[
                    {
                        "ssh_host": "10.0.0.20",
                        "ses_device": "/dev/sg11",
                        "ses_element_id": 0,
                        "ses_slot_number": 0,
                    }
                ],
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(0, selected_enclosure_id="node-a")

            self.assertTrue(summary.available)
            self.assertEqual(summary.power_on_hours, 47003)
            self.assertEqual(summary.attached_sas_address, "0x5003048026b2ff7f")
            awaited = [call.args for call in service._run_ssh_command.await_args_list]
            self.assertEqual(awaited[0][1], "10.0.0.20")

    def test_build_quantastor_smart_devices_prefers_by_id_and_sd_paths_over_by_path(self) -> None:
        settings = Settings()
        system = SystemConfig(
            id="quantastor-lab",
            label="Quantastor Lab",
            default_profile_id="supermicro-ssg-2028r-shared-front-24",
            truenas=TrueNASConfig(
                platform="quantastor",
                api_user="jbodmap",
                api_password="secret",
            ),
            ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
        )
        service = InventoryService(
            settings=settings,
            system=system,
            truenas_client=AsyncMock(),
            ssh_probe=AsyncMock(),
            mapping_store=MappingStore(Path(tempfile.mkdtemp()) / "mappings.json"),
            profile_registry=ProfileRegistry(settings),
        )

        smart_devices = service._build_quantastor_smart_devices(
            disk={
                "devicePath": "/dev/disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
                "altDevicePath": "/dev/sdk",
                "name": "sdk (pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0)",
            },
            merged_raw={
                "devicePath": "/dev/disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
                "altDevicePath": "/dev/sdk",
            },
            cli_hint={
                "devicePath": "/dev/disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
                "altDevicePath": "/dev/sdk",
            },
            hw_hint=None,
            pool_hint={
                "pool_device_raw": {
                    "devicePath": "/dev/disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960",
                    "physicalDiskObj": {
                        "devicePath": "/dev/disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960",
                        "altDevicePath": "/dev/sdk",
                    },
                }
            },
        )

        self.assertEqual(
            smart_devices[:3],
            [
                "disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960",
                "sdk",
                "disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
            ],
        )

    def test_build_quantastor_topology_members_requires_authoritative_pool_device_rows(self) -> None:
        settings = Settings()
        system = SystemConfig(
            id="quantastor-lab",
            label="Quantastor Lab",
            default_profile_id="supermicro-ssg-2028r-shared-front-24",
            truenas=TrueNASConfig(
                platform="quantastor",
                api_user="jbodmap",
                api_password="secret",
            ),
            ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
        )
        service = InventoryService(
            settings=settings,
            system=system,
            truenas_client=AsyncMock(),
            ssh_probe=AsyncMock(),
            mapping_store=MappingStore(Path(tempfile.mkdtemp()) / "mappings.json"),
            profile_registry=ProfileRegistry(settings),
        )
        raw_data = TrueNASRawData(
            enclosures=[],
            disks=[],
            pools=[{"id": "pool-1", "name": "HA-Pool-R10"}],
            disk_temperatures={},
            smart_test_results=[],
            systems=[{"id": "node-a", "name": "QSOSN-Right"}],
            pool_devices=[],
        )
        disk = DiskRecord(
            raw={"storagePoolId": "pool-1", "name": "disk/by-id/wwn-0x5000"},
            device_name="disk/by-id/wwn-0x5000",
            path_device_name="disk/by-id/wwn-0x5000",
            multipath_name=None,
            multipath_member=None,
            serial="SERIAL-1",
            model="MODEL-1",
            size_bytes=None,
            identifier="scsi-SERIAL-1",
            health="ONLINE",
            pool_name="HA-Pool-R10",
            lunid=None,
            bus=None,
            temperature_c=None,
            last_smart_test_type=None,
            last_smart_test_status=None,
            last_smart_test_lifetime_hours=None,
            logical_block_size=None,
            physical_block_size=None,
            enclosure_id="node-a",
            slot=0,
            smart_devices=[],
            lookup_keys={"disk/by-id/wwn-0x5000", "serial-1"},
        )

        members = service._build_quantastor_topology_members(raw_data, [disk])

        self.assertEqual(members, {})

    async def test_quantastor_smart_summary_handles_spare_using_by_path_primary_alias(self) -> None:
        class DummyQuantastorClient:
            async def fetch_all(self) -> TrueNASRawData:
                raise AssertionError("Snapshot is mocked in this test.")

        async def run_command(command: str, host: str | None = None) -> SSHCommandResult:
            target_host = host or "10.0.0.10"
            if target_host != "10.0.0.20":
                return SSHCommandResult(command=command, ok=False, stdout="", stderr="wrong host", exit_code=1)
            if "/dev/disk/by-path/" in command:
                return SSHCommandResult(command=command, ok=False, stdout="", stderr="sudo blocked", exit_code=1)
            if command.endswith("-x /dev/sdk") or command.endswith("-x /dev/disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960"):
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        "Rotation Rate:        Solid State Device\n"
                        "Form Factor:          2.5 inches\n"
                        "Read Cache is:        Enabled\n"
                        "Writeback Cache is:   Enabled\n"
                    ),
                    stderr="",
                    exit_code=0,
                )
            return SSHCommandResult(
                command=command,
                ok=True,
                stdout=(
                    '{'
                    '"power_on_time":{"hours":47003},'
                    '"rotation_rate":0,'
                    '"form_factor":{"name":"2.5 inches"},'
                    '"scsi_transport_protocol":{"name":"SAS (SPL-3)"},'
                    '"logical_unit_id":"0x5002538b103e5ee0",'
                    '"scsi_sas_port_0":{"phy_0":{'
                    '"sas_address":"0x5002538b103e5ee0",'
                    '"attached_sas_address":"0x5003048026b2ff7f"'
                    '}}'
                    '}'
                ),
                stderr="",
                exit_code=0,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                label="Quantastor Lab",
                default_profile_id="supermicro-ssg-2028r-shared-front-24",
                truenas=TrueNASConfig(
                    platform="quantastor",
                    api_user="jbodmap",
                    api_password="secret",
                ),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", extra_hosts=["10.0.0.20"], user="jbodmap", commands=[]),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyQuantastorClient(),
                AsyncMock(),
                temp_dir,
            )
            service._run_ssh_command = AsyncMock(side_effect=run_command)
            slot = SlotView(
                slot=12,
                slot_label="12",
                row_index=0,
                column_index=12,
                device_name="disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
                smart_device_names=[
                    "disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960",
                    "sdk",
                    "disk/by-path/pci-0000:01:00.0-sas-exp0x500605b0000272ff-phy24-lun-0",
                ],
                serial="S49PNA0N308960",
                logical_block_size=4096,
                raw_status={
                    "disk_raw": {
                        "smartHealthTest": "OK",
                        "protocol": "SAS",
                        "sasAddress": "5002538b103e5ee0",
                    }
                },
                ssh_ses_targets=[
                    {
                        "ssh_host": "10.0.0.20",
                        "ses_device": "/dev/sg11",
                        "ses_element_id": 12,
                        "ses_slot_number": 12,
                    }
                ],
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(12, selected_enclosure_id="node-a")

            self.assertTrue(summary.available)
            self.assertEqual(summary.power_on_hours, 47003)
            self.assertEqual(summary.rotation_rate_rpm, 0)
            self.assertEqual(summary.form_factor, "2.5 inches")
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, True)
            awaited = [(call.args[0], call.args[1]) for call in service._run_ssh_command.await_args_list]
            self.assertEqual(awaited[0][1], "10.0.0.20")
            self.assertIn("/dev/disk/by-id/scsi-SSAMSUNG_MZILT3T8HALS_007_S49PNA0N308960", awaited[0][0])

    async def test_core_smart_summary_enriches_sparse_api_result_with_smartctl_text(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []

            async def fetch_disk_smartctl(self, disk_name: str, args: list[str] | None = None):
                self.calls.append((disk_name, tuple(args or [])))
                if args == ["-x"]:
                    return (
                        "Transport protocol:   SAS (SPL-4)\n"
                        "Logical Unit id:      0x5000cca23b713c80\n"
                        "Read Cache is:        Enabled\n"
                        "Writeback Cache is:   Disabled\n"
                        "    negotiated logical link rate: phy enabled; 12 Gbps\n"
                        "    SAS address = 0x5000cca23b713c81\n"
                        "    attached SAS address = 0x500304801f715f3f\n"
                    )
                return (
                    '{'
                    '"temperature":{"current":29},'
                    '"power_on_time":{"hours":24572},'
                    '"logical_block_size":512,'
                    '"physical_block_size":4096,'
                    '"rotation_rate":7200,'
                    '"form_factor":{"name":"3.5 inches"},'
                    '"scsi_error_counter_log":{'
                    '"read":{"gigabytes_processed":"717449.555"},'
                    '"write":{"gigabytes_processed":"109115.331"}'
                    '},'
                    '"scsi_self_test_0":{'
                    '"code":{"string":"Background short"},'
                    '"result":{"string":"Completed"},'
                    '"power_on_time":{"hours":24549}'
                    "}"
                    "}"
                )

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                return SSHCommandResult(
                    command=command,
                    ok=False,
                    stderr="ssh fallback should not run for core API text enrichment",
                    exit_code=1,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=21,
                slot_label="21",
                row_index=0,
                column_index=0,
                device_name="multipath/disk12",
                logical_block_size=512,
                physical_block_size=4096,
                logical_unit_id="5000cca23b713c80",
                multipath=MultipathView(
                    name="disk12",
                    device_name="multipath/disk12",
                    members=[
                        {"device_name": "da65", "state": "ACTIVE", "controller_label": "mpr1"},
                        {"device_name": "da18", "state": "PASSIVE", "controller_label": "mpr0"},
                    ],
                ),
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(21)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 29)
            self.assertEqual(summary.power_on_hours, 24572)
            self.assertEqual(summary.logical_block_size, 512)
            self.assertEqual(summary.physical_block_size, 4096)
            self.assertEqual(summary.rotation_rate_rpm, 7200)
            self.assertEqual(summary.form_factor, "3.5 inches")
            self.assertEqual(summary.bytes_read, 717449555000000)
            self.assertEqual(summary.bytes_written, 109115331000000)
            self.assertEqual(summary.annualized_bytes_written, 38899979633729)
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, False)
            self.assertEqual(summary.transport_protocol, "SAS (SPL-4)")
            self.assertEqual(summary.logical_unit_id, "5000cca23b713c80")
            self.assertEqual(summary.sas_address, "0x5000cca23b713c81")
            self.assertEqual(summary.attached_sas_address, "0x500304801f715f3f")
            self.assertEqual(summary.negotiated_link_rate, "phy enabled; 12 Gbps")
            self.assertEqual(service.ssh_probe.commands, [])
            self.assertEqual(
                service.truenas_client.calls,
                [
                    ("da65", ("-a", "-j")),
                    ("da65", ("-x",)),
                ],
            )

    async def test_core_smart_summary_does_not_attempt_ssh_after_api_success(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...]]] = []

            async def fetch_disk_smartctl(self, disk_name: str, args: list[str] | None = None):
                self.calls.append((disk_name, tuple(args or [])))
                if args == ["-x"]:
                    return (
                        "Transport protocol:   SCSI\n"
                        "Logical Unit id:      0x5002538b103e71d0\n"
                        "Read Cache is:        Enabled\n"
                        "Writeback Cache is:   Enabled\n"
                    )
                return (
                    '{'
                    '"temperature":{"current":30},'
                    '"power_on_time":{"hours":30864},'
                    '"logical_block_size":512,'
                    '"physical_block_size":4096,'
                    '"rotation_rate":0,'
                    '"form_factor":{"name":"2.5 inches"}'
                    "}"
                )

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                return SSHCommandResult(
                    command=command,
                    ok=False,
                    stderr="ssh smartctl should not run after a successful CORE API summary",
                    exit_code=1,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=57,
                slot_label="57",
                row_index=0,
                column_index=0,
                device_name="multipath/disk27",
                smart_device_names=["da91", "da44", "multipath/disk27"],
                logical_block_size=512,
                physical_block_size=4096,
                logical_unit_id="5002538b103e71d0",
                multipath=MultipathView(
                    name="disk27",
                    device_name="multipath/disk27",
                    members=[
                        {"device_name": "da91", "state": "ACTIVE", "controller_label": "mpr1"},
                        {"device_name": "da44", "state": "PASSIVE", "controller_label": "mpr0"},
                    ],
                ),
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(57)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 30)
            self.assertEqual(summary.power_on_hours, 30864)
            self.assertEqual(summary.transport_protocol, "SCSI")
            self.assertEqual(summary.logical_unit_id, "5002538b103e71d0")
            self.assertIsNone(summary.message)
            self.assertEqual(service.ssh_probe.commands, [])
            self.assertEqual(
                service.truenas_client.calls,
                [
                    ("da91", ("-a", "-j")),
                    ("da91", ("-x",)),
                ],
            )

    async def test_core_smart_summary_falls_back_to_ssh_when_api_smartctl_fails(self) -> None:
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise TrueNASAPIError("api smartctl unavailable")

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                if command.endswith("-x /dev/da65"):
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            "Read Cache is:        Enabled\n"
                            "Writeback Cache is:   Disabled\n"
                        ),
                        exit_code=0,
                    )
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        '{'
                        '"temperature":{"current":28},'
                        '"power_on_time":{"hours":33037},'
                        '"logical_block_size":512,'
                        '"physical_block_size":4096,'
                        '"rotation_rate":7200,'
                        '"form_factor":{"name":"3.5 inches"},'
                        '"logical_unit_id":"0x5000cca2c272c1b8",'
                        '"scsi_transport_protocol":{"name":"SAS (SPL-4)"},'
                        '"scsi_sas_port_0":{'
                        '"phy_0":{'
                        '"attached_device_type":"expander device",'
                        '"negotiated_logical_link_rate":"phy enabled; 12 Gbps",'
                        '"sas_address":"0x5000cca2c272c1b9",'
                        '"attached_sas_address":"0x500304801f715f3f"'
                        '}'
                        '}'
                        "}"
                    ),
                    exit_code=0,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                device_name="da65",
                logical_block_size=512,
                physical_block_size=4096,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(0)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 28)
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, False)
            self.assertEqual(summary.transport_protocol, "SAS (SPL-4)")
            self.assertEqual(summary.sas_address, "0x5000cca2c272c1b9")
            self.assertEqual(summary.attached_sas_address, "0x500304801f715f3f")
            self.assertEqual(summary.negotiated_link_rate, "phy enabled; 12 Gbps")

    async def test_core_smart_summary_reports_missing_smartctl_sudo_permission_cleanly(self) -> None:
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise TrueNASAPIError("api smartctl unavailable")

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                return SSHCommandResult(
                    command=command,
                    ok=False,
                    stdout="",
                    stderr=(
                        "Sorry, user jbodmap is not allowed to execute "
                        "'/usr/local/sbin/smartctl -x -j /dev/da95' as root on The-Archive.gcs8.com."
                    ),
                    exit_code=1,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="archive-core",
                truenas=TrueNASConfig(platform="core"),
                ssh=SSHConfig(enabled=True, user="jbodmap"),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=95,
                slot_label="95",
                row_index=0,
                column_index=0,
                device_name="da95",
                temperature_c=31,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(95)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 31)
            self.assertIsNotNone(summary.message)
            assert summary.message is not None
            self.assertIn("jbodmap is missing sudo permission for smartctl", summary.message)
            self.assertIn("/usr/local/sbin/smartctl", summary.message)
            self.assertIn("/usr/sbin/smartctl", summary.message)

    async def test_scale_smart_summary_falls_back_to_ssh_smartctl(self) -> None:
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise AssertionError("SCALE smart summary should prefer SSH smartctl fallback in this test.")

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                if command.endswith("-x /dev/sdc"):
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            "Read Cache is:        Enabled\n"
                            "Writeback Cache is:   Disabled\n"
                        ),
                        exit_code=0,
                    )
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        '{'
                        '"temperature":{"current":35},'
                        '"power_on_time":{"hours":49119},'
                        '"logical_block_size":4096,'
                        '"rotation_rate":7200,'
                        '"form_factor":{"name":"3.5 inches"},'
                        '"scsi_error_counter_log":{'
                        '"read":{"gigabytes_processed":"330638.625"},'
                        '"write":{"gigabytes_processed":"111254.503"}'
                        '},'
                        '"logical_unit_id":"0x5000cca264d473d4",'
                        '"scsi_transport_protocol":{"name":"SAS (SPL-4)"},'
                        '"scsi_sas_port_0":{'
                        '"phy_0":{'
                        '"attached_device_type":"expander device",'
                        '"negotiated_logical_link_rate":"phy enabled; 12 Gbps",'
                        '"sas_address":"0x5000cca264d473d5",'
                        '"attached_sas_address":"0x5003048001c1043f"'
                        '}'
                        '},'
                        '"scsi_self_test_0":{'
                        '"code":{"string":"Background short"},'
                        '"result":{"string":"Completed"},'
                        '"power_on_time":{"hours":49108}'
                        "}"
                        "}"
                    ),
                    exit_code=0,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="offsite-scale",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                device_name="sdc",
                logical_block_size=512,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(0)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 35)
            self.assertEqual(summary.power_on_hours, 49119)
            self.assertEqual(summary.last_test_type, "Background short")
            self.assertEqual(summary.last_test_status, "Completed")
            self.assertEqual(summary.last_test_lifetime_hours, 49108)
            self.assertEqual(summary.last_test_age_hours, 11)
            self.assertEqual(summary.logical_block_size, 4096)
            self.assertEqual(summary.rotation_rate_rpm, 7200)
            self.assertEqual(summary.form_factor, "3.5 inches")
            self.assertEqual(summary.bytes_read, 330638625000000)
            self.assertEqual(summary.bytes_written, 111254503000000)
            self.assertEqual(summary.annualized_bytes_written, 19841394293043)
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, False)
            self.assertEqual(summary.transport_protocol, "SAS (SPL-4)")
            self.assertEqual(summary.logical_unit_id, "0x5000cca264d473d4")
            self.assertEqual(summary.sas_address, "0x5000cca264d473d5")
            self.assertEqual(summary.attached_sas_address, "0x5003048001c1043f")
            self.assertEqual(summary.negotiated_link_rate, "phy enabled; 12 Gbps")
            self.assertIsNone(summary.message)
            self.assertIn("/usr/sbin/smartctl -x -j /dev/sdc", service.ssh_probe.commands[0])
            self.assertIn("/usr/sbin/smartctl -x /dev/sdc", service.ssh_probe.commands[1])

    async def test_scale_smart_summary_accepts_smartctl_advisory_exit_with_valid_json(self) -> None:
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise AssertionError("SCALE smart summary should prefer SSH smartctl fallback in this test.")

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                if command.endswith("-x /dev/sdab"):
                    return SSHCommandResult(
                        command=command,
                        ok=False,
                        stdout=(
                            "Read Cache is:        Enabled\n"
                            "Writeback Cache is:   Enabled\n"
                        ),
                        exit_code=4,
                    )
                return SSHCommandResult(
                    command=command,
                    ok=False,
                    stdout=(
                        '{'
                        '"logical_block_size":4096,'
                        '"rotation_rate":0,'
                        '"form_factor":{"name":"2.5 inches"},'
                        '"logical_unit_id":"0x5000c5003e82533f",'
                        '"scsi_transport_protocol":{"name":"SAS (SPL-4)"},'
                        '"temperature":{"current":39},'
                        '"power_on_time":{"hours":797},'
                        '"scsi_sas_port_0":{'
                        '"phy_0":{'
                        '"attached_device_type":"expander device",'
                        '"negotiated_logical_link_rate":"phy enabled; 12 Gbps",'
                        '"sas_address":"0x5000c5003e82533d",'
                        '"attached_sas_address":"0x500304801e977aff"'
                        '}'
                        '},'
                        '"scsi_self_test_0":{'
                        '"code":{"string":"Background short"},'
                        '"result":{"string":"Completed"},'
                        '"power_on_time":{"hours":779}'
                        "}"
                        "}"
                    ),
                    exit_code=4,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="offsite-scale",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=3,
                slot_label="03",
                row_index=0,
                column_index=0,
                device_name="sdab",
                logical_block_size=512,
                physical_block_size=4096,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(3)

            self.assertTrue(summary.available)
            self.assertEqual(summary.temperature_c, 39)
            self.assertEqual(summary.power_on_hours, 797)
            self.assertEqual(summary.last_test_type, "Background short")
            self.assertEqual(summary.last_test_status, "Completed")
            self.assertEqual(summary.last_test_lifetime_hours, 779)
            self.assertEqual(summary.rotation_rate_rpm, 0)
            self.assertEqual(summary.form_factor, "2.5 inches")
            self.assertEqual(summary.read_cache_enabled, True)
            self.assertEqual(summary.writeback_cache_enabled, True)
            self.assertEqual(summary.transport_protocol, "SAS (SPL-4)")
            self.assertEqual(summary.logical_unit_id, "0x5000c5003e82533f")
            self.assertEqual(summary.sas_address, "0x5000c5003e82533d")
            self.assertEqual(summary.attached_sas_address, "0x500304801e977aff")
            self.assertEqual(summary.negotiated_link_rate, "phy enabled; 12 Gbps")
            self.assertIn("/usr/sbin/smartctl -x -j /dev/sdab", service.ssh_probe.commands[0])
            self.assertIn("/usr/sbin/smartctl -x /dev/sdab", service.ssh_probe.commands[1])

    async def test_linux_smart_summary_surfaces_nvme_wear_and_write_metrics(self) -> None:
        class DummyTrueNASClient:
            async def fetch_disk_smartctl(self, *_args, **_kwargs):
                raise AssertionError("Linux smart summary should use SSH smartctl fallback in this test.")

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                if command.endswith("-x /dev/nvme0n2"):
                    return SSHCommandResult(command=command, ok=True, stdout="", exit_code=0)
                if command == "sudo -n /usr/sbin/nvme smart-log -o json /dev/nvme0":
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            '{'
                            '"temperature":308,'
                            '"avail_spare":100,'
                            '"spare_thresh":5,'
                            '"percent_used":6,'
                            '"data_units_read":33056747326,'
                            '"data_units_written":4624969197,'
                            '"power_on_hours":32283,'
                            '"unsafe_shutdowns":61,'
                            '"media_errors":0'
                            "}"
                        ),
                        exit_code=0,
                    )
                if command == "sudo -n /usr/sbin/nvme id-ctrl -o json /dev/nvme0":
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            '{'
                            '"fr":"11300DR0",'
                            '"ver":66048,'
                            '"wctemp":348,'
                            '"cctemp":353'
                            "}"
                        ),
                        exit_code=0,
                    )
                if command == "sudo -n /usr/sbin/nvme id-ns -o json /dev/nvme0n2":
                    return SSHCommandResult(
                        command=command,
                        ok=True,
                        stdout=(
                            '{'
                            '"eui64":"00a075102b91c7cf",'
                            '"nguid":"000000000000001000a075012b91c7cf"'
                            "}"
                        ),
                        exit_code=0,
                    )
                return SSHCommandResult(
                    command=command,
                    ok=True,
                    stdout=(
                        '{'
                        '"device":{"protocol":"NVMe"},'
                        '"power_on_time":{"hours":32283},'
                        '"logical_block_size":4096,'
                        '"nvme_smart_health_information_log":{'
                        '"available_spare":100,'
                        '"available_spare_threshold":5,'
                        '"percentage_used":6,'
                        '"data_units_read":33056747323,'
                        '"data_units_written":4624968600,'
                        '"media_errors":0,'
                        '"unsafe_shutdowns":61'
                        '}'
                        "}"
                    ),
                    exit_code=0,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="gpu-server",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                device_name="nvme0",
                smart_device_names=["nvme0n2"],
                logical_block_size=4096,
            )
            service.get_snapshot = AsyncMock(
                return_value=InventorySnapshot(
                    slots=[slot],
                    refresh_interval_seconds=30,
                )
            )

            summary = await service.get_slot_smart_summary(0)

            self.assertTrue(summary.available)
            self.assertEqual(summary.transport_protocol, "NVMe")
            self.assertEqual(summary.rotation_rate_rpm, 0)
            self.assertEqual(summary.available_spare_percent, 100)
            self.assertEqual(summary.available_spare_threshold_percent, 5)
            self.assertEqual(summary.endurance_used_percent, 6)
            self.assertEqual(summary.endurance_remaining_percent, 94)
            self.assertEqual(summary.bytes_written, 2367983923200000)
            self.assertEqual(summary.estimated_remaining_bytes_written, 37098414796800000)
            self.assertEqual(summary.media_errors, 0)
            self.assertEqual(summary.unsafe_shutdowns, 61)
            self.assertEqual(summary.firmware_version, "11300DR0")
            self.assertEqual(summary.protocol_version, "1.2")
            self.assertEqual(summary.warning_temperature_c, 75)
            self.assertEqual(summary.critical_temperature_c, 80)
            self.assertEqual(summary.namespace_eui64, "eui.00a075102b91c7cf")
            self.assertEqual(summary.namespace_nguid, "000000000000001000a075012b91c7cf")
            self.assertIn("/usr/sbin/smartctl -x -j /dev/nvme0n2", service.ssh_probe.commands[0])
            self.assertIn("sudo -n /usr/sbin/nvme smart-log -o json /dev/nvme0", service.ssh_probe.commands)
            self.assertIn("sudo -n /usr/sbin/nvme id-ctrl -o json /dev/nvme0", service.ssh_probe.commands)
            self.assertIn("sudo -n /usr/sbin/nvme id-ns -o json /dev/nvme0n2", service.ssh_probe.commands)


class InventoryServiceMutationRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_snapshot_can_return_stale_cache_while_refresh_runs_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            stale_snapshot = InventorySnapshot(
                slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="da0")],
                refresh_interval_seconds=30,
            )
            fresh_snapshot = InventorySnapshot(
                slots=[SlotView(slot=1, slot_label="01", row_index=0, column_index=1, device_name="da1")],
                refresh_interval_seconds=30,
            )
            service._cache["__default__"] = stale_snapshot
            service._cache_until["__default__"] = datetime.now(timezone.utc) - timedelta(seconds=1)

            async def build_snapshot(
                *,
                selected_enclosure_id: str | None = None,
                force_source_refresh: bool = False,
            ) -> InventorySnapshot:
                await asyncio.sleep(0.01)
                return fresh_snapshot

            service._build_snapshot = AsyncMock(side_effect=build_snapshot)

            returned = await service.get_snapshot(allow_stale_cache=True)

            self.assertIs(returned, stale_snapshot)
            await asyncio.sleep(0.05)
            self.assertEqual(service._cache["__default__"].slots[0].device_name, "da1")

    async def test_background_refresh_for_one_enclosure_does_not_block_other_cached_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            stale_snapshot = InventorySnapshot(
                slots=[SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="da0")],
                refresh_interval_seconds=30,
            )
            fresh_snapshot = InventorySnapshot(
                slots=[SlotView(slot=1, slot_label="01", row_index=0, column_index=1, device_name="da1")],
                refresh_interval_seconds=30,
            )
            other_snapshot = InventorySnapshot(
                slots=[SlotView(slot=2, slot_label="02", row_index=0, column_index=2, device_name="da2")],
                refresh_interval_seconds=30,
            )
            service._cache["enc-a"] = stale_snapshot
            service._cache_until["enc-a"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            service._cache["enc-b"] = other_snapshot
            service._cache_until["enc-b"] = datetime.now(timezone.utc) + timedelta(seconds=30)
            build_started = asyncio.Event()
            release_build = asyncio.Event()

            async def build_snapshot(
                *,
                selected_enclosure_id: str | None = None,
                force_source_refresh: bool = False,
            ) -> InventorySnapshot:
                if selected_enclosure_id == "enc-a":
                    build_started.set()
                    await release_build.wait()
                return fresh_snapshot

            service._build_snapshot = AsyncMock(side_effect=build_snapshot)

            returned = await service.get_snapshot(selected_enclosure_id="enc-a", allow_stale_cache=True)

            self.assertIs(returned, stale_snapshot)
            await asyncio.wait_for(build_started.wait(), timeout=0.1)
            other = await asyncio.wait_for(service.get_snapshot(selected_enclosure_id="enc-b"), timeout=0.05)
            self.assertIs(other, other_snapshot)
            release_build.set()
            await asyncio.sleep(0.05)

    async def test_concurrent_source_bundle_requests_share_one_fetch(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.fetch_all_calls = 0

            async def fetch_all(self) -> TrueNASRawData:
                self.fetch_all_calls += 1
                await asyncio.sleep(0.02)
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            truenas_client = DummyTrueNASClient()
            service = build_inventory_service(
                settings,
                system,
                truenas_client,
                AsyncMock(),
                temp_dir,
            )

            first, second = await asyncio.gather(
                service._get_inventory_source_bundle(),
                service._get_inventory_source_bundle(),
            )

            self.assertEqual(truenas_client.fetch_all_calls, 1)
            self.assertIs(first, second)

    async def test_quantastor_refresh_preserves_last_trusted_topology_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="quantastor-lab", truenas=TrueNASConfig(platform="quantastor"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            trusted_snapshot = InventorySnapshot(
                slots=[
                    SlotView(
                        slot=0,
                        slot_label="00",
                        row_index=0,
                        column_index=0,
                        device_name="disk0",
                        pool_name="HA-Pool-R10",
                        vdev_name="mirror-0",
                        vdev_class="data",
                        topology_label="HA-Pool-R10 > mirror-0 > data",
                    )
                ],
                refresh_interval_seconds=30,
                selected_system_id=system.id,
                selected_system_platform="quantastor",
                selected_enclosure_id="node-a",
                platform_context={"topology_complete": True},
            )
            incomplete_snapshot = InventorySnapshot(
                slots=[
                    SlotView(
                        slot=0,
                        slot_label="00",
                        row_index=0,
                        column_index=0,
                        device_name="disk0",
                        pool_name="HA-Pool-R10",
                        vdev_name="disk",
                        vdev_class="data",
                        topology_label="HA-Pool-R10 > disk > data",
                    )
                ],
                refresh_interval_seconds=30,
                selected_system_id=system.id,
                selected_system_platform="quantastor",
                selected_enclosure_id="node-a",
                platform_context={"topology_complete": False},
            )
            service._cache["node-a"] = trusted_snapshot
            service._cache_until["node-a"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            service._build_snapshot = AsyncMock(return_value=incomplete_snapshot)

            returned = await service.get_snapshot(selected_enclosure_id="node-a")

            self.assertIs(returned, trusted_snapshot)
            self.assertIs(service._cache["node-a"], trusted_snapshot)
            self.assertEqual(service._cache["node-a"].slots[0].vdev_name, "mirror-0")

    async def test_get_snapshot_reuses_shared_source_bundle_across_enclosure_switches(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.fetch_all_calls = 0

            async def fetch_all(self) -> TrueNASRawData:
                self.fetch_all_calls += 1
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            truenas_client = DummyTrueNASClient()
            service = build_inventory_service(
                settings,
                system,
                truenas_client,
                AsyncMock(),
                temp_dir,
            )

            await service.get_snapshot(force_refresh=True, selected_enclosure_id="enc-a")
            await service.get_snapshot(selected_enclosure_id="enc-b")

            self.assertEqual(truenas_client.fetch_all_calls, 1)

    async def test_invalidating_source_bundle_forces_fresh_api_fetch(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.fetch_all_calls = 0

            async def fetch_all(self) -> TrueNASRawData:
                self.fetch_all_calls += 1
                return TrueNASRawData(
                    enclosures=[],
                    disks=[],
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            truenas_client = DummyTrueNASClient()
            service = build_inventory_service(
                settings,
                system,
                truenas_client,
                AsyncMock(),
                temp_dir,
            )

            await service.get_snapshot(force_refresh=True)
            service.invalidate_snapshot_cache(reason="test.invalidate", invalidate_source_bundle=True)
            await service.get_snapshot()

            self.assertEqual(truenas_client.fetch_all_calls, 2)

    async def test_get_slot_smart_summary_can_use_persisted_stable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="enc-1",
                present=True,
                state=SlotState.healthy,
                device_name="da0",
                logical_unit_id="5000c500",
                sas_address="5000c500",
            )
            service.slot_detail_store.save_entries(
                [
                    SlotDetailCacheEntry(
                        system_id=system.id,
                        enclosure_id="enc-1",
                        slot=0,
                        identifiers=["da0", "5000c500"],
                        smart_fields={
                            "available": True,
                            "transport_protocol": "SAS",
                            "rotation_rate_rpm": 7200,
                            "logical_unit_id": "5000c500",
                        },
                    )
                ]
            )
            service._schedule_background_smart_refresh = lambda *args, **kwargs: None

            summary = await service._get_slot_smart_summary_for_slot_view(slot, allow_stale_cache=True)

            self.assertTrue(summary.available)
            self.assertEqual(summary.transport_protocol, "SAS")
            self.assertEqual(summary.rotation_rate_rpm, 7200)
            self.assertEqual(summary.logical_unit_id, "5000c500")

    async def test_set_slot_led_can_defer_invalidation_without_second_snapshot(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int, str]] = []

            async def set_slot_status(self, enclosure_id: str, api_slot_number: int, action: str) -> None:
                self.calls.append((enclosure_id, api_slot_number, action))

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            truenas_client = DummyTrueNASClient()
            service = build_inventory_service(
                settings,
                system,
                truenas_client,
                AsyncMock(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="enc-1",
                led_supported=True,
                led_backend="api",
            )
            snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=30)
            service.get_snapshot = AsyncMock(return_value=snapshot)
            service._cache["__default__"] = snapshot
            service._cache_until["__default__"] = datetime.now(timezone.utc) + timedelta(seconds=30)

            await service.set_slot_led(0, LedAction.identify, invalidate_snapshot=False)

            self.assertEqual(service.get_snapshot.await_count, 1)
            self.assertIn("__default__", service._cache)
            self.assertEqual(truenas_client.calls, [("enc-1", 1, LedAction.identify.value)])

    async def test_set_slot_led_default_invalidation_clears_source_bundle(self) -> None:
        class DummyTrueNASClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int, str]] = []

            async def set_slot_status(self, enclosure_id: str, api_slot_number: int, action: str) -> None:
                self.calls.append((enclosure_id, api_slot_number, action))

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            truenas_client = DummyTrueNASClient()
            service = build_inventory_service(
                settings,
                system,
                truenas_client,
                AsyncMock(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="enc-1",
                led_supported=True,
                led_backend="api",
            )
            snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=30)
            service.get_snapshot = AsyncMock(return_value=snapshot)
            service._cache["__default__"] = snapshot
            service._cache_until["__default__"] = datetime.now(timezone.utc) + timedelta(seconds=30)
            service._source_bundle = object()  # type: ignore[assignment]
            service._source_bundle_until = datetime.now(timezone.utc) + timedelta(seconds=30)

            await service.set_slot_led(0, LedAction.identify)

            self.assertEqual(truenas_client.calls, [("enc-1", 1, LedAction.identify.value)])
            self.assertIsNone(service._source_bundle)

    async def test_save_mapping_invalidates_cache_without_second_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="enc-1",
            )
            snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=30)
            service.get_snapshot = AsyncMock(return_value=snapshot)
            service._cache["__default__"] = snapshot
            service._cache_until["__default__"] = datetime.now(timezone.utc) + timedelta(seconds=30)

            saved = await service.save_mapping(0, {"serial": "SER123"})

            self.assertEqual(service.get_snapshot.await_count, 1)
            self.assertEqual(saved.serial, "SER123")
            self.assertEqual(
                service.mapping_store.get_mapping(system.id, "enc-1", 0).serial,
                "SER123",
            )
            self.assertEqual(service._cache, {})

    async def test_clear_mapping_can_defer_invalidation_without_second_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="enc-1",
            )
            snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=30)
            service.get_snapshot = AsyncMock(return_value=snapshot)
            service._cache["__default__"] = snapshot
            service._cache_until["__default__"] = datetime.now(timezone.utc) + timedelta(seconds=30)
            service.mapping_store.save_mapping(
                ManualMapping(system_id=system.id, enclosure_id="enc-1", slot=0, serial="SER123")
            )

            cleared = await service.clear_mapping(0, invalidate_snapshot=False)

            self.assertTrue(cleared)
            self.assertEqual(service.get_snapshot.await_count, 1)
            self.assertIsNone(service.mapping_store.get_mapping(system.id, "enc-1", 0))
            self.assertIn("__default__", service._cache)

    async def test_import_mapping_bundle_can_defer_invalidation_without_snapshot_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(id="default", truenas=TrueNASConfig(platform="core"))
            service = build_inventory_service(
                settings,
                system,
                AsyncMock(),
                AsyncMock(),
                temp_dir,
            )
            service.get_snapshot = AsyncMock()
            service._cache["__default__"] = InventorySnapshot(slots=[], refresh_interval_seconds=30)
            service._cache_until["__default__"] = datetime.now(timezone.utc) + timedelta(seconds=30)

            imported = await service.import_mapping_bundle(
                MappingBundle(
                    mappings=[
                        ManualMapping(slot=0, enclosure_id="enc-1", serial="SER123"),
                    ]
                ),
                invalidate_snapshot=False,
            )

            self.assertEqual(imported, 1)
            self.assertEqual(service.get_snapshot.await_count, 0)
            self.assertIn("__default__", service._cache)


class InventoryServiceLedTests(unittest.IsolatedAsyncioTestCase):
    async def test_unifi_led_control_uses_sata_led_sm_set_fault(self) -> None:
        class DummyTrueNASClient:
            pass

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                return SSHCommandResult(command=command, ok=True, stdout="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="unvr",
                default_profile_id=UNIFI_UNVR_FRONT_4_PROFILE_ID,
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True, user="root"),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                led_supported=True,
                led_backend="unifi_fault",
                raw_status={"vendor_slot_number": 1},
            )

            await service._set_unifi_slot_led_over_ssh(slot, LedAction.identify)
            await service._set_unifi_slot_led_over_ssh(slot, LedAction.clear)

            self.assertIn(
                "python3 -c 'from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, True)'",
                service.ssh_probe.commands,
            )
            self.assertIn(
                "python3 -c 'from ustd.hwmon import sata_led_sm; sata_led_sm.set_fault(1, False)'",
                service.ssh_probe.commands,
            )

    async def test_scale_sg_ses_led_control_uses_slot_number(self) -> None:
        class DummyTrueNASClient:
            pass

        class DummySSHProbe:
            def __init__(self) -> None:
                self.commands: list[str] = []

            async def run_command(self, command: str) -> SSHCommandResult:
                self.commands.append(command)
                return SSHCommandResult(command=command, ok=True, stdout="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="offsite-scale",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                led_supported=True,
                led_backend="scale_sg_ses",
                ssh_ses_targets=[
                    {
                        "ses_device": "/dev/sg27",
                        "ses_element_id": 0,
                        "ses_slot_number": 0,
                    }
                ],
            )

            await service._set_slot_led_over_ssh(slot, LedAction.identify)
            await service._set_slot_led_over_ssh(slot, LedAction.clear)

            self.assertIn(
                "sudo -n /usr/bin/sg_ses --dev-slot-num=0 --set=ident /dev/sg27",
                service.ssh_probe.commands,
            )
            self.assertIn(
                "sudo -n /usr/bin/sg_ses --dev-slot-num=0 --clear=ident /dev/sg27",
                service.ssh_probe.commands,
            )

    async def test_quantastor_sg_ses_led_control_uses_target_host_override(self) -> None:
        class DummyTrueNASClient:
            pass

        class DummySSHProbe:
            async def run_command(self, command: str) -> SSHCommandResult:
                return SSHCommandResult(command=command, ok=True, stdout="", exit_code=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="quantastor-lab",
                truenas=TrueNASConfig(platform="quantastor"),
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap"),
            )
            service = build_inventory_service(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                temp_dir,
            )
            service._run_ssh_command = AsyncMock(
                return_value=SSHCommandResult(command="", ok=True, stdout="", exit_code=0)
            )
            slot = SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                led_supported=True,
                led_backend="quantastor_sg_ses",
                ssh_ses_targets=[
                    {
                        "ssh_host": "10.0.0.20",
                        "ses_device": "/dev/sg11",
                        "ses_element_id": 0,
                        "ses_slot_number": 0,
                    }
                ],
            )

            await service._set_slot_led_over_ssh(slot, LedAction.identify)
            await service._set_slot_led_over_ssh(slot, LedAction.clear)

            awaited = [call.args for call in service._run_ssh_command.await_args_list]
            self.assertIn(
                ("sudo -n /usr/bin/sg_ses --dev-slot-num=0 --set=ident /dev/sg11", "10.0.0.20"),
                awaited,
            )
            self.assertIn(
                ("sudo -n /usr/bin/sg_ses --dev-slot-num=0 --clear=ident /dev/sg11", "10.0.0.20"),
                awaited,
            )


if __name__ == "__main__":
    unittest.main()
