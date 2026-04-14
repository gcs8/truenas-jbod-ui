from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, LedAction, SlotView
from app.services.inventory import InventoryService, build_lunid_aliases, resolve_persistent_id
from app.services.mapping_store import MappingStore
from app.services.ssh_probe import SSHCommandResult


class InventoryHelpersTests(unittest.TestCase):
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

    def test_resolve_persistent_id_prefers_partuuid_leaf(self) -> None:
        value, label = resolve_persistent_id("/dev/disk/by-partuuid/83672e59-1b7c-40a0-970a-15ad0776ddda")

        self.assertEqual(value, "83672e59-1b7c-40a0-970a-15ad0776ddda")
        self.assertEqual(label, "PARTUUID")

    def test_resolve_persistent_id_preserves_wwn_leaf(self) -> None:
        value, label = resolve_persistent_id("/dev/disk/by-id/wwn-0x5000c5003e8253a7")

        self.assertEqual(value, "wwn-0x5000c5003e8253a7")
        self.assertEqual(label, "WWN")


class InventoryServiceSmartSummaryTests(unittest.IsolatedAsyncioTestCase):
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
            service = InventoryService(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                MappingStore(f"{temp_dir}\\slot_mappings.json"),
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
            service = InventoryService(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                MappingStore(f"{temp_dir}\\slot_mappings.json"),
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


class InventoryServiceLedTests(unittest.IsolatedAsyncioTestCase):
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
            service = InventoryService(
                settings,
                system,
                DummyTrueNASClient(),
                DummySSHProbe(),
                MappingStore(f"{temp_dir}\\slot_mappings.json"),
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


if __name__ == "__main__":
    unittest.main()
