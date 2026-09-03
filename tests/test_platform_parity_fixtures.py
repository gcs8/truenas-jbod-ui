from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.services.inventory import LINUX_ENCLOSURE_SYSFS_MAP_COMMAND, InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_sg_ses_join_filter, parse_ssh_outputs, parse_storcli_physical_drives
from app.services.profile_registry import (
    ProfileRegistry,
    SCALE_SSG_FRONT_24_PROFILE_ID,
    SCALE_SSG_REAR_12_PROFILE_ID,
)
from app.services.quantastor_api import QuantastorRESTClient
from app.services.slot_detail_store import SlotDetailStore
from app.services.truenas_ws import TrueNASRawData

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "platform_parity"

def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")

def fixture_json(name: str):
    return json.loads(fixture_text(name))

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
        None,
        MappingStore(f"{temp_dir}\\slot_mappings.json"),
        ProfileRegistry(settings),
        SlotDetailStore(f"{temp_dir}\\slot_detail_cache.json"),
    )

class PlatformParityFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_scale_empty_middleware_rows_can_render_linux_ses_fixture_pack(self) -> None:
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
                ssh=SSHConfig(enabled=True, host="10.0.0.10", user="jbodmap", commands=[]),
            )
            service = build_inventory_service(settings, system, DummyScaleClient(), AsyncMock(), temp_dir)
            ses_overlay = parse_ssh_outputs(
                {
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg26": fixture_text("scale_sg26_aes.txt"),
                    "sudo -n /usr/bin/sg_ses -p ec /dev/sg26": fixture_text("scale_sg26_ec.txt"),
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg37": fixture_text("scale_sg37_aes.txt"),
                    "sudo -n /usr/bin/sg_ses -p ec /dev/sg37": fixture_text("scale_sg37_ec.txt"),
                },
                60,
                None,
                None,
            )
            service._tag_ses_overlay(ses_overlay, "10.0.0.10")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(ses_overlay, []))

            by_device = {enclosure.ses_device: enclosure for enclosure in ses_overlay.ses_enclosures}
            self.assertEqual(by_device["/dev/sg26"].profile_id, SCALE_SSG_FRONT_24_PROFILE_ID)
            self.assertEqual(by_device["/dev/sg37"].profile_id, SCALE_SSG_REAR_12_PROFILE_ID)

            snapshot = await service.get_snapshot()

            self.assertEqual(snapshot.sources["ssh"].message, "SSH probe and SCALE SES rediscovery completed.")
            self.assertEqual(len(snapshot.enclosures), 2)
            self.assertEqual(snapshot.selected_profile.id, SCALE_SSG_FRONT_24_PROFILE_ID)
            self.assertEqual(snapshot.layout_slot_count, 24)
            slot0 = next(slot for slot in snapshot.slots if slot.slot == 0)
            self.assertEqual(slot0.led_backend, "scale_sg_ses")
            self.assertEqual(slot0.ssh_ses_targets[0]["ses_device"], "/dev/sg26")
            self.assertTrue(slot0.identify_active)
            self.assertIn(
                "TrueNAS SCALE did not return enclosure rows, so this view is using Linux SES AES page parsing "
                "for slot mapping on the selected enclosure.",
                snapshot.warnings,
            )

    async def test_quantastor_optional_endpoint_failures_keep_required_rest_inventory(self) -> None:
        fixture = fixture_json("quantastor_optional_failures.json")

        class FixtureQuantastorClient(QuantastorRESTClient):
            def _request_json(self, endpoint: str, params=None):
                return fixture[endpoint]

        client = FixtureQuantastorClient(
            TrueNASConfig(
                platform="quantastor",
                host="https://quantastor.example.test",
                api_user="admin",
                api_password="secret",
            )
        )

        payload = await client.fetch_all()

        self.assertEqual([system["id"] for system in payload.systems], ["node-a", "node-b"])
        self.assertEqual(payload.disks[0]["serial"], "QSPARITY0001")
        self.assertEqual(payload.pools[0]["name"], "bulk")
        self.assertEqual(payload.pool_devices, [])
        self.assertEqual(payload.ha_groups, [])
        self.assertEqual(payload.hw_disks[0]["slot"], "01")
        self.assertEqual(payload.hw_enclosures, [])

    def test_linux_nvme_mdadm_fixture_keeps_storage_identity_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="linux-nvme-mdadm",
                truenas=TrueNASConfig(platform="linux"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(Settings(systems=[system]), system, AsyncMock(), AsyncMock(), temp_dir)
            ssh_data = parse_ssh_outputs(
                {
                    "lsblk -OJ": fixture_text("linux_lsblk.json"),
                    "sudo -n /usr/sbin/mdadm --detail --scan": fixture_text("linux_mdadm.txt"),
                    "/usr/sbin/nvme list-subsys -o json": fixture_text("linux_nvme_subsystems.json"),
                },
                4,
                None,
                None,
            )

            records = service._build_linux_disk_records(ssh_data)
            by_device = {record.device_name: record for record in records}

            self.assertIn("md127", ssh_data.linux_mdadm_arrays)
            self.assertEqual(ssh_data.linux_mdadm_arrays["md127"].name, "linux-parity:nvme-data")
            self.assertEqual(by_device["nvme0"].path_device_name, "nvme0n2")
            self.assertEqual(by_device["nvme0"].pool_name, "/srv/nvme-array")
            self.assertEqual(by_device["nvme0"].raw["top_array_name"], "md127")
            self.assertEqual(by_device["nvme0"].raw["transport_address"], "0000:5e:00.0")
            self.assertEqual(by_device["nvme0"].smart_devices, ["nvme0n2", "nvme0n1"])
            self.assertEqual(by_device["nvme1"].path_device_name, "nvme1n1")
            self.assertEqual(by_device["nvme1"].pool_name, "/srv/nvme-array")

    def test_esxi_non_c0_storcli_fixture_maps_controller_and_virtual_drive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            system = SystemConfig(
                id="esxi-c1",
                truenas=TrueNASConfig(platform="esxi"),
                ssh=SSHConfig(enabled=True),
            )
            service = build_inventory_service(Settings(systems=[system]), system, AsyncMock(), AsyncMock(), temp_dir)
            ssh_data = parse_ssh_outputs(
                {
                    "/opt/lsi/storcli64/storcli64 /c1 show all J": fixture_text("esxi_storcli_c1_controller.json"),
                    "/opt/lsi/storcli64/storcli64 /c1/vall show all J": fixture_text("esxi_storcli_c1_vall.json"),
                    "/opt/lsi/storcli64/storcli64 /c1/eall/sall show all J": fixture_text("esxi_storcli_c1_physical.json"),
                },
                8,
                None,
                None,
            )
            ssh_data.esxi_storage_devices = [
                {
                    "id": "naa.600605b00abc00000000000000000041",
                    "display_name": "Local RAID Disk (naa.600605b00abc00000000000000000041)",
                    "devfs_path": "/vmfs/devices/disks/naa.600605b00abc00000000000000000041",
                    "other_uids": "vml.0200000000600605b00abc00000000000000000041",
                    "is_local": "true",
                    "drive_type": "logical",
                    "raid_level": "RAID1",
                }
            ]
            ssh_data.esxi_storage_paths = [
                {
                    "device": "naa.600605b00abc00000000000000000041",
                    "runtime_name": "vmhba3:C1:T41:L0",
                    "target": "41",
                    "transport": "sas",
                    "state": "active",
                }
            ]

            records = service._build_esxi_disk_records(ssh_data)

            self.assertEqual(ssh_data.esxi_storcli_controller["Basics"]["Controller"], 1)
            self.assertEqual(ssh_data.esxi_storcli_physical_drives[0]["controller_id"], "c1")
            self.assertEqual(ssh_data.esxi_storcli_virtual_drives[0]["name"], "ESXi-Data")
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.raw["controller_id"], "c1")
            self.assertEqual(record.pool_name, "ESXi-Data")
            self.assertEqual(record.lunid, "naa.600605b00abc00000000000000000041")
            self.assertEqual(record.raw["esxi_runtime_name"], "vmhba3:C1:T41:L0")
            self.assertIn("/c1/e252/s7", record.lookup_keys)

    def test_esxi_multi_controller_physical_fixture_does_not_blend_same_slot_details(self) -> None:
        drives = parse_storcli_physical_drives(fixture_text("esxi_storcli_multi_controller_physical.json"))
        by_controller = {drive["controller_id"]: drive for drive in drives}

        self.assertEqual(by_controller["c0"]["slot_key"], "252:7")
        self.assertEqual(by_controller["c0"]["serial"], "ZC0PARITY")
        self.assertEqual(by_controller["c0"]["firmware"], "A3A0")
        self.assertEqual(by_controller["c1"]["slot_key"], "252:7")
        self.assertEqual(by_controller["c1"]["serial"], "ZC1PARITY")
        self.assertEqual(by_controller["c1"]["firmware"], "SN03")

    async def test_scale_shared_sata_aes_uses_enclosure_driver_mapping(self) -> None:
        """
        Issue #119: SATA-heavy shelves whose expander stamps one shared SAS
        address into every AES descriptor must not stay unmapped (or worse,
        map wrong). The kernel enclosure-driver bindings provide the per-bay
        device names, and the shared address is demoted to display evidence.
        """

        disks = [
            {"devname": "sdaa", "name": "sdaa", "serial": "SYNTHSATA001", "model": "EXAMPLE-HDD", "lunid": "5bbbbbbb00001000"},
            {"devname": "sdab", "name": "sdab", "serial": "SYNTHSATA002", "model": "EXAMPLE-HDD", "lunid": "5bbbbbbb00001002"},
            {"devname": "sdac", "name": "sdac", "serial": "SYNTHSATA003", "model": "EXAMPLE-HDD", "lunid": "5bbbbbbb00002000"},
            {"devname": "sdad", "name": "sdad", "serial": "SYNTHSATA004", "model": "EXAMPLE-HDD", "lunid": "5bbbbbbb00003000"},
            {"devname": "sdae", "name": "sdae", "serial": "SYNTHSAS0005", "model": "EXAMPLE-SAS", "lunid": "5aaaaaaa00000d04"},
        ]

        class DummyScaleClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[],
                    disks=disks,
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="shared-sata-scale",
                label="Shared SATA SCALE",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True, host="10.0.0.11", user="jbodmap", commands=[]),
            )
            service = build_inventory_service(settings, system, DummyScaleClient(), AsyncMock(), temp_dir)
            ses_overlay = parse_ssh_outputs(
                {
                    "sudo -n /usr/bin/sg_ses -p aes /dev/sg84": fixture_text("scale_shared_sata_aes.txt"),
                    LINUX_ENCLOSURE_SYSFS_MAP_COMMAND: fixture_text("scale_shared_sata_sysfs.txt"),
                },
                6,
                None,
                None,
            )
            service._tag_ses_overlay(ses_overlay, "10.0.0.11")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(ses_overlay, []))

            snapshot = await service.get_snapshot()

            slots_by_number = {slot.slot: slot for slot in snapshot.slots}
            # The kernel enclosure-driver bindings map every SATA bay even
            # though AES reports the expander's address for all of them.
            self.assertEqual(slots_by_number[0].device_name, "sdaa")
            self.assertEqual(slots_by_number[1].device_name, "sdab")
            self.assertEqual(slots_by_number[2].device_name, "sdac")
            self.assertEqual(slots_by_number[3].device_name, "sdad")
            self.assertEqual(slots_by_number[4].device_name, "sdae")
            # Adjacent-lunid disks must never swap slots via shifted aliases.
            self.assertEqual(slots_by_number[0].serial, "SYNTHSATA001")
            self.assertEqual(slots_by_number[1].serial, "SYNTHSATA002")
            # The shared address is not presented as a per-bay SAS address.
            self.assertIsNone(slots_by_number[0].sas_address)
            self.assertIsNone(slots_by_number[1].sas_address)
            # The empty bay stays empty instead of ghosting as populated.
            self.assertFalse(slots_by_number[5].present)
            self.assertIsNone(slots_by_number[5].device_name)
            self.assertTrue(
                any("shared SAS address" in warning for warning in snapshot.warnings),
                snapshot.warnings,
            )

    def test_scale_md1280_join_captures_parse_all_reported_bays(self) -> None:
        for dev in ("sg1", "sg76"):
            with self.subTest(dev=dev):
                parsed = parse_sg_ses_join_filter(
                    fixture_text(f"scale_md1280_{dev}_join.txt"),
                    f"sg_ses join /dev/{dev}",
                )

                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(list(parsed.slots), list(range(84)))
                slot = parsed.slots[73]
                self.assertEqual(slot.slot_number_source, "ses_device_slot_number")
                self.assertEqual(
                    slot.control_targets,
                    [
                        {
                            "ses_device": f"/dev/{dev}",
                            "ses_element_id": 73,
                            "ses_slot_number": 73,
                        }
                    ],
                )
                self.assertEqual(slot.presence_source, "sg_ses_join")

    async def test_scale_md1280_real_captures_map_via_enclosure_driver(self) -> None:
        """
        Issue #119 acceptance: real (pseudonymised) captures from the
        reporter's Dell EN-8435A / MD1280 (Xyratex 5U84) shelves. AES reports
        per-phy addresses unrelated to the drives, SATA bays claim "no SAS
        device attached", and every empty bay's descriptor is flagged invalid
        while the EC page latches Critical onto it. The kernel
        enclosure-driver map must still resolve every populated bay, empties
        must stay empty, and the shelf must keep its full 84-bay geometry.
        """

        probe_output = fixture_text("scale_md1280_sysfs.txt")
        truth: dict[str, dict[int, str]] = {}
        for line in probe_output.splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) == 5:
                truth.setdefault(parts[1], {})[int(parts[2])] = parts[4]

        lsblk = fixture_json("scale_md1280_lsblk.json")["blockdevices"]
        disks = []
        for row in lsblk:
            if row.get("type") not in (None, "disk"):
                continue
            wwn = row.get("wwn") or ""
            disks.append(
                {
                    "devname": row.get("name"),
                    "name": row.get("name"),
                    "serial": row.get("serial"),
                    "model": row.get("model") or "FIXTURE-DISK",
                    "lunid": wwn.removeprefix("0x") if wwn else None,
                }
            )

        class DummyScaleClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[],
                    disks=disks,
                    pools=[],
                    disk_temperatures={},
                    smart_test_results=[],
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="md1280-parity",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True, host="192.0.2.50", user="jbodmap", commands=[]),
            )
            service = build_inventory_service(settings, system, DummyScaleClient(), AsyncMock(), temp_dir)

            from app.services.inventory import LINUX_ENCLOSURE_SYSFS_MAP_COMMAND
            from app.services.parsers import ParsedSSHData

            overlay = ParsedSSHData()
            logical_by_sg: dict[str, str] = {}
            for dev in ("sg1", "sg76"):
                aes_text = fixture_text(f"scale_md1280_{dev}_aes.txt")
                logical_by_sg[dev] = aes_text.splitlines()[1].split(":", 1)[1].strip()
                parsed = parse_ssh_outputs(
                    {
                        f"sudo -n /usr/bin/sg_ses -p aes /dev/{dev}": aes_text,
                        f"sudo -n /usr/bin/sg_ses -p ec /dev/{dev}": fixture_text(f"scale_md1280_{dev}_ec.txt"),
                        f"sudo -n /usr/bin/sg_ses --join --filter /dev/{dev}": fixture_text(f"scale_md1280_{dev}_join.txt"),
                        LINUX_ENCLOSURE_SYSFS_MAP_COMMAND: probe_output,
                    },
                    84,
                    None,
                    None,
                )
                overlay = service._merge_ses_overlay_data(overlay, parsed)
            service._tag_ses_overlay(overlay, "192.0.2.50")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(overlay, []))

            for dev in ("sg1", "sg76"):
                snap = await service.get_snapshot(selected_enclosure_id=logical_by_sg[dev])
                resolved = {slot.slot: slot for slot in snap.slots}
                gt = truth[dev]

                self.assertEqual(snap.layout_slot_count, 84)
                selected = next(e for e in snap.enclosures if e.id == logical_by_sg[dev])
                self.assertEqual(selected.slot_count, 84)

                for slot_number, device in sorted(gt.items()):
                    view = resolved.get(slot_number)
                    self.assertIsNotNone(view, f"{dev} slot {slot_number} missing from snapshot")
                    assert view is not None
                    self.assertEqual(
                        view.device_name,
                        device,
                        f"{dev} slot {slot_number} resolved {view.device_name!r}, expected {device!r}",
                    )
                    self.assertTrue(view.present, f"{dev} slot {slot_number} should be present")
                    self.assertEqual(str(view.state), "SlotState.healthy")

                empty_views = [view for view in resolved.values() if view.slot not in gt]
                self.assertEqual(len(empty_views), 84 - len(gt))
                for view in empty_views:
                    self.assertFalse(
                        view.present,
                        f"{dev} empty slot {view.slot} phantom-present (status={view.raw_status.get('status')!r})"
                        if hasattr(view, "raw_status")
                        else f"{dev} empty slot {view.slot} phantom-present",
                    )
                    self.assertEqual(str(view.state), "SlotState.empty")

                self.assertFalse(
                    [w for w in snap.warnings if "shared SAS address" in w],
                    "per-phy unique addresses must not trip the shared-address guard",
                )

    def test_scale_md1280_fixture_identifiers_have_deterministic_pseudonyms(self) -> None:
        rows = fixture_json("scale_md1280_lsblk.json")["blockdevices"]
        serials = [str(row["serial"]) for row in rows if row.get("serial")]
        self.assertEqual(len(serials), 105)
        self.assertEqual(len(set(serials)), 105)
        self.assertTrue(all(re.fullmatch(r"[A-Z]{6}[0-9]{4}", value) for value in serials))

        wwns = [str(row["wwn"]) for row in rows if row.get("wwn")]
        standard_sas = [
            value
            for value in wwns
            if re.fullmatch(r"0x[0-9a-fA-F]{16}", value)
        ]
        explicit_nvme = [value for value in wwns if value not in standard_sas]
        self.assertEqual(
            explicit_nvme,
            ["FIXTURE-NVME-WWN-001", "FIXTURE-NVME-WWN-002"],
        )
        self.assertEqual(len(standard_sas), 102)
        self.assertEqual(len(set(standard_sas)), 102)
        sorted_sas = sorted(int(value[2:], 16) for value in standard_sas)
        self.assertEqual(
            Counter(right - left for left, right in zip(sorted_sas, sorted_sas[1:])),
            Counter({1: 91, 7: 9, 103: 1}),
        )


    async def test_md1280_enclosure_offers_per_drawer_sub_views(self) -> None:
        """
        The MD1280 selector must offer both drawers as their own views in
        addition to the whole shelf, and a drawer view must render exactly its
        own 42 bays with chassis-true 1-based labels.
        """

        aes_output = "\n".join(
            [
                "  DELL      EN-8435A-E6EBD    3535",
                "  Primary enclosure logical identifier (hex): 5eeeeeee00000084",
                "Additional element status diagnostic page:",
                "  additional element status descriptor list",
                "    Element type: Array device slot, subenclosure id: 0 [ti=0]",
            ]
            + [
                line
                for slot in (0, 1, 43, 44)
                for line in (
                    f"      Element index: {slot}  eiioe=0",
                    "        Transport protocol: SAS",
                    f"        number of phys: 1, not all phys: 1, device slot number: {slot}",
                    "        phy index: 0",
                    "          SAS device type: no SAS device attached",
                    "          target port for: SATA_device",
                    "          attached SAS address: 0x5eeeeeee00000001",
                    f"          SAS address: 0x5eeeeeee000000{50 + slot:02x}",
                )
            ]
        )

        class DummyScaleClient:
            async def fetch_all(self) -> TrueNASRawData:
                return TrueNASRawData(
                    enclosures=[], disks=[], pools=[], disk_temperatures={}, smart_test_results=[]
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings()
            system = SystemConfig(
                id="md1280-drawers",
                truenas=TrueNASConfig(platform="scale"),
                ssh=SSHConfig(enabled=True, host="192.0.2.51", user="jbodmap", commands=[]),
            )
            service = build_inventory_service(settings, system, DummyScaleClient(), AsyncMock(), temp_dir)
            overlay = parse_ssh_outputs(
                {"sudo -n /usr/bin/sg_ses -p aes /dev/sg9": aes_output}, 84, None, None
            )
            service._tag_ses_overlay(overlay, "192.0.2.51")
            service._fetch_scale_ses_overlay = AsyncMock(return_value=(overlay, []))

            snapshot = await service.get_snapshot()
            labels = [option.label for option in snapshot.enclosures]
            self.assertEqual(
                labels,
                [
                    "Dell MD1280 Drawer 1-42 (Top)",
                    "Dell MD1280 Drawer 43-84 (Bottom)",
                    "Dell MD1280 84 Bay",
                ],
            )
            top, bottom, full = snapshot.enclosures
            self.assertEqual(top.id, "5eeeeeee00000084::dell-md1280-drawer-top-42")
            self.assertEqual(bottom.id, "5eeeeeee00000084::dell-md1280-drawer-bottom-42")
            self.assertEqual(full.id, "5eeeeeee00000084")
            self.assertEqual((top.slot_count, bottom.slot_count), (42, 42))
            self.assertEqual(snapshot.selected_enclosure_id, top.id)
            self.assertEqual(snapshot.selected_enclosure_label, top.label)
            self.assertIsNotNone(snapshot.selected_profile)
            assert snapshot.selected_profile is not None
            self.assertEqual(snapshot.selected_profile.id, "dell-md1280-drawer-top-42")
            self.assertEqual(snapshot.layout_slot_count, top.slot_count)
            self.assertEqual(len(snapshot.slots), top.slot_count)

            with patch.object(
                service,
                "_resolve_disk_for_slot",
                wraps=service._resolve_disk_for_slot,
            ) as resolve_disk:
                bottom_snap = await service.get_snapshot(selected_enclosure_id=bottom.id)
            self.assertEqual(
                {call.args[1] for call in resolve_disk.call_args_list},
                {"5eeeeeee00000084"},
            )
            slots = sorted(slot.slot for slot in bottom_snap.slots)
            self.assertEqual(slots, list(range(42, 84)))
            self.assertEqual(bottom_snap.layout_slot_count, 42)
            labels = sorted(slot.slot_label for slot in bottom_snap.slots)
            self.assertEqual((labels[0], labels[-1]), ("43", "84"))
            self.assertEqual(bottom_snap.selected_enclosure_id, bottom.id)
            self.assertEqual(bottom_snap.selected_enclosure_label, bottom.label)
            self.assertIsNotNone(bottom_snap.selected_profile)
            assert bottom_snap.selected_profile is not None
            self.assertEqual(bottom_snap.selected_profile.id, "dell-md1280-drawer-bottom-42")

            top_snap = await service.get_snapshot(selected_enclosure_id=top.id)
            self.assertEqual(
                sorted(slot.slot for slot in top_snap.slots), list(range(0, 42))
            )
            top_labels = sorted(int(slot.slot_label) for slot in top_snap.slots)
            self.assertEqual((top_labels[0], top_labels[-1]), (1, 42))
            self.assertEqual(top_snap.selected_enclosure_id, top.id)
            self.assertEqual(top_snap.selected_enclosure_label, top.label)
            self.assertIsNotNone(top_snap.selected_profile)
            assert top_snap.selected_profile is not None
            self.assertEqual(top_snap.selected_profile.id, "dell-md1280-drawer-top-42")

            full_snap = await service.get_snapshot(selected_enclosure_id=full.id)
            self.assertEqual(full_snap.selected_enclosure_id, full.id)
            self.assertEqual(full_snap.selected_enclosure_label, full.label)
            self.assertIsNotNone(full_snap.selected_profile)
            assert full_snap.selected_profile is not None
            self.assertEqual(full_snap.selected_profile.id, "dell-md1280-drawer-84")
            self.assertEqual(full_snap.layout_slot_count, 84)
            self.assertEqual(len(full_snap.slots), 84)


if __name__ == "__main__":
    unittest.main()
