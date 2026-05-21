from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.services.inventory import InventoryService
from app.services.mapping_store import MappingStore
from app.services.parsers import parse_ssh_outputs, parse_storcli_physical_drives
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


if __name__ == "__main__":
    unittest.main()
