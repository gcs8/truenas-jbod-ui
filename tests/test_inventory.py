from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, LedAction, MultipathView, SlotView
from app.services.inventory import InventoryService, build_lunid_aliases, resolve_persistent_id
from app.services.mapping_store import MappingStore
from app.services.parsers import ParsedSSHData, ZpoolMember
from app.services.profile_registry import ProfileRegistry
from app.services.ssh_probe import SSHCommandResult
from app.services.truenas_ws import TrueNASAPIError


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
    )


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

    def test_resolve_persistent_id_preserves_eui_leaf(self) -> None:
        value, label = resolve_persistent_id("eui.000000000000001000a075012b91c7cf")

        self.assertEqual(value, "eui.000000000000001000a075012b91c7cf")
        self.assertEqual(label, "EUI64")

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


if __name__ == "__main__":
    unittest.main()
