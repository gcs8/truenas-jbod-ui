import inspect
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import parsers
from app.services.parsers import (
    _apply_enclosure_sysfs_device_names,
    _merge_ses_enclosures,
    SESMapEnclosure,
    SESMapSlot,
    build_slot_candidates_from_ses_enclosures,
    canonicalize_ssh_command,
    merge_slot_candidate_maps,
    normalize_device_name,
    parse_camcontrol_devlist,
    parse_enclosure_sysfs_map,
    parse_esxcli_smart_get,
    parse_gmultipath_list,
    parse_lsscsi_devices,
    parse_lsblk_json,
    parse_mdadm_detail_scan,
    parse_nvme_id_ctrl_summary,
    parse_nvme_id_ns_summary,
    parse_nvme_list_subsys_json,
    parse_nvme_smart_log_summary,
    parse_pool_query_topology,
    parse_ssh_outputs,
    parse_sesutil_show_enclosures,
    parse_sesutil_map,
    parse_ubntstorage_json,
    parse_sg_ses_aes,
    parse_sg_ses_enclosure_status,
    parse_sg_ses_join_filter,
    parse_smart_test_results,
    parse_smartctl_text_enrichment,
    parse_smartctl_summary,
    parse_storcli_physical_drives,
    parse_unifi_gpio_debug,
    parse_zpool_status,
)


class ParserTests(unittest.TestCase):
    def test_normalize_device_name_does_not_read_a_freebsd_disk_out_of_a_linux_partition(self) -> None:
        # `sda1` used to normalize to `da1` because the FreeBSD `da<N>` token
        # matched inside the Linux partition name (issue #173).
        self.assertEqual(normalize_device_name("sda1"), "sda1")
        self.assertEqual(normalize_device_name("/dev/sda1"), "sda1")
        self.assertEqual(normalize_device_name("sdab"), "sdab")
        # FreeBSD, NVMe, multipath and embedded-token forms are unchanged.
        self.assertEqual(normalize_device_name("da1"), "da1")
        self.assertEqual(normalize_device_name("/dev/da0p2"), "da0")
        self.assertEqual(normalize_device_name("ada3p1"), "ada3")
        self.assertEqual(normalize_device_name("nvme0n1p1"), "nvme0n1")
        self.assertEqual(normalize_device_name("/dev/multipath/disk0"), "multipath/disk0")
        self.assertEqual(normalize_device_name("(pass0,da0)"), "da0")
        self.assertEqual(normalize_device_name("gptid/abc"), "gptid/abc")

    def test_parse_camcontrol_devlist_tracks_models_and_controllers(self) -> None:
        output = """
scbus12 on mpr0 bus 0:
<WDC WUH721818AL5204 C232>         at scbus12 target 153 lun 0 (da24,pass31)
<HGST HUH728080AL5200 A907>        at scbus12 target 159 lun 0 (da30,pass37)
scbus13 on mpr1 bus 0:
<WDC WUH721818AL5204 C232>         at scbus13 target 153 lun 0 (da71,pass82)
<HGST HUH728080AL5200 A907>        at scbus13 target 159 lun 0 (da77,pass88)
""".strip()

        parsed = parse_camcontrol_devlist(output)

        self.assertEqual(parsed.models["da24"], "WDC WUH721818AL5204 C232")
        self.assertEqual(parsed.models["da77"], "HGST HUH728080AL5200 A907")
        self.assertEqual(parsed.controllers["da24"], "mpr0")
        self.assertEqual(parsed.controllers["da71"], "mpr1")
        self.assertEqual(parsed.peer_devices["da24"], ["da71"])
        self.assertEqual(parsed.peer_devices["da77"], ["da30"])

    def test_parse_pool_query_topology_groups_spares_by_pool(self) -> None:
        parsed = parse_pool_query_topology(
            [
                {
                    "name": "tank",
                    "topology": {
                        "data": [
                            {
                                "type": "RAIDZ2",
                                "children": [
                                    {"type": "DISK", "path": "/dev/gptid/data-a", "status": "ONLINE"},
                                ],
                            },
                        ],
                        "spare": [
                            {"type": "DISK", "path": "/dev/gptid/spare-a", "status": "ONLINE"},
                            {"type": "DISK", "path": "/dev/gptid/spare-b", "status": "ONLINE"},
                        ],
                    },
                }
            ]
        )

        for key in ("gptid/spare-a", "gptid/spare-b"):
            with self.subTest(key=key):
                self.assertEqual(parsed[key].pool_name, "tank")
                self.assertEqual(parsed[key].vdev_class, "spare")
                self.assertEqual(parsed[key].vdev_name, "spares")
                self.assertEqual(parsed[key].topology_label, "tank > spares > spare")
        self.assertEqual(parsed["gptid/data-a"].vdev_name, "raidz2-0")

    def test_parse_zpool_status_groups_spares_by_pool(self) -> None:
        output = """
  pool: tank
 state: ONLINE
config:

        NAME                 STATE     READ WRITE CKSUM
        tank                 ONLINE       0     0     0
          raidz2-0           ONLINE       0     0     0
            gptid/data-a     ONLINE       0     0     0
        spares
          gptid/spare-a      AVAIL
          gptid/spare-b      AVAIL

errors: No known data errors
""".strip()

        parsed = parse_zpool_status(output)

        for key in ("gptid/spare-a", "gptid/spare-b"):
            with self.subTest(key=key):
                self.assertEqual(parsed[key].pool_name, "tank")
                self.assertEqual(parsed[key].vdev_class, "spare")
                self.assertEqual(parsed[key].vdev_name, "spares")
                self.assertEqual(parsed[key].topology_label, "tank > spares > spare")
        self.assertEqual(parsed["gptid/data-a"].vdev_name, "raidz2-0")

    def test_canonicalize_sg_ses_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses -p aes /dev/sg27"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses aes /dev/sg27")

    def test_canonicalize_sg_ses_ec_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses -p ec /dev/sg38"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses ec /dev/sg38")

    def test_canonicalize_sg_ses_join_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses --join --filter /dev/sg26"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses join /dev/sg26")

    def test_canonicalize_linux_inventory_commands(self) -> None:
        self.assertEqual(canonicalize_ssh_command("/usr/bin/lsblk -OJ"), "lsblk -OJ")
        self.assertEqual(
            canonicalize_ssh_command(
                "/usr/bin/lsblk --json --bytes --output NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,HCTL"
            ),
            "lsblk -OJ",
        )
        self.assertEqual(canonicalize_ssh_command("/usr/bin/lsscsi -g"), "lsscsi -g")
        self.assertEqual(canonicalize_ssh_command("/usr/bin/lsscsi -g -t"), "lsscsi -g -t")
        self.assertEqual(canonicalize_ssh_command("sudo -n /usr/sbin/mdadm --detail --scan"), "mdadm --detail --scan")
        self.assertEqual(canonicalize_ssh_command("/usr/bin/nvme list-subsys -o json"), "nvme list-subsys -o json")
        self.assertEqual(
            canonicalize_ssh_command(
                "/usr/sbin/nvme list-subsys -o json 2>/dev/null || /usr/bin/nvme list-subsys -o json 2>/dev/null || true"
            ),
            "nvme list-subsys -o json",
        )
        self.assertEqual(canonicalize_ssh_command("/usr/sbin/ubntstorage disk inspect"), "ubntstorage disk inspect")
        self.assertEqual(canonicalize_ssh_command("/usr/sbin/ubntstorage space inspect"), "ubntstorage space inspect")
        self.assertEqual(canonicalize_ssh_command("cat /sys/kernel/debug/gpio"), "gpio debug")

    def test_canonicalize_core_pci_slot_commands(self) -> None:
        self.assertEqual(canonicalize_ssh_command("/usr/sbin/pciconf -lv"), "pciconf -lv")
        self.assertEqual(canonicalize_ssh_command("sudo -n /usr/local/sbin/dmidecode -t slot"), "dmidecode slot")
        self.assertEqual(canonicalize_ssh_command("sudo -n /usr/local/sbin/dmidecode -t 9 || true"), "dmidecode slot")
        self.assertEqual(
            canonicalize_ssh_command("sysctl dev.mpr.0.%location dev.mpr.1.%parent"),
            "mpr sysctl pci locations",
        )
        self.assertEqual(
            canonicalize_ssh_command("sysctl -a | egrep '^dev\\.mpr\\.[0-9]+\\.%(location|parent):' || true"),
            "mpr sysctl pci locations",
        )

    def test_parse_lsscsi_g_t_extracts_transport_and_sg_devices(self) -> None:
        output = """
[1:0:1:0]    disk    sas:0x5000cca264d473d5          /dev/sdc   /dev/sg2
[1:0:26:0]   enclosu sas:0x5003048001c1043f          -          /dev/sg26
""".strip()

        parsed = parse_lsscsi_devices(output, transport=True)

        self.assertEqual(parsed[0].hctl, "1:0:1:0")
        self.assertEqual(parsed[0].device_type, "disk")
        self.assertEqual(parsed[0].block_device, "/dev/sdc")
        self.assertEqual(parsed[0].sg_device, "/dev/sg2")
        self.assertEqual(parsed[0].transport, "sas")
        self.assertEqual(parsed[0].transport_address, "0x5000cca264d473d5")
        self.assertEqual(parsed[1].device_type, "enclosu")
        self.assertIsNone(parsed[1].block_device)
        self.assertEqual(parsed[1].sg_device, "/dev/sg26")

    def test_parse_lsscsi_g_preserves_colon_bearing_vendor(self) -> None:
        parsed = parse_lsscsi_devices(
            "[0:0:0:0] disk ACME:Storage Array Model R1 /dev/sda /dev/sg0"
        )

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].vendor, "ACME:Storage")
        self.assertEqual(parsed[0].model, "Array Model")
        self.assertEqual(parsed[0].revision, "R1")
        self.assertIsNone(parsed[0].transport)
        self.assertIsNone(parsed[0].transport_address)
        self.assertEqual(parsed[0].block_device, "/dev/sda")
        self.assertEqual(parsed[0].sg_device, "/dev/sg0")

    def test_parse_ssh_outputs_merges_colon_vendor_with_transport_evidence(self) -> None:
        parsed = parse_ssh_outputs(
            {
                "lsscsi -g": "[0:0:0:0] disk ACME:Storage Array Model R1 /dev/sda /dev/sg0",
                "lsscsi -g -t": "[0:0:0:0] disk sas:0x5000c50012345678 /dev/sda /dev/sg0",
            },
            slot_count=1,
            enclosure_filter=None,
        )

        self.assertEqual(len(parsed.linux_scsi_devices), 1)
        device = parsed.linux_scsi_devices[0]
        self.assertEqual(device.vendor, "ACME:Storage")
        self.assertEqual(device.model, "Array Model")
        self.assertEqual(device.revision, "R1")
        self.assertEqual(device.transport, "sas")
        self.assertEqual(device.transport_address, "0x5000c50012345678")
        self.assertEqual(device.block_device, "/dev/sda")
        self.assertEqual(device.sg_device, "/dev/sg0")

    def test_parse_sg_ses_join_filter_extracts_joined_slot_detail(self) -> None:
        output = """
LSI       SAS3x40           0601
Primary enclosure logical identifier (hex): 5003048001c1043f
Slot00 [0,0]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: OK
  Additional Element Status:
    Transport protocol: SAS
    number of phys: 1, not all phys: 0, device slot number: 0
    phy index: 0
      SAS device type: end device
      target port for: SSP
      attached SAS address: 0x5003048001c1043f
      SAS address: 0x5000cca264d473d5
      phy identifier: 0x0
""".strip()

        enclosure = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg26")

        self.assertIsNotNone(enclosure)
        self.assertEqual(enclosure.ses_device, "/dev/sg26")
        self.assertEqual(enclosure.enclosure_id, "5003048001c1043f")
        slot = enclosure.slots[0]
        self.assertEqual(slot.status, "OK")
        self.assertEqual(slot.transport_protocol, "SAS")
        self.assertEqual(slot.sas_device_type, "end device")
        self.assertEqual(slot.target_port_protocol, "SSP")
        self.assertEqual(slot.attached_sas_address, "5003048001c1043f")
        self.assertEqual(slot.sas_address, "5000cca264d473d5")
        self.assertEqual(slot.phy_identifier, "0x0")

    def test_parse_sg_ses_join_filter_accepts_descriptor_variants(self) -> None:
        headers = (
            "[0,7]  Element type: Array device slot",
            "Drive Bay Seven [0,7]  Element type: Array device slot",
            "ArbitraryDescriptor [0,7]  Element type: device slot",
        )

        for header in headers:
            with self.subTest(header=header):
                output = "\n".join(
                    (
                        "ExampleCo  GenericShelf  0001",
                        header,
                        "  Additional Element Status:",
                        "    number of phys: 1, not all phys: 0, device slot number: 7",
                        "      SAS device type: end device",
                    )
                )

                parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg7")

                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(list(parsed.slots), [7])
                self.assertEqual(parsed.enclosure_name, "ExampleCo GenericShelf 0001")

    def test_parse_sg_ses_join_filter_rekeys_to_reported_bay(self) -> None:
        output = """
ExampleCo  GenericShelf  0001
Slot17 [0,17]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: OK
  Additional Element Status:
    number of phys: 1, not all phys: 0, device slot number: 7
      SAS device type: end device
""".strip()

        parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg7")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(list(parsed.slots), [7])
        slot = parsed.slots[7]
        self.assertEqual(slot.slot_number, 7)
        self.assertEqual(slot.element_id, 17)
        self.assertEqual(
            slot.control_targets,
            [{"ses_device": "/dev/sg7", "ses_element_id": 17, "ses_slot_number": 7}],
        )

    def test_parse_sg_ses_join_filter_resets_on_every_non_slot_header(self) -> None:
        output = """
ExampleCo  GenericShelf  0001
[0,7]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: OK
    Ident=0
  Additional Element Status:
    number of phys: 1, not all phys: 0, device slot number: 7
      SAS device type: end device
Fan0 [2,0]  Element type: Cooling
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: Critical
    Ident=1
PSU0 [1,0]  Element type: Power supply
  Enclosure Status:
    Predicted failure=1, Disabled=1, Swap=0, status: Noncritical
    Ident=1
Temp0 [3,0]  Element type: Temperature sensor
  Enclosure Status:
    Predicted failure=1, Disabled=1, Swap=0, status: Critical
    Ident=1
""".strip()

        parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg7")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        slot = parsed.slots[7]
        self.assertEqual(slot.status, "OK")
        self.assertFalse(slot.identify_active)
        self.assertFalse(slot.predicted_failure)
        self.assertFalse(slot.disabled)

    def test_parse_sg_ses_join_filter_records_presence_from_joined_evidence(self) -> None:
        output = """
ExampleCo  GenericShelf  0001
[0,7]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: Noncritical
  Additional Element Status:
    number of phys: 1, not all phys: 0, device slot number: 7
      SAS device type: no SAS device attached
      SAS address: 0x0
[0,8]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: Noncritical
  Additional Element Status:
    number of phys: 1, not all phys: 0, device slot number: 8
      SAS device type: end device
      SAS address: 0x5000000000000008
""".strip()

        parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg7")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertFalse(parsed.slots[7].present)
        self.assertEqual(parsed.slots[7].presence_source, "sg_ses_join")
        self.assertTrue(parsed.slots[8].present)
        self.assertEqual(parsed.slots[8].presence_source, "sg_ses_join")

    def test_sg_ses_descriptor_refines_presence_without_conflict(self) -> None:
        fixtures = (
            (
                "aes",
                parse_sg_ses_aes,
                """
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 0
        SAS device type: no SAS device attached
        SAS address: 0x5000000000000001
""".strip(),
            ),
            (
                "join",
                parse_sg_ses_join_filter,
                """
[0,0]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: Noncritical
  Additional Element Status:
    device slot number: 0
    SAS device type: no SAS device attached
    SAS address: 0x5000000000000001
""".strip(),
            ),
        )

        for source, parser, output in fixtures:
            with self.subTest(source=source):
                parsed = parser(output, f"sg_ses {source} /dev/sg0")

                self.assertIsNotNone(parsed)
                assert parsed is not None
                slot = parsed.slots[0]
                self.assertIs(slot.present, True)
                self.assertEqual(slot.presence_source, f"sg_ses_{source}")
                self.assertFalse(slot.presence_conflict)

    def test_canonicalize_esxi_inventory_commands(self) -> None:
        self.assertEqual(canonicalize_ssh_command("esxcli storage core device list"), "esxcli storage core device list")
        self.assertEqual(canonicalize_ssh_command("esxcli storage vmfs extent list"), "esxcli storage vmfs extent list")
        self.assertEqual(
            canonicalize_ssh_command("/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J"),
            "storcli /c0/eall/sall show all J",
        )

    def test_parse_esxi_storcli_json_maps_physical_members(self) -> None:
        virtual_drives = """
{
  "Controllers": [
    {
      "Command Status": {"Status": "Success"},
      "Response Data": {
        "/c0/v0": [
          {"DG/VD": "0/0", "TYPE": "RAID1", "State": "Optl", "Size": "100.000 GB", "Name": "ESXi"}
        ],
        "PDs for VD 0": [
          {"EID:Slt": "13:0", "State": "Onln"},
          {"EID:Slt": "13:1", "State": "Onln"}
        ],
        "VD0 Properties": {
          "SCSI NAA Id": "naa.60030480208ba599ffa8f1"
        }
      }
    }
  ]
}
""".strip()
        physical_drives = """
{
  "Controllers": [
    {
      "Command Status": {"Status": "Success"},
      "Response Data": {
        "Drive /c0/e13/s0": [
          {
            "EID:Slt": "13:0", "DID": 11, "State": "Onln", "DG": "0", "Size": "1.818 TB", "Intf": "NVMe", "Med": "SSD", "SeSz": "512B", "Model": "Samsung SSD 970 EVO 2TB"
          }
        ],
        "Drive /c0/e13/s0 - Detailed Information": {
          "Drive /c0/e13/s0 State": {
            "Media Error Count": "60",
            "Other Error Count": "2",
            "Predictive Failure Count": "0",
            "S.M.A.R.T alert flagged by drive": "No",
            "Drive Temperature": "34C"
          },
          "Drive /c0/e13/s0 Device attributes": {
            "SN": "SERIAL0",
            "Firmware Revision": "2B2QEXE7"
          },
          "Drive /c0/e13/s0 Port Information": {
            "Connector Name": "C0 x4",
            "Connected Port Number(path)": "0(path0)",
            "Link Speed": "8.0GT/s"
          }
        },
        "Drive /c0/e13/s1": [
          {
            "EID:Slt": "13:1", "DID": 12, "State": "Onln", "DG": "0", "Size": "1.818 TB", "Intf": "NVMe", "Med": "SSD", "SeSz": "512B", "Model": "Samsung SSD 970 EVO 2TB"
          }
        ],
        "Drive /c0/e13/s1 - Detailed Information": {
          "Drive /c0/e13/s1 State": {
            "Media Error Count": "262",
            "Predictive Failure Count": "0",
            "S.M.A.R.T alert flagged by drive": "No",
            "Drive Temperature": "35C"
          },
          "Drive /c0/e13/s1 Device attributes": {
            "SN": "SERIAL1",
            "Firmware Revision": "2B2QEXE7"
          },
          "Drive /c0/e13/s1 Port Information": {
            "Connector Name": "C1 x4",
            "Connected Port Number(path)": "1(path0)",
            "Link Speed": "8.0GT/s"
          }
        }
      }
    }
  ]
}
""".strip()

        parsed = parse_ssh_outputs(
            {
                "/opt/lsi/storcli64/storcli64 /c0/vall show all J": virtual_drives,
                "/opt/lsi/storcli64/storcli64 /c0/eall/sall show all J": physical_drives,
            },
            slot_count=2,
            enclosure_filter=None,
        )

        self.assertEqual(parsed.esxi_storcli_virtual_drives[0]["name"], "ESXi")
        self.assertEqual(parsed.esxi_storcli_virtual_drives[0]["physical_drives"][0]["slot_key"], "13:0")
        self.assertEqual(parsed.esxi_storcli_physical_drives[0]["slot_key"], "13:0")
        self.assertEqual(parsed.esxi_storcli_physical_drives[0]["connector_name"], "C0 x4")
        self.assertEqual(parsed.esxi_storcli_physical_drives[0]["temperature_c"], 34)
        self.assertEqual(parsed.esxi_storcli_physical_drives[1]["media_errors"], 262)

    def test_parse_storcli_physical_drives_reads_all_controller_blocks(self) -> None:
        output = json.dumps(
            {
                "Controllers": [
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "Drive Information": [
                                {"Ctl": "c0", "EID:Slt": "252:0", "State": "JBOD"},
                            ]
                        },
                    },
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "Drive Information": [
                                {"Ctl": "c1", "EID:Slt": "252:1", "State": "Onln"},
                            ]
                        },
                    },
                ]
            }
        )

        parsed = parse_storcli_physical_drives(output)

        self.assertEqual([drive["controller_id"] for drive in parsed], ["c0", "c1"])
        self.assertEqual([drive["slot_key"] for drive in parsed], ["252:0", "252:1"])

    def test_parse_storcli_uses_controller_id_from_single_controller_command(self) -> None:
        output = json.dumps(
            {
                "Controllers": [
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "Drive Information": [
                                {"EID:Slt": "252:0", "State": "JBOD"},
                            ]
                        },
                    }
                ]
            }
        )

        parsed = parse_ssh_outputs(
            {"storcli /c7/eall/sall show all J": output},
            slot_count=1,
            enclosure_filter=None,
        )

        self.assertEqual(parsed.esxi_storcli_physical_drives[0]["controller_id"], "c7")

    def test_parse_storcli_virtual_drives_reads_all_controller_blocks(self) -> None:
        output = json.dumps(
            {
                "Controllers": [
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "VD LIST": [
                                {"DG/VD": "0/0", "Name": "boot", "TYPE": "RAID1", "State": "Optl"},
                            ],
                            "PDs for VD 0": [{"EID:Slt": "252:0"}],
                        },
                    },
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "VD LIST": [
                                {"DG/VD": "1/1", "Name": "data", "TYPE": "RAID5", "State": "Optl"},
                            ],
                            "PDs for VD 1": [{"EID:Slt": "252:0"}],
                        },
                    },
                ]
            }
        )

        parsed = parse_ssh_outputs(
            {"storcli /call/vall show all J": output},
            slot_count=2,
            enclosure_filter=None,
        )

        self.assertEqual(
            [
                (
                    drive["controller_id"],
                    drive["name"],
                    drive["raid"],
                    drive["physical_drives"][0]["controller_id"],
                )
                for drive in parsed.esxi_storcli_virtual_drives
            ],
            [("c0", "boot", "RAID1", "c0"), ("c1", "data", "RAID5", "c1")],
        )

    def test_parse_storcli_warns_when_a_controller_block_is_invalid(self) -> None:
        output = json.dumps(
            {
                "Controllers": [
                    {
                        "Command Status": {"Status": "Success"},
                        "Response Data": {
                            "Drive Information": [
                                {"Ctl": "c0", "EID:Slt": "252:0", "State": "JBOD"},
                            ]
                        },
                    },
                    {
                        "Command Status": {"Status": "Failure", "Description": "synthetic failure"},
                        "Response Data": "not an object",
                    },
                ]
            }
        )

        parsed = parse_ssh_outputs(
            {"storcli /call/eall/sall show all J": output},
            slot_count=1,
            enclosure_filter=None,
        )

        self.assertEqual([drive["controller_id"] for drive in parsed.esxi_storcli_physical_drives], ["c0"])
        self.assertEqual(
            parsed.warnings,
            ["StorCLI controller block 1 was invalid and was not used: synthetic failure."],
        )

    def test_parse_unifi_gpio_debug_uses_last_output_line_per_slot(self) -> None:
        output = """
gpiochip1: GPIOs 480-495, parent: i2c/0-0021, pca9575, can sleep:
 gpio-480 (                    |hdd@0               ) out hi
 gpio-481 (                    |hdd@1               ) out hi
 gpio-492 (                    |hdd@0               ) out lo
 gpio-493 (                    |hdd@1               ) out hi
""".strip()

        parsed = parse_unifi_gpio_debug(output)

        self.assertEqual(parsed, {0: False, 1: True})

    def test_parse_ubntstorage_json_accepts_plain_list_payloads(self) -> None:
        parsed = parse_ubntstorage_json('[{"node":"sda","slot":1},{"node":"sdb","slot":2}]')

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["node"], "sda")
        self.assertEqual(parsed[1]["slot"], 2)

    def test_parse_ssh_outputs_preserves_ubntstorage_rows(self) -> None:
        parsed = parse_ssh_outputs(
            {
                "/usr/sbin/ubntstorage disk inspect": '[{"node":"sda","slot":1,"healthy":"optimal"}]',
                "/usr/sbin/ubntstorage space inspect": '[{"node":"md3","state":"ready"}]',
                "cat /sys/kernel/debug/gpio": """
gpiochip1: GPIOs 480-495, parent: i2c/0-0021, pca9575, can sleep:
 gpio-480 (                    |hdd@0               ) out hi
 gpio-492 (                    |hdd@0               ) out hi
 gpio-493 (                    |hdd@1               ) out lo
""".strip(),
            },
            7,
            None,
        )

        self.assertEqual(parsed.ubntstorage_disks[0]["node"], "sda")
        self.assertEqual(parsed.ubntstorage_spaces[0]["node"], "md3")
        self.assertEqual(parsed.unifi_led_states, {0: True, 1: False})

    def test_parse_gmultipath_list_preserves_consumers(self) -> None:
        output = """
Geom name: disk12
Providers:
1. Name: multipath/disk12
   Mediasize: 8001563222016 (7.3T)
   Sectorsize: 512
   State: OPTIMAL
Consumers:
1. Name: da65
   Mediasize: 8001563222016 (7.3T)
   State: ACTIVE
   Mode: r2w2e4
2. Name: da18
   Mediasize: 8001563222016 (7.3T)
   State: PASSIVE
   Mode: r2w2e4
Mode: Active/Passive
UUID: d83955b0-0a0c-11e7-bd32-0cc47a8ff400
State: OPTIMAL
""".strip()

        parsed = parse_gmultipath_list(output)
        multipath = parsed["multipath/disk12"]

        self.assertEqual(multipath.mode, "Active/Passive")
        self.assertEqual(multipath.state, "OPTIMAL")
        self.assertEqual(multipath.provider_state, "OPTIMAL")
        self.assertEqual(multipath.device_name, "multipath/disk12")
        self.assertEqual(len(multipath.consumers), 2)
        self.assertEqual(multipath.consumers[0].device_name, "da65")
        self.assertEqual(multipath.consumers[0].state, "ACTIVE")
        self.assertEqual(multipath.consumers[1].device_name, "da18")
        self.assertEqual(multipath.consumers[1].state, "PASSIVE")

    def test_parse_gmultipath_list_handles_degraded_failed_member(self) -> None:
        output = """
Geom name: disk19
Providers:
1. Name: multipath/disk19
   Mediasize: 18000207937536 (16T)
   Sectorsize: 512
   State: DEGRADED
Consumers:
1. Name: da85
   Mediasize: 18000207937536 (16T)
   State: ACTIVE
   Mode: r2w2e4
2. Name: da38
   Mediasize: 18000207937536 (16T)
   State: FAIL
   Mode: r2w2e4
Mode: Active/Active
UUID: 31260ced-2335-11e8-a29d-0cc47a8ff400
State: DEGRADED
""".strip()

        parsed = parse_gmultipath_list(output)
        multipath = parsed["multipath/disk19"]

        self.assertEqual(multipath.mode, "Active/Active")
        self.assertEqual(multipath.state, "DEGRADED")
        self.assertEqual(multipath.provider_state, "DEGRADED")
        self.assertEqual(len(multipath.consumers), 2)
        self.assertEqual(multipath.consumers[0].device_name, "da85")
        self.assertEqual(multipath.consumers[0].state, "ACTIVE")
        self.assertEqual(multipath.consumers[1].device_name, "da38")
        self.assertEqual(multipath.consumers[1].state, "FAIL")

    def test_parse_sg_ses_aes_extracts_scale_front_slots(self) -> None:
        output = """
  LSI       SAS3x40           0601
  Primary enclosure logical identifier (hex): 5003048001c1043f
Additional element status diagnostic page:
  generation code: 0x0
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5003048001c1043f
          SAS address: 0x5000cca264d473d5
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 1
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5003048001c1043f
          SAS address: 0x5000cca264ccb7ed
    Element type: SAS expander, subenclosure id: 0 [ti=1]
      Element index: 24  eiioe=0
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg27")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.ses_device, "/dev/sg27")
        self.assertEqual(parsed.enclosure_id, "5003048001c1043f")
        self.assertEqual(parsed.enclosure_label, "Front 24 Bay")
        self.assertEqual(parsed.layout_rows, 6)
        self.assertEqual(parsed.layout_columns, 4)
        self.assertEqual(parsed.slot_layout, [[5, 11, 17, 23], [4, 10, 16, 22], [3, 9, 15, 21], [2, 8, 14, 20], [1, 7, 13, 19], [0, 6, 12, 18]])
        self.assertEqual(parsed.slots[0].sas_address, "5000cca264d473d5")
        self.assertEqual(parsed.slots[1].sas_address, "5000cca264ccb7ed")
        self.assertTrue(parsed.slots[0].present)

    def test_ec_merges_with_one_based_aes_by_element_identity(self) -> None:
        aes_output = """
  ExampleCo  OneBasedShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000101
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 1
        SAS device type: end device
        SAS address: 0x5000000000000001
      Element index: 1  eiioe=0
        device slot number: 2
        SAS device type: no SAS device attached
        SAS address: 0x0
""".strip()
        ec_output = """
  ExampleCo  OneBasedShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000101
Enclosure status diagnostic page:
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Overall descriptor:
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
        Ident=1
      Element 1 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Not installed
        Ident=0
""".strip()

        parsed = parse_ssh_outputs(
            {
                "sg_ses aes /dev/sg7": aes_output,
                "sg_ses ec /dev/sg7": ec_output,
            },
            slot_count=2,
            enclosure_filter=None,
        )

        self.assertEqual(len(parsed.ses_enclosures), 1)
        enclosure = parsed.ses_enclosures[0]
        self.assertEqual(sorted(enclosure.slots), [1, 2])
        self.assertNotIn(0, enclosure.slots)
        self.assertEqual(enclosure.slots[1].element_id, 0)
        self.assertEqual(enclosure.slots[1].status, "OK")
        self.assertTrue(enclosure.slots[1].present)
        self.assertTrue(enclosure.slots[1].identify_active)
        self.assertEqual(
            enclosure.slots[1].control_targets,
            [{"ses_device": "/dev/sg7", "ses_element_id": 0, "ses_slot_number": 1}],
        )
        self.assertEqual(enclosure.slots[2].element_id, 1)
        self.assertEqual(enclosure.slots[2].status, "Not installed")
        self.assertFalse(enclosure.slots[2].present)

    def test_combined_enclosure_order_ignores_population_and_status(self) -> None:
        def enclosure(
            *,
            name: str,
            label: str,
            ses_device: str,
            enclosure_id: str,
            count: int,
            populated: bool,
        ) -> SESMapEnclosure:
            return SESMapEnclosure(
                enclosure_id=enclosure_id,
                enclosure_name=name,
                enclosure_label=label,
                ses_device=ses_device,
                slots={
                    slot_number: SESMapSlot(
                        slot_number=slot_number,
                        present=populated,
                        status="Noncritical" if populated else "Not installed",
                    )
                    for slot_number in range(count)
                },
            )

        variants = (
            [
                enclosure(
                    name="LSI SAS3x40 0601",
                    label="Front 24 Bay",
                    ses_device="/dev/sg27",
                    enclosure_id="front-id",
                    count=24,
                    populated=False,
                ),
                enclosure(
                    name="LSI SAS3x28 0601",
                    label="Rear 12 Bay",
                    ses_device="/dev/sg38",
                    enclosure_id="rear-id",
                    count=12,
                    populated=True,
                ),
            ],
            [
                enclosure(
                    name="LSI SAS3x28 0601",
                    label="Rear 12 Bay",
                    ses_device="/dev/sg38",
                    enclosure_id="rear-id",
                    count=12,
                    populated=False,
                ),
                enclosure(
                    name="LSI SAS3x40 0601",
                    label="Front 24 Bay",
                    ses_device="/dev/sg27",
                    enclosure_id="front-id",
                    count=24,
                    populated=True,
                ),
            ],
        )

        for enclosures in variants:
            with self.subTest(populated=enclosures[0].enclosure_label):
                candidates, selected = build_slot_candidates_from_ses_enclosures(
                    enclosures,
                    36,
                    None,
                )
                self.assertEqual(selected["label"], "Front 24 Bay + Rear 12 Bay")
                self.assertEqual(candidates[0]["enclosure_label"], "Front 24 Bay")
                self.assertEqual(candidates[24]["enclosure_label"], "Rear 12 Bay")

    def test_parse_sg_ses_aes_applies_eiioe_element_coordinates(self) -> None:
        # sg_ses(8) INDEXES: EIIOE=1 includes the first type header's one
        # overall status element; EC and --index use the type-local coordinate.
        fixtures = (
            (0, 0, 0),
            (0, 7, 7),
            (1, 1, 0),
            (1, 8, 7),
        )
        for eiioe, raw_element, expected_element in fixtures:
            with self.subTest(eiioe=eiioe, raw_element=raw_element):
                output = f"""
  ExampleCo  CoordinateShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000303
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: {raw_element}  eiioe={eiioe}
        device slot number: 7
        SAS device type: end device
""".strip()

                parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg3")

                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.slots[7].element_id, expected_element)
                self.assertEqual(
                    parsed.slots[7].control_targets,
                    [
                        {
                            "ses_device": "/dev/sg3",
                            "ses_element_id": expected_element,
                            "ses_slot_number": 7,
                        }
                    ],
                )

    def test_all_typed_ses_parsers_accept_both_device_slot_names(self) -> None:
        for element_type in ("Array device slot", "Device slot"):
            with self.subTest(parser="sesutil_map", element_type=element_type):
                parsed_map = parse_sesutil_map(
                    f"""
ses0:
  Enclosure Name: ExampleCo AliasShelf
  Enclosure ID: 5000000000000404
  Element 7, Type: {element_type}
    Status: OK
    Description: Slot07
""".strip()
                )
                self.assertEqual(len(parsed_map), 1)
                self.assertEqual(list(parsed_map[0].slots), [7])

            with self.subTest(parser="sg_ses_aes", element_type=element_type):
                parsed_aes = parse_sg_ses_aes(
                    f"""
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: {element_type}, subenclosure id: 0 [ti=0]
      Element index: 7  eiioe=0
        device slot number: 7
""".strip(),
                    "sg_ses aes /dev/sg4",
                )
                self.assertIsNotNone(parsed_aes)
                assert parsed_aes is not None
                self.assertEqual(list(parsed_aes.slots), [7])

            with self.subTest(parser="sg_ses_ec", element_type=element_type):
                parsed_ec = parse_sg_ses_enclosure_status(
                    f"""
Enclosure status diagnostic page:
  status descriptor list
    Element type: {element_type}, subenclosure id: 0 [ti=0]
      Overall descriptor:
      Element 7 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
""".strip(),
                    "sg_ses ec /dev/sg4",
                )
                self.assertIsNotNone(parsed_ec)
                assert parsed_ec is not None
                self.assertEqual(list(parsed_ec.slots), [7])

            with self.subTest(parser="sg_ses_join", element_type=element_type):
                parsed_join = parse_sg_ses_join_filter(
                    f"""
[0,7]  Element type: {element_type}
  Additional Element Status:
    device slot number: 7
""".strip(),
                    "sg_ses join /dev/sg4",
                )
                self.assertIsNotNone(parsed_join)
                assert parsed_join is not None
                self.assertEqual(list(parsed_join.slots), [7])

    def test_repeated_device_slot_numbers_preserve_every_element_as_degraded(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "platform_parity" / "scale_md1280_sg1_aes.txt"
        fixture = fixture_path.read_text(encoding="utf-8")
        all_zero_fixture = re.sub(
            r"(device slot number:\s*)\d+",
            r"\g<1>0",
            fixture,
            flags=re.IGNORECASE,
        )

        parsed = parse_sg_ses_aes(all_zero_fixture, "sg_ses aes /dev/sg1")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(len(parsed.slots) + len(parsed.unmapped_slots), 84)
        repeated_slots = [slot for slot in parsed.unmapped_slots if slot.slot_number_degraded]
        invalid_slots = [slot for slot in parsed.unmapped_slots if not slot.slot_number_degraded]
        self.assertEqual(len(repeated_slots), 70)
        self.assertEqual(len({slot.element_id for slot in repeated_slots}), 70)
        self.assertTrue(all(slot.reported_slot_number == 0 for slot in repeated_slots))
        self.assertTrue(
            all("multiple distinct elements" in (slot.slot_number_warning or "") for slot in repeated_slots)
        )
        # Invalid descriptors cannot use their raw element indexes as bay
        # numbers when the remaining descriptors prove no consistent offset.
        self.assertEqual(len(invalid_slots), 14)
        self.assertTrue(all(slot.reported_slot_number is None for slot in invalid_slots))
        self.assertTrue(
            all("no consistent element-to-device-slot offset" in (slot.slot_number_warning or "") for slot in invalid_slots)
        )
        _, selected = build_slot_candidates_from_ses_enclosures([parsed], 84, None)
        self.assertEqual(len(selected["unmapped_ses_elements"]), 84)
        selected_repeated = [
            item for item in selected["unmapped_ses_elements"] if item["slot_number_degraded"]
        ]
        self.assertEqual(len(selected_repeated), 70)
        self.assertTrue(
            all(item["slot_number_degraded"] for item in selected_repeated)
        )
        self.assertTrue(
            all(item["reported_slot_number"] == 0 for item in selected_repeated)
        )
        self.assertTrue(any("multiple distinct elements" in warning for warning in selected["warnings"]))

    def test_join_repeated_device_slot_numbers_preserve_every_element_as_degraded(self) -> None:
        joined = parse_sg_ses_join_filter(
            """
[0,0]  Element type: Device slot
  Additional Element Status:
    device slot number: 5
    SAS device type: end device
[0,1]  Element type: Device slot
  Additional Element Status:
    device slot number: 5
    SAS device type: end device
[0,2]  Element type: Device slot
  Additional Element Status:
    device slot number: 5
    SAS device type: end device
""".strip(),
            "sg_ses join /dev/sg5",
        )
        self.assertIsNotNone(joined)
        assert joined is not None
        self.assertEqual(joined.slots, {})
        self.assertEqual([slot.element_id for slot in joined.unmapped_slots], [0, 1, 2])
        self.assertTrue(all(slot.slot_number_degraded for slot in joined.unmapped_slots))

    def test_parse_sg_ses_aes_requires_model_evidence_for_24_bay_operator_profile(self) -> None:
        output = """
  ExampleCo  GenericShelf24  0001
  Primary enclosure logical identifier (hex): 5000000000000024
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 0
      Element index: 23  eiioe=0
        device slot number: 23
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg27")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.profile_id)
        self.assertEqual(parsed.enclosure_label, "24 Bay SES")
        self.assertIsNone(parsed.slot_layout)

    def test_parse_sg_ses_aes_requires_model_evidence_for_12_bay_operator_profile(self) -> None:
        output = """
  ExampleCo  GenericShelf12  0001
  Primary enclosure logical identifier (hex): 5000000000000012
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 0
      Element index: 11  eiioe=0
        device slot number: 11
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg38")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed.profile_id)
        self.assertEqual(parsed.enclosure_label, "12 Bay SES")
        self.assertIsNone(parsed.slot_layout)

    def test_parse_sesutil_map_preserves_unrecognized_description_with_element_fallback(self) -> None:
        output = """
ses0:
  Enclosure Name: ExampleCo GenericShelf
  Enclosure ID: 5000000000000007
  Element 7, Type: Array Device Slot
    Status: OK
    Description: ArrayDevice07
    Device Names: da7, pass7
""".strip()

        parsed = parse_sesutil_map(output)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].slots, {})
        slot = parsed[0].unmapped_slots[0]
        self.assertEqual(slot.description, "ArrayDevice07")
        self.assertEqual(slot.device_names, ["da7"])
        self.assertEqual(getattr(slot, "slot_number_source", None), "ses_element_id_fallback")
        self.assertIn("unrecognized SES slot description", getattr(slot, "slot_number_warning", None) or "")

        candidates, selected = build_slot_candidates_from_ses_enclosures(parsed, 8, None)
        self.assertEqual(candidates, {})
        self.assertEqual(selected["unmapped_ses_elements"][0]["ses_element_id"], 7)
        self.assertEqual(selected["unmapped_ses_elements"][0]["device_names"], ["da7"])
        self.assertIn("unrecognized SES slot description", selected["warnings"][0])

    def test_parse_sesutil_map_refines_same_record_presence_without_conflict(self) -> None:
        parsed = parse_sesutil_map(
            """
ses0:
  Enclosure Name: ExampleCo GenericShelf
  Enclosure ID: 5000000000000007
  Element 0, Type: Array Device Slot
    Status: Not installed
    Description: Slot00
    Device Names: da0, pass0
""".strip()
        )

        slot = parsed[0].slots[0]
        self.assertIs(slot.present, True)
        self.assertEqual(slot.presence_source, "sesutil_map")
        self.assertFalse(slot.presence_conflict)

    def test_parse_sesutil_map_duplicate_slot_uses_each_descriptors_device_names(self) -> None:
        parsed = parse_sesutil_map(
            """
ses0:
  Enclosure Name: ExampleCo GenericShelf
  Enclosure ID: 5000000000000007
  Element 0, Type: Array Device Slot
    Status: OK
    Description: Slot00
    Device Names: da0, pass0
  Element 24, Type: Array Device Slot
    Status: Not installed
    Description: Slot00
    Device Names:
""".strip()
        )

        slot = parsed[0].slots[0]
        self.assertIs(slot.present, True)
        self.assertEqual(slot.presence_source, "sesutil_map")
        self.assertTrue(slot.presence_conflict)

    def test_parse_sg_ses_aes_preserves_slot_without_device_slot_number(self) -> None:
        output = """
  ExampleCo  GenericShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000007
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 7  eiioe=0
        Transport protocol: SAS
        SAS device type: end device
        SAS address: 0x5000000000007000
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg7")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.slots, {})
        slot = parsed.unmapped_slots[0]
        self.assertEqual(slot.sas_address, "5000000000007000")
        self.assertEqual(getattr(slot, "slot_number_source", None), "ses_element_id_fallback")
        self.assertIn("did not report a device slot number", getattr(slot, "slot_number_warning", None) or "")

        candidates, selected = build_slot_candidates_from_ses_enclosures([parsed], 8, None)
        self.assertEqual(candidates, {})
        self.assertIsNone(selected["unmapped_ses_elements"][0]["ses_slot_number"])
        self.assertIsNone(
            selected["unmapped_ses_elements"][0]["ses_targets"][0]["ses_slot_number"]
        )

    def test_parse_sesutil_map_duplicate_slot_keeps_later_detail_lines(self) -> None:
        output = """
ses0:
  Enclosure Name: ExampleCo DualPathShelf
  Enclosure ID: 5000000000000005
  Element 5, Type: Array Device Slot
    Status: OK
    Description: Slot05
  Element 29, Type: Array Device Slot
    Status: OK
    Description: Slot05
    Device Names: da5, pass5
    Extra status:
      - LED=locate
""".strip()

        parsed = parse_sesutil_map(output)

        self.assertEqual(len(parsed), 1)
        slot = parsed[0].slots[5]
        # The second sighting of the same physical slot keeps feeding detail
        # lines after the merge; they must land on the stored slot instead of
        # an orphaned accumulation object.
        self.assertEqual(slot.device_names, ["da5"])
        self.assertTrue(slot.identify_active)
        self.assertTrue(slot.present)
        target_elements = {target.get("ses_element_id") for target in slot.control_targets}
        self.assertIn(5, target_elements)
        self.assertIn(29, target_elements)

    def test_parse_sg_ses_aes_duplicate_slot_keeps_later_detail_lines(self) -> None:
        output = """
  ExampleCo  DualPathShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000005
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x500000000000503f
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x500000000000603f
          SAS address: 0x5000cca264d47000
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(list(parsed.slots), [5])
        slot = parsed.slots[5]
        # The SAS address is only reported by the second element sighting and
        # is parsed after the duplicate slot number merges; it must reach the
        # stored slot instead of an orphaned accumulation object.
        self.assertEqual(slot.sas_address, "5000cca264d47000")
        self.assertTrue(slot.present)
        self.assertFalse(slot.slot_number_degraded)
        self.assertEqual(parsed.unmapped_slots, [])

    def test_parse_sesutil_map_duplicate_slot_merges_device_names_from_both_elements(self) -> None:
        output = """
ses0:
  Enclosure Name: ExampleCo DualPathShelf
  Enclosure ID: 5000000000000005
  Element 5, Type: Array Device Slot
    Status: OK
    Description: Slot05
    Device Names: da5, pass5
  Element 29, Type: Array Device Slot
    Status: OK
    Description: Slot05
    Device Names: da29, pass29
""".strip()

        slot = parse_sesutil_map(output)[0].slots[5]

        self.assertEqual(slot.device_names, ["da5", "da29"])

    def test_parse_sg_ses_aes_duplicate_slot_keeps_first_nonempty_path_details(self) -> None:
        output = """
  ExampleCo  DualPathShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000005
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x500000000000503f
          SAS address: 0x5000cca264d47000
          phy identifier: 0x0
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
        phy index: 1
          SAS device type: end device
          attached SAS address: 0x500000000000603f
          SAS address: 0x5000cca264d47001
          phy identifier: 0x1
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        slot = parsed.slots[5]
        self.assertEqual(slot.attached_sas_address, "500000000000503f")
        self.assertEqual(slot.sas_address, "5000cca264d47000")
        self.assertEqual(slot.phy_identifier, "0x0")

    def test_parse_sg_ses_aes_duplicate_slot_prefers_attached_path_over_absent_path(self) -> None:
        output = """
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
          SAS device type: no SAS device attached
          SAS address: 0x0
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 5
          SAS device type: end device
          SAS address: 0x5000cca264d47000
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg5")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        slot = parsed.slots[5]
        self.assertEqual(slot.sas_device_type, "end device")
        self.assertEqual(slot.sas_address, "5000cca264d47000")
        self.assertTrue(slot.present)

    def test_unmapped_element_collision_does_not_replace_reported_slot(self) -> None:
        output = """
ses0:
  Enclosure Name: ExampleCo GenericShelf
  Enclosure ID: 5000000000000007
  Element 7, Type: Array Device Slot
    Status: OK
    Description: ArrayDevice07
    Device Names: da7
  Element 8, Type: Array Device Slot
    Status: OK
    Description: Slot07
    Device Names: da8
""".strip()

        parsed = parse_sesutil_map(output)

        self.assertEqual(parsed[0].slots[7].device_names, ["da8"])
        self.assertEqual(parsed[0].slots[7].slot_number_source, "ses_description")
        self.assertEqual(parsed[0].unmapped_slots[0].element_id, 7)
        self.assertEqual(parsed[0].unmapped_slots[0].device_names, ["da7"])

    def test_commandless_ec_and_aes_merge_by_element_identity_in_any_order(self) -> None:
        def enclosure(source: str) -> SESMapEnclosure:
            if source == "aes":
                return SESMapEnclosure(
                    enclosure_id="enc-a",
                    enclosure_name="Shelf",
                    slots={
                        1: SESMapSlot(
                            slot_number=1,
                            element_id=0,
                            slot_number_source="ses_device_slot_number",
                            present=True,
                            presence_source="sg_ses_aes",
                        )
                    },
                )
            return SESMapEnclosure(
                enclosure_id="enc-a",
                enclosure_name="Shelf",
                slots={
                    0: SESMapSlot(
                        slot_number=0,
                        element_id=0,
                        status="OK",
                        identify_active=True,
                    )
                },
            )

        for first, second in (("aes", "ec"), ("ec", "aes")):
            with self.subTest(first=first):
                merged = _merge_ses_enclosures([enclosure(first), enclosure(second)])[0]
                self.assertIsNone(merged.ses_device)
                self.assertEqual(list(merged.slots), [1])
                self.assertEqual(merged.slots[1].status, "OK")
                self.assertTrue(merged.slots[1].identify_active)
                self.assertTrue(merged.slots[1].present)

    def test_reported_slot_provenance_wins_regardless_of_merge_order(self) -> None:
        def enclosure(source: str) -> SESMapEnclosure:
            is_reported = source == "ses_device_slot_number"
            return SESMapEnclosure(
                enclosure_id="enc-a",
                enclosure_name="Shelf",
                slots={
                    7: SESMapSlot(
                        slot_number=7,
                        element_id=7,
                        ses_device="/dev/sg7",
                        slot_number_source=source,
                        slot_number_warning=None if is_reported else "fallback warning",
                        control_targets=[
                            {
                                "ses_device": "/dev/sg7",
                                "ses_element_id": 7,
                                "ses_slot_number": 7 if is_reported else None,
                            }
                        ],
                    )
                },
            )

        for enclosures in (
            [enclosure("ses_element_id_fallback"), enclosure("ses_device_slot_number")],
            [enclosure("ses_device_slot_number"), enclosure("ses_element_id_fallback")],
        ):
            with self.subTest(order=enclosures[0].slots[7].slot_number_source):
                merged_slot = _merge_ses_enclosures(enclosures)[0].slots[7]
                self.assertEqual(merged_slot.slot_number_source, "ses_device_slot_number")
                self.assertIsNone(merged_slot.slot_number_warning)
                self.assertEqual(merged_slot.control_targets[0]["ses_slot_number"], 7)

    def test_stronger_empty_presence_clears_weaker_device_names_in_any_merge_order(self) -> None:
        def enclosure(*, strong: bool) -> SESMapEnclosure:
            return SESMapEnclosure(
                enclosure_id="enc-a",
                slots={
                    0: SESMapSlot(
                        slot_number=0,
                        present=False if strong else True,
                        presence_source="sg_ses_aes" if strong else "sesutil_show",
                        device_names=[] if strong else ["da-old"],
                        device_names_source=None if strong else "sesutil_show",
                        sas_address="0" if strong else None,
                        sas_address_source="sg_ses_aes" if strong else None,
                    )
                },
            )

        for enclosures in (
            [enclosure(strong=False), enclosure(strong=True)],
            [enclosure(strong=True), enclosure(strong=False)],
        ):
            with self.subTest(first=enclosures[0].slots[0].presence_source):
                slot = _merge_ses_enclosures(enclosures)[0].slots[0]
                self.assertIs(slot.present, False)
                self.assertEqual(slot.presence_source, "sg_ses_aes")
                self.assertFalse(slot.presence_conflict)
                self.assertEqual(slot.device_names, [])

    def test_equal_strength_presence_conflict_keeps_present_and_marks_conflict(self) -> None:
        def enclosure(present: bool) -> SESMapEnclosure:
            return SESMapEnclosure(
                enclosure_id="enc-a",
                slots={
                    0: SESMapSlot(
                        slot_number=0,
                        present=present,
                        presence_source="sg_ses_aes",
                    )
                },
            )

        for enclosures in (
            [enclosure(False), enclosure(True)],
            [enclosure(True), enclosure(False)],
        ):
            slot = _merge_ses_enclosures(enclosures)[0].slots[0]
            self.assertIs(slot.present, True)
            self.assertTrue(slot.presence_conflict)
            self.assertEqual(slot.presence_source, "sg_ses_aes")

    def test_equal_strength_sas_conflict_disables_address_correlation(self) -> None:
        def enclosure(address: str) -> SESMapEnclosure:
            return SESMapEnclosure(
                enclosure_id="enc-a",
                slots={
                    0: SESMapSlot(
                        slot_number=0,
                        present=True,
                        presence_source="sg_ses_aes",
                        sas_address=address,
                        sas_address_source="sg_ses_aes",
                    )
                },
            )

        for enclosures in (
            [enclosure("5000000000000001"), enclosure("5000000000000002")],
            [enclosure("5000000000000002"), enclosure("5000000000000001")],
        ):
            slot = _merge_ses_enclosures(enclosures)[0].slots[0]
            self.assertIsNone(slot.sas_address)
            self.assertTrue(slot.sas_address_conflict)
            self.assertEqual(slot.sas_address_source, "sg_ses_aes")

    def test_equal_strength_device_names_merge_in_deterministic_order(self) -> None:
        def enclosure(device_name: str) -> SESMapEnclosure:
            return SESMapEnclosure(
                enclosure_id="enc-a",
                slots={
                    0: SESMapSlot(
                        slot_number=0,
                        present=True,
                        presence_source="sg_ses_aes",
                        device_names=[device_name],
                        device_names_source="sg_ses_aes",
                    )
                },
            )

        candidates = [
            {
                0: {
                    "present": True,
                    "presence_source": "sg_ses_aes",
                    "device_names": [device_name],
                    "device_names_source": "sg_ses_aes",
                    "device_hint": device_name,
                }
            }
            for device_name in ("sdb", "sda")
        ]

        for first, second in ((0, 1), (1, 0)):
            slot = _merge_ses_enclosures(
                [enclosure(("sdb", "sda")[first]), enclosure(("sdb", "sda")[second])]
            )[0].slots[0]
            self.assertEqual(slot.device_names, ["sda", "sdb"])

            candidate = merge_slot_candidate_maps(candidates[first], candidates[second])[0]
            self.assertEqual(candidate["device_names"], ["sda", "sdb"])
            self.assertEqual(candidate["device_hint"], "sda")

    def test_sysfs_device_binding_overrides_aes_empty_evidence(self) -> None:
        enclosure = SESMapEnclosure(
            ses_device="/dev/sg4",
            slots={
                3: SESMapSlot(
                    slot_number=3,
                    slot_number_source="ses_device_slot_number",
                    present=False,
                    presence_source="sg_ses_aes",
                    sas_address="0",
                    sas_address_source="sg_ses_aes",
                )
            },
        )

        _apply_enclosure_sysfs_device_names([enclosure], {"sg4": {3: ["sdb"]}})

        slot = enclosure.slots[3]
        self.assertIs(slot.present, True)
        self.assertEqual(slot.presence_source, "enclosure_sysfs")
        self.assertEqual(slot.device_names, ["sdb"])
        self.assertEqual(slot.device_names_source, "enclosure_sysfs")

    def test_sysfs_device_binding_skips_slots_not_keyed_by_device_slot_number(self) -> None:
        # The kernel `slot` attribute is the SES device slot number. Slots
        # keyed by an EC element index (no source) or by an invalid AES
        # descriptor's element index share no coordinate with it, so the
        # hint has no bay to land on (issue #276).
        enclosure = SESMapEnclosure(
            ses_device="/dev/sg4",
            slots={
                1: SESMapSlot(slot_number=1, element_id=1, present=True, presence_source="sg_ses_ec"),
                2: SESMapSlot(
                    slot_number=2,
                    element_id=2,
                    slot_number_source="ses_element_index_invalid_descriptor",
                    present=False,
                    presence_source="sg_ses_aes",
                ),
                3: SESMapSlot(slot_number=3, element_id=2, slot_number_source="ses_description"),
                4: SESMapSlot(slot_number=4, element_id=3, slot_number_source="ses_device_slot_number"),
            },
        )

        _apply_enclosure_sysfs_device_names(
            [enclosure],
            {"sg4": {1: ["sda"], 2: ["sdb"], 3: ["sdc"], 4: ["sdd"]}},
        )

        self.assertEqual(enclosure.slots[1].device_names, [])
        self.assertIsNone(enclosure.slots[1].device_names_source)
        self.assertEqual(enclosure.slots[2].device_names, [])
        self.assertIs(enclosure.slots[2].present, False)
        self.assertEqual(enclosure.slots[2].presence_source, "sg_ses_aes")
        # `SlotNN` descriptor text has no checked-in evidence of equalling the
        # device slot number, so it is not a joinable coordinate either.
        self.assertEqual(enclosure.slots[3].device_names, [])
        self.assertEqual(enclosure.slots[4].device_names, ["sdd"])
        self.assertEqual(enclosure.slots[4].device_names_source, "enclosure_sysfs")
        # Every dropped binding is counted once for its SES path; the placed
        # one is not.
        self.assertEqual(enclosure.unplaced_sysfs_bindings_by_ses_device, {"/dev/sg4": 3})

    def test_merged_ses_sysfs_warnings_keep_path_counts_and_secondary_placements(self) -> None:
        enclosure = SESMapEnclosure(
            ses_device="/dev/sg10",
            ses_devices=["/dev/sg10", "/dev/sg2"],
            slots={
                0: SESMapSlot(slot_number=0, element_id=0, presence_source="sg_ses_ec"),
                1: SESMapSlot(
                    slot_number=1,
                    element_id=1,
                    slot_number_source="ses_device_slot_number",
                ),
            },
        )

        _apply_enclosure_sysfs_device_names(
            [enclosure],
            {
                "sg10": {0: ["sda"], 1: ["sdb"]},
                "sg2": {0: ["sdc"], 1: ["sdd"], 2: ["sde"]},
            },
        )
        _, selected = build_slot_candidates_from_ses_enclosures(
            [enclosure],
            2,
            None,
            enclosures_are_merged=True,
        )

        self.assertEqual(enclosure.slots[1].device_names, ["sdb", "sdd"])
        self.assertEqual(
            enclosure.unplaced_sysfs_bindings_by_ses_device,
            {"/dev/sg10": 1, "/dev/sg2": 2},
        )
        self.assertEqual(
            [warning for warning in selected["warnings"] if "Kernel enclosure bindings" in warning],
            [
                "Kernel enclosure bindings for /dev/sg2 could not be placed: "
                "SES reported no device slot numbers for 2 bound devices.",
                "Kernel enclosure bindings for /dev/sg10 could not be placed: "
                "SES reported no device slot numbers for 1 bound device.",
            ],
        )

    def test_candidate_map_keeps_stronger_empty_presence_in_any_merge_order(self) -> None:
        strong = {
            0: {
                "present": False,
                "presence_source": "sg_ses_aes",
                "device_names": [],
                "device_names_source": None,
            }
        }
        weak = {
            0: {
                "present": True,
                "presence_source": "sesutil_show",
                "device_names": ["da-old"],
                "device_names_source": "sesutil_show",
                "device_hint": "da-old",
            }
        }

        for base, overlay in ((weak, strong), (strong, weak)):
            candidate = merge_slot_candidate_maps(base, overlay)[0]
            self.assertIs(candidate["present"], False)
            self.assertEqual(candidate["presence_source"], "sg_ses_aes")
            self.assertEqual(candidate["device_names"], [])
            self.assertIsNone(candidate.get("device_hint"))

    def test_zero_based_preferred_enclosure_capacity_stops_at_exact_slot_count(self) -> None:
        enclosures = [
            SESMapEnclosure(
                enclosure_id=enclosure_id,
                enclosure_name=name,
                slots={slot: SESMapSlot(slot_number=slot) for slot in range(30)},
            )
            for enclosure_id, name in (("enc-a", "A Shelf"), ("enc-b", "B Shelf"))
        ]

        candidates, selected = build_slot_candidates_from_ses_enclosures(enclosures, 30, None)

        self.assertEqual(len(candidates), 30)
        self.assertEqual(selected["id"], "enc-a")

    def test_parse_storcli_physical_drives_rejects_eidless_slot_identity(self) -> None:
        output = """
{
  "Controllers": [
    {
      "Command Status": {"Status": "Success"},
      "Response Data": {
        "Drive Information": [
          {"EID:Slt": ":5", "DID": 5, "State": "JBOD", "Model": "Example Disk"}
        ]
      }
    }
  ]
}
""".strip()

        parsed = parse_storcli_physical_drives(output)

        self.assertEqual(parsed, [])

    def test_parse_sg_ses_aes_ignores_warning_preamble_for_enclosure_name(self) -> None:
        output = """
warning: diagnostic page was retried
  ExampleCo  GenericShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000000
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 0
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg0")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_name, "ExampleCo GenericShelf 0001")

    def test_parse_sg_ses_enclosure_status_ignores_warning_preamble_for_enclosure_name(self) -> None:
        output = """
warning: diagnostic page was retried
  ExampleCo  GenericShelf  0001
  Primary enclosure logical identifier (hex): 5000000000000000
Enclosure status diagnostic page:
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
""".strip()

        parsed = parse_sg_ses_enclosure_status(output, "sg_ses ec /dev/sg0")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_name, "ExampleCo GenericShelf 0001")

    def test_parse_sg_ses_join_filter_ignores_warning_preamble_for_enclosure_name(self) -> None:
        output = """
warning: joined page was retried
ExampleCo  GenericShelf  0001
Primary enclosure logical identifier (hex): 5000000000000000
Slot00 [0,0]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: OK
""".strip()

        parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg0")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_name, "ExampleCo GenericShelf 0001")

    def test_parse_sg_ses_join_filter_stops_name_scan_at_first_slot_without_header(self) -> None:
        output = """
ExampleCo  GenericShelf  0001
Slot00 [0,0]  Element type: Array device slot
  Enclosure Status:
    Predicted failure=0, Disabled=0, Swap=0, status: OK
""".strip()

        parsed = parse_sg_ses_join_filter(output, "sg_ses join /dev/sg0")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_name, "ExampleCo GenericShelf 0001")

    def test_parse_sg_ses_aes_marks_empty_rear_slots(self) -> None:
        output = """
  LSI       SAS3x28           0601
  Primary enclosure logical identifier (hex): 500304801e977aff
Additional element status diagnostic page:
  generation code: 0x0
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 2  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 2
        phy index: 0
          SAS device type: no SAS device attached
          attached SAS address: 0x0
          SAS address: 0x0
    Element type: SAS expander, subenclosure id: 0 [ti=1]
      Element index: 12  eiioe=0
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg38")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_label, "Rear 12 Bay")
        self.assertEqual(parsed.layout_rows, 3)
        self.assertEqual(parsed.layout_columns, 4)
        self.assertEqual(parsed.slot_layout, [[2, 5, 8, 11], [1, 4, 7, 10], [0, 3, 6, 9]])
        self.assertEqual(parsed.slots[2].sas_address, "0")
        self.assertFalse(parsed.slots[2].present)

    def test_parse_sg_ses_enclosure_status_extracts_identify_state(self) -> None:
        output = """
  LSI       SAS3x40           0601
  Primary enclosure logical identifier (hex): 5003048001c1043f
Enclosure status diagnostic page:
  INVOP=0, INFO=0, NON-CRIT=0, CRIT=0, UNRECOV=0
  generation code: 0x1
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Overall descriptor:
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
        Slot address: 0
        App client bypassed A=0, Do not remove=0, Enc bypassed A=0
        Insert ready=0, RMV=0, Ident=1, Report=0, App client bypassed B=0
      Element 1 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Not installed
        Slot address: 1
        App client bypassed A=0, Do not remove=0, Enc bypassed A=0
        Insert ready=0, RMV=0, Ident=0, Report=0, App client bypassed B=0
    Element type: SAS expander, subenclosure id: 0 [ti=1]
      Overall descriptor:
""".strip()

        parsed = parse_sg_ses_enclosure_status(output, "sg_ses ec /dev/sg27")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.enclosure_label, "Front 24 Bay")
        self.assertEqual(parsed.slots[0].identify_active, True)
        self.assertEqual(parsed.slots[0].status, "OK")
        self.assertTrue(parsed.slots[0].present)
        self.assertEqual(parsed.slots[0].control_targets[0]["ses_slot_number"], 0)
        self.assertEqual(parsed.slots[1].identify_active, False)
        self.assertFalse(parsed.slots[1].present)

    def test_parse_sesutil_show_assigns_24_bay_profile_metadata(self) -> None:
        output = """
ses2:  <LSI SAS3x40 0601>; ID: 50030480090c4f7f
Desc  Device  Model  Serial  Status
Slot 00  da0  Samsung SSD  SER000  OK
Slot 06  -  -  -  Not installed
""".strip()

        parsed = parse_sesutil_show_enclosures(output)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].profile_id, "supermicro-ssg-6048r-front-24")
        self.assertEqual(parsed[0].enclosure_label, "Front 24 Bay")
        self.assertEqual(parsed[0].layout_rows, 6)
        self.assertEqual(parsed[0].layout_columns, 4)
        self.assertEqual(parsed[0].slot_layout, [[5, 11, 17, 23], [4, 10, 16, 22], [3, 9, 15, 21], [2, 8, 14, 20], [1, 7, 13, 19], [0, 6, 12, 18]])
        self.assertTrue(parsed[0].slots[0].present)
        self.assertFalse(parsed[0].slots[6].present)

    def test_parse_sesutil_show_does_not_fabricate_element_control_targets(self) -> None:
        output = """
ses2:  <ExampleCo OneBasedShelf 0001>; ID: 5000000000000101
Desc  Device  Model  Serial  Status
Slot 01  da0  Example Disk  SYNTH0001  OK
""".strip()

        slot = parse_sesutil_show_enclosures(output)[0].slots[1]

        self.assertIsNone(slot.element_id)
        self.assertEqual(slot.control_targets, [])

    def test_core_map_show_mismatch_keeps_only_authentic_map_element_target(self) -> None:
        ses_map = """
ses2:
  Enclosure Name: ExampleCo OneBasedShelf
  Enclosure ID: 5000000000000101
  Element 2, Type: Array Device Slot
    Status: OK
    Description: Slot01
    Device Names: da0, pass0
""".strip()
        ses_show = """
ses2:  <ExampleCo OneBasedShelf 0001>; ID: 5000000000000101
Desc  Device  Model  Serial  Status
Slot 01  da0  Example Disk  SYNTH0001  OK
""".strip()

        parsed = parse_ssh_outputs(
            {"sesutil map": ses_map, "sesutil show": ses_show},
            slot_count=1,
            enclosure_filter=None,
        )

        self.assertEqual(
            parsed.ses_slot_candidates[0]["ses_targets"],
            [
                {
                    "ses_device": "/dev/ses2",
                    "ses_element_id": 2,
                    "ses_slot_number": 1,
                }
            ],
        )

    def test_core_map_show_matching_indexes_keeps_one_authentic_target(self) -> None:
        ses_map = """
ses2:
  Enclosure Name: ExampleCo MatchingShelf
  Enclosure ID: 5000000000000202
  Element 1, Type: Array Device Slot
    Status: OK
    Description: Slot01
    Device Names: da0, pass0
""".strip()
        ses_show = """
ses2:  <ExampleCo MatchingShelf 0001>; ID: 5000000000000202
Desc  Device  Model  Serial  Status
Slot 01  da0  Example Disk  SYNTH0002  OK
""".strip()

        parsed = parse_ssh_outputs(
            {"sesutil map": ses_map, "sesutil show": ses_show},
            slot_count=1,
            enclosure_filter=None,
        )

        self.assertEqual(len(parsed.ses_slot_candidates[0]["ses_targets"]), 1)
        self.assertEqual(parsed.ses_slot_candidates[0]["ses_targets"][0]["ses_element_id"], 1)
        self.assertEqual(parsed.ses_slot_candidates[0]["ses_targets"][0]["ses_slot_number"], 1)

    def test_parse_ssh_outputs_builds_ses_candidates_once_after_collecting_all_evidence(self) -> None:
        ses_map = """
ses2:
  Enclosure Name: LSI SAS3x40
  Enclosure ID: 50030480090c4f7f
  Element 0, Type: Array Device Slot
    Status: OK
    Description: Slot00
    Device Names: da0, pass0
""".strip()
        ses_show = """
ses2:  <LSI SAS3x40 0601>; ID: 50030480090c4f7f
Desc  Device  Model  Serial  Status
Slot 00  da0  Samsung SSD  SER000  OK
""".strip()

        with (
            patch(
                "app.services.parsers.build_slot_candidates_from_ses_enclosures",
                wraps=build_slot_candidates_from_ses_enclosures,
            ) as build_candidates,
            patch(
                "app.services.parsers._merge_ses_enclosures",
                wraps=_merge_ses_enclosures,
            ) as merge_enclosures,
        ):
            parsed = parse_ssh_outputs(
                {"sesutil map": ses_map, "sesutil show": ses_show},
                slot_count=24,
                enclosure_filter=None,
            )

        self.assertEqual(build_candidates.call_count, 1)
        self.assertEqual(merge_enclosures.call_count, 1)
        self.assertEqual(len(parsed.ses_enclosures), 1)
        self.assertEqual(parsed.ses_slot_candidates[0]["device_hint"], "da0")
        self.assertEqual(parsed.ses_slot_candidates[0]["model_hint"], "Samsung SSD")
        self.assertEqual(parsed.ses_slot_candidates[0]["serial_hint"], "SER000")
        self.assertEqual(parsed.ses_slot_candidates[0]["ses_device"], "/dev/ses2")

    def test_parse_ssh_outputs_preserves_default_meta_for_unparseable_sesutil_map(self) -> None:
        with patch(
            "app.services.parsers.build_slot_candidates_from_ses_enclosures",
            wraps=build_slot_candidates_from_ses_enclosures,
        ) as build_candidates:
            parsed = parse_ssh_outputs(
                {"sesutil map": "command returned no parseable enclosure rows"},
                slot_count=24,
                enclosure_filter=None,
            )

        self.assertEqual(build_candidates.call_count, 1)
        self.assertEqual(parsed.ses_slot_candidates, {})
        self.assertEqual(
            parsed.ses_selected_meta,
            {
                "id": None,
                "label": None,
                "name": None,
                "unmapped_ses_elements": [],
                "warnings": [],
            },
        )

    def test_parse_ssh_outputs_show_only_candidates_omit_empty_overlay_fields(self) -> None:
        ses_show = """
ses2:  <LSI SAS3x40 0601>; ID: 50030480090c4f7f
Desc  Device  Model  Serial  Status
Slot 00  da0  Samsung SSD  SER000  OK
""".strip()

        parsed = parse_ssh_outputs(
            {"sesutil show": ses_show},
            slot_count=24,
            enclosure_filter=None,
        )

        candidate = parsed.ses_slot_candidates[0]
        self.assertFalse(any(value is None for value in candidate.values()))
        self.assertNotIn("sas_address_hint", candidate)
        self.assertNotIn("ses_element_id", candidate)
        self.assertNotIn("slot_number_source", candidate)
        self.assertNotIn("ses_targets", candidate)

    def test_parse_ssh_outputs_preserves_scale_profile_id_after_ses_merge(self) -> None:
        aes_output = """
  LSI       SAS3x40           0601
  Primary enclosure logical identifier (hex): 5003048001c1043f
Additional element status diagnostic page:
  generation code: 0x0
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5003048001c1043f
          SAS address: 0x5000cca264d473d5
    Element type: SAS expander, subenclosure id: 0 [ti=1]
      Element index: 24  eiioe=0
""".strip()
        ec_output = """
  LSI       SAS3x40           0601
  Primary enclosure logical identifier (hex): 5003048001c1043f
Enclosure status diagnostic page:
  INVOP=0, INFO=0, NON-CRIT=0, CRIT=0, UNRECOV=0
  generation code: 0x1
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Overall descriptor:
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
        Slot address: 0
        App client bypassed A=0, Do not remove=0, Enc bypassed A=0
        Insert ready=0, RMV=0, Ident=1, Report=0, App client bypassed B=0
    Element type: SAS expander, subenclosure id: 0 [ti=1]
      Overall descriptor:
""".strip()

        parsed = parse_ssh_outputs(
            {
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg27": aes_output,
                "sudo -n /usr/bin/sg_ses -p ec /dev/sg27": ec_output,
            },
            slot_count=24,
            enclosure_filter="",
            selected_enclosure_id="5003048001c1043f",
        )

        self.assertEqual(len(parsed.ses_enclosures), 1)
        self.assertEqual(parsed.ses_enclosures[0].profile_id, "supermicro-ssg-6048r-front-24")
        self.assertIn(0, parsed.ses_slot_candidates)
        self.assertEqual(parsed.ses_slot_candidates[0]["ses_device"], "/dev/sg27")
        self.assertEqual(parsed.ses_slot_candidates[0]["attached_sas_address"], "5003048001c1043f")

    def test_parse_smart_test_results_uses_latest_test(self) -> None:
        results = [
            {
                "disk": "da65",
                "current_test": None,
                "tests": [
                    {
                        "description": "Background short",
                        "status": "SUCCESS",
                        "status_verbose": "Completed",
                        "lifetime": 24548,
                    }
                ],
            },
            {
                "disk": "da18",
                "current_test": None,
                "tests": [],
            },
        ]

        parsed = parse_smart_test_results(results)

        self.assertIn("da65", parsed)
        self.assertEqual(parsed["da65"]["description"], "Background short")
        self.assertEqual(parsed["da65"]["status_verbose"], "Completed")
        self.assertEqual(parsed["da65"]["lifetime"], 24548)
        self.assertNotIn("da18", parsed)

    def test_parse_smartctl_summary_extracts_phase_one_fields(self) -> None:
        output = """
{
  "temperature": {"current": 31},
  "power_on_time": {"hours": 24566},
  "logical_block_size": 512,
  "physical_block_size": 4096
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["temperature_c"], 31)
        self.assertEqual(parsed["power_on_hours"], 24566)
        self.assertEqual(parsed["power_on_days"], 1023)
        self.assertEqual(parsed["logical_block_size"], 512)
        self.assertEqual(parsed["physical_block_size"], 4096)
        self.assertIsNone(parsed["message"])

    def test_parse_smartctl_summary_extracts_ata_volume_cache_and_link_metrics(self) -> None:
        output = """
{
  "device": {"protocol": "ATA"},
  "temperature": {"current": 48},
  "power_on_time": {"hours": 39905},
  "logical_block_size": 512,
  "physical_block_size": 4096,
  "rotation_rate": 7200,
  "form_factor": {"name": "3.5 inches"},
  "firmware_version": "SN04",
  "smart_status": {"passed": true},
  "sata_version": {"string": "SATA 3.1"},
  "interface_speed": {"current": {"string": "6.0 Gb/s"}},
  "read_lookahead": {"enabled": true},
  "write_cache": {"enabled": true},
  "ata_smart_attributes": {
    "table": [
      {"id": 241, "raw": {"value": 1000}},
      {"id": 242, "raw": {"value": 2000}}
    ]
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["smart_health_status"], "PASSED")
        self.assertEqual(parsed["rotation_rate_rpm"], 7200)
        self.assertEqual(parsed["form_factor"], "3.5 inches")
        self.assertEqual(parsed["firmware_version"], "SN04")
        self.assertEqual(parsed["transport_protocol"], "ATA")
        self.assertEqual(parsed["protocol_version"], "SATA 3.1")
        self.assertEqual(parsed["negotiated_link_rate"], "6.0 Gb/s")
        self.assertTrue(parsed["read_cache_enabled"])
        self.assertTrue(parsed["writeback_cache_enabled"])
        self.assertEqual(parsed["bytes_read"], 1024000)
        self.assertEqual(parsed["bytes_written"], 512000)

    def test_parse_smartctl_summary_hides_annualized_write_for_low_hour_disk(self) -> None:
        output = """
{
  "device": {"protocol": "ATA"},
  "power_on_time": {"hours": 183},
  "logical_block_size": 512,
  "ata_smart_attributes": {
    "table": [
      {"id": 241, "raw": {"value": 35446452000}},
      {"id": 242, "raw": {"value": 590679}}
    ]
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertEqual(parsed["bytes_written"], 18148583424000)
        self.assertEqual(parsed["bytes_read"], 302427648)
        self.assertIsNone(parsed["annualized_bytes_read"])
        self.assertIsNone(parsed["annualized_bytes_written"])

    def test_parse_smartctl_summary_extracts_ata_host_counters_with_32mib_units(self) -> None:
        output = """
{
  "device": {"protocol": "ATA"},
  "power_on_time": {"hours": 61405},
  "logical_block_size": 512,
  "ata_smart_attributes": {
    "table": [
      {"id": 241, "name": "Host_Writes_32MiB", "raw": {"value": 6139}},
      {"id": 242, "name": "Host_Reads_32MiB", "raw": {"value": 5882}}
    ]
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertEqual(parsed["bytes_written"], 205990658048)
        self.assertEqual(parsed["bytes_read"], 197367169024)
        self.assertEqual(parsed["annualized_bytes_read"], 28156280443)
        self.assertEqual(parsed["annualized_bytes_written"], 29386502149)

    def test_parse_smartctl_summary_prefers_ata_device_statistics_over_vendor_host_units(self) -> None:
        output = """
{
  "device": {"protocol": "ATA"},
  "logical_block_size": 512,
  "power_on_time": {"hours": 61405},
  "ata_smart_attributes": {
    "table": [
      {"id": 241, "name": "Host_Writes_32MiB", "raw": {"value": 6139}},
      {"id": 242, "name": "Host_Reads_32MiB", "raw": {"value": 5882}}
    ]
  },
  "ata_device_statistics": {
    "pages": [
      {
        "number": 1,
        "name": "General Statistics",
        "revision": 2,
        "table": [
          {"offset": 24, "name": "Logical Sectors Written", "size": 6, "value": 4286460901},
          {"offset": 40, "name": "Logical Sectors Read", "size": 6, "value": 3747432196}
        ]
      }
    ]
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertEqual(parsed["bytes_written"], 2194667981312)
        self.assertEqual(parsed["bytes_read"], 1918685284352)

    def test_parse_smartctl_summary_extracts_ata_endurance_and_command_counters(self) -> None:
        output = """
{
  "device": {"protocol": "ATA"},
  "logical_block_size": 512,
  "power_on_time": {"hours": 1000},
  "ata_smart_attributes": {
    "table": [
      {"id": 12, "name": "Power_Cycle_Count", "raw": {"value": 33}},
      {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 2}},
      {"id": 232, "name": "Available_Reservd_Space", "raw": {"value": 90}}
    ]
  },
  "ata_device_statistics": {
    "pages": [
      {
        "number": 1,
        "name": "General Statistics",
        "revision": 2,
        "table": [
          {"offset": 8, "name": "Lifetime Power-On Resets", "size": 4, "value": 12},
          {"offset": 24, "name": "Logical Sectors Written", "size": 6, "value": 1024},
          {"offset": 32, "name": "Number of Write Commands", "size": 6, "value": 900},
          {"offset": 40, "name": "Logical Sectors Read", "size": 6, "value": 2048},
          {"offset": 48, "name": "Number of Read Commands", "size": 6, "value": 450}
        ]
      },
      {
        "number": 6,
        "name": "Transport Statistics",
        "revision": 1,
        "table": [
          {"offset": 8, "name": "Number of Hardware Resets", "size": 4, "value": 5},
          {"offset": 24, "name": "Number of Interface CRC Errors", "size": 4, "value": 2}
        ]
      },
      {
        "number": 7,
        "name": "Solid State Device Statistics",
        "revision": 1,
        "table": [
          {"offset": 8, "name": "Percentage Used Endurance Indicator", "size": 1, "value": 10}
        ]
      }
    ]
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertEqual(parsed["power_cycle_count"], 33)
        self.assertEqual(parsed["power_on_resets"], 12)
        self.assertEqual(parsed["available_spare_percent"], 90)
        self.assertEqual(parsed["endurance_used_percent"], 10)
        self.assertEqual(parsed["endurance_remaining_percent"], 90)
        self.assertEqual(parsed["bytes_written"], 524288)
        self.assertEqual(parsed["bytes_read"], 1048576)
        self.assertEqual(parsed["annualized_bytes_read"], 9185525)
        self.assertEqual(parsed["annualized_bytes_written"], 4592762)
        self.assertEqual(parsed["estimated_lifetime_bytes_written"], 5242880)
        self.assertEqual(parsed["estimated_remaining_bytes_written"], 4718592)
        self.assertEqual(parsed["write_commands"], 900)
        self.assertEqual(parsed["read_commands"], 450)
        self.assertEqual(parsed["hardware_resets"], 5)
        self.assertEqual(parsed["interface_crc_errors"], 2)

    def test_parse_smartctl_summary_extracts_scsi_self_test_history(self) -> None:
        output = """
{
  "temperature": {"current": 35},
  "power_on_time": {"hours": 49119},
  "logical_block_size": 4096,
  "scsi_self_test_0": {
    "code": {"string": "Background short"},
    "result": {"string": "Completed"},
    "power_on_time": {"hours": 49108}
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["last_test_type"], "Background short")
        self.assertEqual(parsed["last_test_status"], "Completed")
        self.assertEqual(parsed["last_test_lifetime_hours"], 49108)
        self.assertEqual(parsed["last_test_age_hours"], 11)

    def test_parse_smartctl_summary_extracts_scsi_transport_details(self) -> None:
        output = """
{
  "logical_unit_id": "0x5000cca264d473d4",
  "power_on_time": {"hours": 49144},
  "rotation_rate": 7200,
  "form_factor": {"name": "3.5 inches"},
  "scsi_transport_protocol": {"name": "SAS (SPL-4)"},
  "scsi_environmental_reports": {"temperature_1": {"current": 36}},
  "scsi_error_counter_log": {
    "read": {"gigabytes_processed": "330638.625"},
    "write": {"gigabytes_processed": "111254.503"}
  },
  "scsi_sas_port_0": {
    "phy_0": {
      "attached_device_type": "expander device",
      "negotiated_logical_link_rate": "phy enabled; 12 Gbps",
      "sas_address": "0x5000cca264d473d5",
      "attached_sas_address": "0x5003048001c1043f"
    }
  },
  "scsi_sas_port_1": {
    "phy_0": {
      "attached_device_type": "no device attached",
      "negotiated_logical_link_rate": "phy enabled; unknown",
      "sas_address": "0x5000cca264d473d6",
      "attached_sas_address": "0x0"
    }
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["temperature_c"], 36)
        self.assertEqual(parsed["rotation_rate_rpm"], 7200)
        self.assertEqual(parsed["form_factor"], "3.5 inches")
        self.assertEqual(parsed["transport_protocol"], "SAS (SPL-4)")
        self.assertEqual(parsed["logical_unit_id"], "0x5000cca264d473d4")
        self.assertEqual(parsed["sas_address"], "0x5000cca264d473d5")
        self.assertEqual(parsed["attached_sas_address"], "0x5003048001c1043f")
        self.assertEqual(parsed["negotiated_link_rate"], "phy enabled; 12 Gbps")
        self.assertEqual(parsed["bytes_read"], 330638625000000)
        self.assertEqual(parsed["bytes_written"], 111254503000000)
        self.assertEqual(parsed["annualized_bytes_read"], 58936886598567)
        self.assertEqual(parsed["annualized_bytes_written"], 19831300795214)

    def test_parse_smartctl_summary_strips_scsi_identifier_error_suffixes(self) -> None:
        output = """
{
  "device": {"protocol": "SCSI"},
  "logical_unit_id": "0x00e04c2020202000error: designator length",
  "scsi_sas_port_0": {
    "phy_0": {
      "sas_address": "0x5000cca264d473d5error: designator length",
      "attached_sas_address": "0x5003048001c1043ferror: designator length"
    }
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["logical_unit_id"], "0xe04c2020202000")
        self.assertEqual(parsed["sas_address"], "0x5000cca264d473d5")
        self.assertEqual(parsed["attached_sas_address"], "0x5003048001c1043f")

    def test_parse_smartctl_text_enrichment_extracts_ata_cache_health_and_link_metadata(self) -> None:
        output = """
=== START OF INFORMATION SECTION ===
SMART overall-health self-assessment test result: PASSED
TRIM Command: Available, deterministic, zeroed
SATA Version is: SATA 3.1, 6.0 Gb/s (current: 6.0 Gb/s)
Rd look-ahead is: Enabled
Write cache is: Enabled
""".strip()

        parsed = parse_smartctl_text_enrichment(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["smart_health_status"], "PASSED")
        self.assertTrue(parsed["trim_supported"])
        self.assertEqual(parsed["protocol_version"], "SATA 3.1, 6.0 Gb/s")
        self.assertEqual(parsed["negotiated_link_rate"], "6.0 Gb/s")
        self.assertTrue(parsed["read_cache_enabled"])
        self.assertTrue(parsed["writeback_cache_enabled"])

    def test_parse_smartctl_summary_extracts_nvme_wear_and_write_metrics(self) -> None:
        output = """
{
  "device": {"protocol": "NVMe"},
  "power_on_time": {"hours": 1000},
  "nvme_smart_health_information_log": {
    "available_spare": 95,
    "available_spare_threshold": 10,
    "percentage_used": 25,
    "data_units_read": 2000000,
    "data_units_written": 1000000,
    "media_errors": 3,
    "unsafe_shutdowns": 4
  }
}
""".strip()

        parsed = parse_smartctl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["transport_protocol"], "NVMe")
        self.assertEqual(parsed["rotation_rate_rpm"], 0)
        self.assertEqual(parsed["available_spare_percent"], 95)
        self.assertEqual(parsed["available_spare_threshold_percent"], 10)
        self.assertEqual(parsed["endurance_used_percent"], 25)
        self.assertEqual(parsed["endurance_remaining_percent"], 75)
        self.assertEqual(parsed["bytes_read"], 1024000000000)
        self.assertEqual(parsed["bytes_written"], 512000000000)
        self.assertEqual(parsed["annualized_bytes_read"], 8970240000000)
        self.assertEqual(parsed["annualized_bytes_written"], 4485120000000)
        self.assertEqual(parsed["estimated_lifetime_bytes_written"], 2048000000000)
        self.assertEqual(parsed["estimated_remaining_bytes_written"], 1536000000000)
        self.assertEqual(parsed["media_errors"], 3)
        self.assertEqual(parsed["unsafe_shutdowns"], 4)

    def test_parse_nvme_smart_log_summary_extracts_controller_native_metrics(self) -> None:
        output = """
{
  "temperature": 308,
  "avail_spare": 100,
  "spare_thresh": 5,
  "percent_used": 6,
  "data_units_read": 33056747326,
  "data_units_written": 4624969197,
  "power_on_hours": 32283,
  "unsafe_shutdowns": 61,
  "media_errors": 0
}
""".strip()

        parsed = parse_nvme_smart_log_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["temperature_c"], 35)
        self.assertEqual(parsed["power_on_hours"], 32283)
        self.assertEqual(parsed["available_spare_percent"], 100)
        self.assertEqual(parsed["available_spare_threshold_percent"], 5)
        self.assertEqual(parsed["endurance_used_percent"], 6)
        self.assertEqual(parsed["endurance_remaining_percent"], 94)
        self.assertEqual(parsed["bytes_read"], 16925054630912000)
        self.assertEqual(parsed["bytes_written"], 2367984228864000)
        self.assertEqual(parsed["annualized_bytes_read"], 4592617742055854)
        self.assertEqual(parsed["media_errors"], 0)
        self.assertEqual(parsed["unsafe_shutdowns"], 61)
        self.assertEqual(parsed["transport_protocol"], "NVMe")

    def test_parse_nvme_id_ctrl_summary_extracts_identity_thresholds(self) -> None:
        output = """
{
  "fr": "11300DR0",
  "ver": 66048,
  "wctemp": 348,
  "cctemp": 353
}
""".strip()

        parsed = parse_nvme_id_ctrl_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["firmware_version"], "11300DR0")
        self.assertEqual(parsed["protocol_version"], "1.2")
        self.assertEqual(parsed["warning_temperature_c"], 75)
        self.assertEqual(parsed["critical_temperature_c"], 80)

    def test_parse_nvme_id_ns_summary_extracts_namespace_identifiers(self) -> None:
        output = """
{
  "eui64": "00a075102b91c7cf",
  "nguid": "000000000000001000a075012b91c7cf"
}
""".strip()

        parsed = parse_nvme_id_ns_summary(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["namespace_eui64"], "eui.00a075102b91c7cf")
        self.assertEqual(parsed["namespace_nguid"], "000000000000001000a075012b91c7cf")

    def test_parse_smartctl_text_enrichment_extracts_transport_fields(self) -> None:
        output = """
Transport protocol:   SAS (SPL-4)
Logical Unit id:      0x5000cca23b713c80
Read Cache is:        Enabled
Writeback Cache is:   Disabled
    negotiated logical link rate: phy enabled; 12 Gbps
    SAS address = 0x5000cca23b713c81
    attached SAS address = 0x500304801f715f3f
""".strip()

        parsed = parse_smartctl_text_enrichment(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["transport_protocol"], "SAS (SPL-4)")
        self.assertEqual(parsed["logical_unit_id"], "0x5000cca23b713c80")
        self.assertEqual(parsed["read_cache_enabled"], True)
        self.assertEqual(parsed["writeback_cache_enabled"], False)
        self.assertEqual(parsed["sas_address"], "0x5000cca23b713c81")
        self.assertEqual(parsed["attached_sas_address"], "0x500304801f715f3f")
        self.assertEqual(parsed["negotiated_link_rate"], "phy enabled; 12 Gbps")

    def test_parse_smartctl_text_enrichment_strips_scsi_identifier_error_suffixes(self) -> None:
        output = """
Transport protocol:   SCSI
Logical Unit id:      0x00e04c2020202000error: designator length
    SAS address = 0x5000cca23b713c81error: designator length
    attached SAS address = 0x500304801f715f3ferror: designator length
""".strip()

        parsed = parse_smartctl_text_enrichment(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["logical_unit_id"], "0xe04c2020202000")
        self.assertEqual(parsed["sas_address"], "0x5000cca23b713c81")
        self.assertEqual(parsed["attached_sas_address"], "0x500304801f715f3f")

    def test_parse_linux_inventory_helpers_extract_useful_structures(self) -> None:
        lsblk_payload = """
{
  "blockdevices": [
    {
      "name": "nvme0n2",
      "serial": "ABC123",
      "model": "Micron",
      "size": "1.7T",
      "tran": "nvme",
      "children": [
        {
          "name": "md1",
          "type": "raid1",
          "children": [
            {
              "name": "md5",
              "type": "raid0",
              "mountpoint": "/mnt/nvme_raid"
            }
          ]
        }
      ]
    }
  ]
}
""".strip()
        mdadm_payload = "ARRAY /dev/md5 metadata=1.2 name=gpu-server:5 UUID=d99263a4:ecf74f58:98073ff4:f9be9c77"
        nvme_subsys_payload = """
{
  "Subsystems": [
    {
      "Name": "nvme-subsys0",
      "NQN": "nqn.test",
      "Paths": [
        {
          "Name": "nvme0",
          "Transport": "pcie",
          "Address": "10000:01:00.0",
          "State": "live"
        }
      ]
    }
  ]
}
""".strip()

        blockdevices = parse_lsblk_json(lsblk_payload)
        arrays = parse_mdadm_detail_scan(mdadm_payload)
        subsystems = parse_nvme_list_subsys_json(nvme_subsys_payload)

        self.assertEqual(blockdevices[0]["name"], "nvme0n2")
        self.assertEqual(arrays["md5"].name, "gpu-server:5")
        self.assertEqual(arrays["/dev/md5"].uuid, "d99263a4:ecf74f58:98073ff4:f9be9c77")
        self.assertEqual(subsystems["nvme0"]["address"], "10000:01:00.0")
        self.assertEqual(subsystems["nvme0"]["transport"], "pcie")

    def test_parse_smartctl_summary_handles_invalid_json(self) -> None:
        parsed = parse_smartctl_summary("not-json")

        self.assertFalse(parsed["available"])
        self.assertEqual(parsed["message"], "SMART JSON parsing failed.")

    def test_parse_esxcli_smart_get_keeps_host_error_counts_distinct_from_uncorrected(self) -> None:
        output = """
Parameter                 Value         Threshold  Worst  Raw
------------------------  ------------  ---------  -----  ---
Health Status             OK            N/A        N/A    N/A
Write Error Count         0             N/A        N/A    N/A
Read Error Count          444578        N/A        N/A    N/A
Power Cycle Count         29            N/A        N/A    N/A
Reallocated Sector Count  0             N/A        N/A    N/A
Drive Temperature         34            N/A        N/A    N/A
Write Sectors TOT Count   224246729061  N/A        N/A    N/A
Read Sectors TOT Count    49378996762   N/A        N/A    N/A
Program Fail Count        0             N/A        N/A    N/A
Erase Fail Count          0             N/A        N/A    N/A
""".strip()

        parsed = parse_esxcli_smart_get(output, logical_block_size=512)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["smart_health_status"], "OK")
        self.assertEqual(parsed["read_error_count"], 444578)
        self.assertEqual(parsed["write_error_count"], 0)
        self.assertNotIn("uncorrected_read_errors", parsed)
        self.assertNotIn("uncorrected_write_errors", parsed)


if __name__ == "__main__":
    unittest.main()


class AtaSelfTestLifetimeTests(unittest.TestCase):
    def test_parse_smartctl_summary_reads_ata_lifetime_hours_as_int(self) -> None:
        # smartctl reports ATA self-test lifetime_hours as a plain integer;
        # only the SCSI power_on_time field uses the {"hours": N} dict shape.
        payload = {
            "device": {"protocol": "ATA"},
            "smart_status": {"passed": True},
            "power_on_time": {"hours": 43110},
            "ata_smart_self_test_log": {
                "standard": {
                    "table": [
                        {
                            "type": {"value": 1, "string": "Short offline"},
                            "status": {"value": 0, "string": "Completed without error", "passed": True},
                            "lifetime_hours": 43104,
                        }
                    ]
                }
            },
        }

        import json as _json

        summary = parse_smartctl_summary(_json.dumps(payload))

        self.assertEqual(summary["last_test_lifetime_hours"], 43104)
        self.assertEqual(summary["last_test_age_hours"], 6)


class EnclosureSysfsAndDegradedAesTests(unittest.TestCase):
    SHARED_ADDRESS_AES = """
  EXAMPLE  SATAJBOD          0100
  Primary enclosure logical identifier (hex): 5eeeeeee00000084
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5eeeeeee00000084
          SAS address: 0x5eeeeeee00000084
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 1
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5eeeeeee00000084
          SAS address: 0x5eeeeeee00000084
      Element index: 2  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 2
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5eeeeeee00000084
          SAS address: 0x5aaaaaaa00000d05
      Element index: 3  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 3
        phy index: 0
          SAS device type: no SAS device attached
          attached SAS address: 0x5eeeeeee00000084
          SAS address: 0x5eeeeeee00000084
""".strip()

    @staticmethod
    def _build_bounded_aes_output(descriptor_count: int, *, dual_path: bool = False) -> str:
        lines = [
            "EXAMPLE  BOUNDEDAES  0100",
            "Additional element status diagnostic page:",
            "  additional element status descriptor list",
            "    Element type: Array device slot, subenclosure id: 0 [ti=0]",
        ]
        for element_id in range(descriptor_count):
            lines.append(f"      Element index: {element_id}  eiioe=0")
            if dual_path or element_id == 0:
                slot_number = element_id // 2 if dual_path else 0
                lines.append(f"        device slot number: {slot_number}")
            else:
                lines.append("        flagged as invalid (no further information)")
        return "\n".join(lines)

    def test_canonicalize_enclosure_sysfs_map_command(self) -> None:
        command = (
            "for c in /sys/class/enclosure/*/*; do "
            '[ -f "$c/slot" ] || continue; printf x; done'
        )
        self.assertEqual(canonicalize_ssh_command(command), "enclosure sysfs map")

    def test_parse_enclosure_sysfs_map_reads_slots_and_devices(self) -> None:
        output = "\n".join(
            [
                "13:0:0:0|sg84 |0|0|sdaa",
                "13:0:0:0|sg84 |1|1|sdab sdbb",
                "13:0:0:0|sg84 |5|5|",
                "2:0:0:0|sg2 |-1|7|sdy",
                "1:0:0:0|sg1 |-1|SLOT 01|sdx",
                "not a mapping line",
                "3:0:0:0|ses0 |0|0|sdz",
            ]
        )

        mapping = parse_enclosure_sysfs_map(output)

        self.assertEqual(mapping["sg84"][0], ["sdaa"])
        # Multipath shelves expose one component per path device.
        self.assertEqual(mapping["sg84"][1], ["sdab", "sdbb"])
        # Empty bays carry no bound block device and must not create entries.
        self.assertNotIn(5, mapping["sg84"])
        # A missing slot attribute falls back to numeric component names only.
        self.assertEqual(mapping["sg2"][7], ["sdy"])
        self.assertNotIn("sg1", mapping)
        # Only sg nodes join back to sg_ses evidence.
        self.assertNotIn("ses0", mapping)

    def test_parse_ssh_outputs_disables_shared_aes_addresses_and_uses_sysfs_slots(self) -> None:
        sysfs_output = "\n".join(
            [
                "13:0:0:0|sg84 |0|0|sdaa",
                "13:0:0:0|sg84 |1|1|sdab",
                "13:0:0:0|sg84 |2|2|sdac",
            ]
        )

        parsed = parse_ssh_outputs(
            {
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg84": self.SHARED_ADDRESS_AES,
                "for c in /sys/class/enclosure/*/*; do printf x; done": sysfs_output,
            },
            4,
            None,
            None,
        )

        candidates = parsed.ses_slot_candidates
        # Shared expander addresses cannot identify a bay, so the hint is
        # demoted to display-only evidence instead of a match key.
        self.assertIsNone(candidates[0].get("sas_address_hint"))
        self.assertTrue(candidates[0]["sas_address_degraded"])
        self.assertEqual(candidates[0]["shared_sas_address"], "5eeeeeee00000084")
        self.assertIsNone(candidates[1].get("sas_address_hint"))
        # The unique per-drive address keeps working as a match key.
        self.assertEqual(candidates[2]["sas_address_hint"], "5aaaaaaa00000d05")
        self.assertFalse(candidates[2]["sas_address_degraded"])
        # The kernel enclosure-driver bindings provide the per-bay device hints.
        self.assertEqual(candidates[0]["device_names"], ["sdaa"])
        self.assertEqual(candidates[1]["device_names"], ["sdab"])
        self.assertEqual(parsed.ses_slot_to_device[0], "sdaa")
        # A bay with no attached device must not read as populated just
        # because the expander stamped its own address into the descriptor.
        self.assertFalse(candidates[3]["present"])
        self.assertTrue(
            any("shared SAS address" in warning for warning in parsed.ses_selected_meta.get("warnings") or [])
        )

    def test_parse_ssh_outputs_keeps_sysfs_bindings_from_every_merged_ses_path(self) -> None:
        parsed = parse_ssh_outputs(
            {
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg84": self.SHARED_ADDRESS_AES,
                "sudo -n /usr/bin/sg_ses -p aes /dev/sg85": self.SHARED_ADDRESS_AES,
                "for c in /sys/class/enclosure/*/*; do printf x; done": (
                    "13:0:0:0|sg85 |1|1|sdz"
                ),
            },
            4,
            None,
            None,
        )

        self.assertEqual(len(parsed.ses_enclosures), 1)
        self.assertEqual(parsed.ses_slot_candidates[1]["device_names"], ["sdz"])
        self.assertEqual(parsed.ses_slot_to_device[1], "sdz")

    def test_parse_ssh_outputs_keeps_unique_aes_addresses_unflagged(self) -> None:
        output = """
  EXAMPLE  SASJBOD           0100
  Primary enclosure logical identifier (hex): 5eeeeeee00000024
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 0
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5eeeeeee00000024
          SAS address: 0x5aaaaaaa00000a01
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 0, device slot number: 1
        phy index: 0
          SAS device type: end device
          attached SAS address: 0x5eeeeeee00000024
          SAS address: 0x5aaaaaaa00000a02
""".strip()

        parsed = parse_ssh_outputs(
            {"sudo -n /usr/bin/sg_ses -p aes /dev/sg26": output},
            2,
            None,
            None,
        )

        candidates = parsed.ses_slot_candidates
        self.assertEqual(candidates[0]["sas_address_hint"], "5aaaaaaa00000a01")
        self.assertEqual(candidates[1]["sas_address_hint"], "5aaaaaaa00000a02")
        self.assertFalse(candidates[0]["sas_address_degraded"])
        self.assertIsNone(candidates[0].get("shared_sas_address"))
        self.assertEqual(parsed.ses_selected_meta.get("warnings"), [])

    def test_ec_status_condition_codes_leave_presence_undecided(self) -> None:
        output = """
  EXAMPLE  SATAJBOD          0100
Enclosure Status diagnostic page:
  status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element 0 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Critical
      Element 1 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Noncritical
      Element 2 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: Not installed
      Element 3 descriptor:
        Predicted failure=0, Disabled=0, Swap=0, status: OK
""".strip()

        parsed = parse_sg_ses_enclosure_status(output, "sg_ses ec /dev/sg84")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        # Issue #119's shelf latches Critical onto every EMPTY bay and
        # Noncritical onto every populated one — condition codes cannot decide
        # occupancy by themselves.
        self.assertIsNone(parsed.slots[0].present)
        self.assertIsNone(parsed.slots[1].present)
        self.assertIs(parsed.slots[2].present, False)
        self.assertIs(parsed.slots[3].present, True)

    def test_aes_invalid_descriptor_keeps_bay_in_geometry_as_empty(self) -> None:
        output = """
  EXAMPLE  SATAJBOD          0100
  Primary enclosure logical identifier (hex): 5eeeeeee00000084
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 0
        phy index: 0
          SAS device type: no SAS device attached
          target port for: SATA_device
          attached SAS address: 0x5eeeeeee00000001
          SAS address: 0x5eeeeeee00000002
      Element index: 1  eiioe=0
        flagged as invalid (no further information)
      Element index: 2  eiioe=0
        flagged as invalid (no further information)
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg84")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        # Invalid descriptors (issue #119's empty bays) must not vanish from
        # the shelf geometry: the valid descriptor proves a zero offset, so
        # the empty elements safely retain matching bay numbers with
        # low-strength provenance.
        self.assertEqual(sorted(parsed.slots), [0, 1, 2])
        self.assertIs(parsed.slots[1].present, False)
        self.assertEqual(parsed.slots[1].slot_number_source, "ses_element_index_invalid_descriptor")
        self.assertIn("flagged invalid", parsed.slots[1].slot_number_warning or "")
        # A stronger reported slot number must still win on merge.
        self.assertIs(parsed.slots[0].present, True)
        self.assertEqual(parsed.slots[0].slot_number_source, "ses_device_slot_number")

    def test_aes_invalid_descriptor_stays_unmapped_without_consistent_offset(self) -> None:
        output = """
  EXAMPLE  AMBIGUOUSOFFSET    0100
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        device slot number: 1
      Element index: 1  eiioe=0
        device slot number: 3
      Element index: 2  eiioe=0
        flagged as invalid (no further information)
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg8")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(sorted(parsed.slots), [1, 3])
        self.assertEqual(len(parsed.unmapped_slots), 1)
        unmapped = parsed.unmapped_slots[0]
        self.assertEqual(unmapped.element_id, 2)
        self.assertFalse(unmapped.present)
        self.assertEqual(
            unmapped.control_targets,
            [{"ses_device": "/dev/sg8", "ses_element_id": 2, "ses_slot_number": None}],
        )
        self.assertIn("no consistent element-to-device-slot offset", unmapped.slot_number_warning or "")

    def test_aes_descriptor_cap_accepts_two_paths_for_every_supported_slot(self) -> None:
        descriptor_cap = 2 * 4096

        parsed = parse_sg_ses_aes(
            self._build_bounded_aes_output(descriptor_cap, dual_path=True),
            "sg_ses aes /dev/sg8",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(list(parsed.slots), list(range(4096)))

    def test_aes_descriptor_cap_rejects_the_whole_oversized_page(self) -> None:
        descriptor_cap = 2 * 4096

        parsed = parse_sg_ses_aes(
            self._build_bounded_aes_output(descriptor_cap + 1),
            "sg_ses aes /dev/sg8",
        )

        self.assertIsNone(parsed)

    def test_aes_output_cap_accepts_the_exact_boundary(self) -> None:
        output_cap = 4 * 1024 * 1024
        output = self._build_bounded_aes_output(1)
        output = output.ljust(output_cap)

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg8")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(list(parsed.slots), [0])

    def test_aes_output_cap_rejects_the_whole_oversized_page(self) -> None:
        output_cap = 4 * 1024 * 1024
        output = self._build_bounded_aes_output(1)
        output = output.ljust(output_cap + 1)

        self.assertIsNone(parse_sg_ses_aes(output, "sg_ses aes /dev/sg8"))

    def test_aes_invalid_descriptor_translation_has_linear_membership_work(self) -> None:
        class ComparisonCountingInt(int):
            comparisons = 0
            __hash__ = int.__hash__

            def __eq__(self, other: object) -> bool:
                type(self).comparisons += 1
                return super().__eq__(other)

            def __add__(self, other: int) -> "ComparisonCountingInt":
                return type(self)(int(self) + other)

        descriptor_count = 128
        invalid_descriptors = [
            SESMapSlot(slot_number=-1, element_id=ComparisonCountingInt(element_id))
            for element_id in range(1, descriptor_count + 1)
        ]
        enclosure = SESMapEnclosure(ses_device="/dev/sg8")

        parsers._finalize_ses_invalid_descriptor_evidence(
            enclosure,
            invalid_descriptors,
            [SESMapSlot(slot_number=0, element_id=0, reported_slot_number=0)],
        )

        comparisons = ComparisonCountingInt.comparisons
        self.assertLessEqual(comparisons, descriptor_count * 4)
        self.assertEqual(list(enclosure.slots), list(range(1, descriptor_count + 1)))


class ParserConsolidationTests(unittest.TestCase):
    def test_smart_summary_finalizer_preserves_order_and_messages(self) -> None:
        unavailable = parsers._finalize_summary(
            {"temperature_c": None, "namespace_nguid": None},
            "No fields.",
        )
        available = parsers._finalize_summary(
            {"temperature_c": 0, "namespace_nguid": None},
            "No fields.",
        )

        self.assertEqual(
            list(unavailable),
            ["available", "temperature_c", "namespace_nguid", "message"],
        )
        self.assertEqual(unavailable["message"], "No fields.")
        self.assertFalse(unavailable["available"])
        self.assertIsNone(available["message"])
        self.assertTrue(available["available"])

    def test_five_smart_summary_parsers_use_shared_finalizer(self) -> None:
        for parser in (
            parsers.parse_smartctl_summary,
            parsers.parse_nvme_smart_log_summary,
            parsers.parse_nvme_id_ctrl_summary,
            parsers.parse_nvme_id_ns_summary,
            parsers.parse_esxcli_smart_get,
        ):
            with self.subTest(parser=parser.__name__):
                self.assertIn("_finalize_summary(", inspect.getsource(parser))

    def test_smartctl_named_extractors_preserve_waterfall_test_and_phy_values(self) -> None:
        payload = {
            "logical_block_size": 512,
            "power_on_time": {"hours": 1000},
            "ata_smart_attributes": {
                "table": [
                    {"id": 232, "raw": {"value": 90}},
                    {"id": 241, "raw": {"value": 10}},
                    {"id": 242, "raw": {"value": 20}},
                ]
            },
            "ata_smart_self_test_log": {
                "standard": {
                    "table": [
                        {
                            "type": {"string": "Short offline"},
                            "status": {"string": "Completed without error"},
                            "lifetime_hours": 990,
                        }
                    ]
                }
            },
            "scsi_sas_port_0": {
                "phy_0": {
                    "attached_device_type": "no device attached",
                    "sas_address": "0x5000000000000001",
                    "attached_sas_address": "0x0",
                },
                "phy_1": {
                    "attached_device_type": "expander device",
                    "sas_address": "0x5000000000000002",
                    "attached_sas_address": "0x5000000000000003",
                    "negotiated_logical_link_rate": "12 Gbps",
                },
            },
        }

        traffic = parsers._extract_smartctl_traffic_and_wear(payload, 1000, 512)
        latest_test = parsers._extract_smartctl_latest_test(payload)
        sas_phy = parsers._extract_smartctl_sas_phy(payload)

        self.assertEqual(traffic["available_spare_percent"], 90)
        self.assertEqual(traffic["bytes_read"], 10240)
        self.assertEqual(traffic["bytes_written"], 5120)
        self.assertEqual(traffic["estimated_remaining_bytes_written"], None)
        self.assertEqual(
            latest_test,
            ("Short offline", "Completed without error", 990),
        )
        self.assertEqual(
            sas_phy,
            ("0x5000000000000002", "0x5000000000000003", "12 Gbps"),
        )

    def test_ata_iterators_and_enabled_toggle_share_normalization(self) -> None:
        payload = {
            "ata_smart_attributes": {
                "table": [
                    {"id": 12, "raw": {"value": 7}},
                    "ignored",
                ]
            },
            "ata_device_statistics": {
                "pages": [
                    {"table": [{"name": "Number of Read Commands", "value": 11}]},
                    {"table": "ignored"},
                ]
            },
        }

        self.assertEqual(
            list(parsers._iter_ata_attribute_entries(payload)),
            [{"id": 12, "raw": {"value": 7}}],
        )
        self.assertEqual(
            list(parsers._iter_ata_device_stat_entries(payload)),
            [{"name": "Number of Read Commands", "value": 11}],
        )
        self.assertIs(parsers._parse_enabled_disabled(" Enabled, supported "), True)
        self.assertIs(parsers._parse_enabled_disabled("DISABLED"), False)
        self.assertIsNone(parsers._parse_enabled_disabled("unknown"))
        enrichment = parsers.parse_smartctl_text_enrichment(
            "Read Cache is: Enabled\n"
            "Rd look-ahead is: unknown\n"
            "Writeback Cache is: Enabled\n"
            "Write cache is: unknown"
        )
        self.assertIs(enrichment["read_cache_enabled"], True)
        self.assertIs(enrichment["writeback_cache_enabled"], True)

    def test_sg_ses_common_field_table_and_helper_cover_only_five_prefixes(self) -> None:
        self.assertEqual(
            parsers.SG_SES_COMMON_FIELD_PREFIXES,
            {
                "Primary enclosure logical identifier": ("enclosure_id", ":", "hex_text"),
                "Transport protocol:": ("transport_protocol", ":", "text"),
                "attached SAS address:": ("attached_sas_address", ":", "hex"),
                "target port for:": ("target_port_protocol", ":", "text"),
                "phy identifier:": ("phy_identifier", ":", "text"),
            },
        )
        self.assertEqual(
            parsers._parse_sg_ses_common_field("attached SAS address: 0x0000ABCD"),
            ("attached_sas_address", "abcd"),
        )
        self.assertEqual(
            parsers._parse_sg_ses_common_field(
                "Primary enclosure logical identifier (hex): 5000000000000001"
            ),
            ("enclosure_id", "5000000000000001"),
        )
        self.assertIsNone(parsers._parse_sg_ses_common_field("SAS address: 0x1234"))
        self.assertIsNone(parsers._parse_sg_ses_common_field("SAS device type: end device"))

    def test_ec_and_join_share_status_line_helper(self) -> None:
        for parser in (
            parsers.parse_sg_ses_enclosure_status,
            parsers.parse_sg_ses_join_filter,
        ):
            with self.subTest(parser=parser.__name__):
                source = inspect.getsource(parser)
                self.assertIn("_apply_sg_ses_status_line(", source)
                self.assertNotIn("slot.status =", source)

    def test_aes_and_join_use_common_field_parser_but_keep_assignment_policies(self) -> None:
        for parser in (parsers.parse_sg_ses_aes, parsers.parse_sg_ses_join_filter):
            with self.subTest(parser=parser.__name__):
                self.assertIn("_parse_sg_ses_common_field(", inspect.getsource(parser))

        aes = parsers.parse_sg_ses_aes(
            """ExampleCo Shelf 0001
Element type: Array device slot
Element index: 0
Transport protocol: SAS
Transport protocol: ATA
number of phys: 1, device slot number: 0
target port for: SSP
target port for: SATA_device
attached SAS address: 0x5000000000000001
attached SAS address: 0x5000000000000002
phy identifier: 0x0
phy identifier: 0x1""",
            "sg_ses aes /dev/sg7",
        )
        joined = parsers.parse_sg_ses_join_filter(
            """ExampleCo Shelf 0001
Slot00 [0,0] Element type: Array device slot
Transport protocol: SAS
Transport protocol: ATA
number of phys: 1, device slot number: 0
target port for: SSP
target port for: SATA_device
attached SAS address: 0x5000000000000001
attached SAS address: 0x5000000000000002
phy identifier: 0x0
phy identifier: 0x1""",
            "sg_ses join /dev/sg7",
        )

        self.assertIsNotNone(aes)
        self.assertIsNotNone(joined)
        assert aes is not None and joined is not None
        self.assertEqual(aes.slots[0].transport_protocol, "ATA")
        self.assertEqual(aes.slots[0].target_port_protocol, "SSP")
        self.assertEqual(aes.slots[0].attached_sas_address, "5000000000000001")
        self.assertEqual(aes.slots[0].phy_identifier, "0x0")
        self.assertEqual(joined.slots[0].transport_protocol, "ATA")
        self.assertEqual(joined.slots[0].target_port_protocol, "SATA_device")
        self.assertEqual(joined.slots[0].attached_sas_address, "5000000000000002")
        self.assertEqual(joined.slots[0].phy_identifier, "0x1")

    def test_simple_ssh_command_tables_cover_all_pure_prefix_rows(self) -> None:
        self.assertEqual(
            parsers.SIMPLE_SSH_COMMAND_PREFIXES,
            {
                ("glabel", ("status",)): "glabel status",
                ("gmultipath", ("list",)): "gmultipath list",
                ("sesutil", ("map",)): "sesutil map",
                ("sesutil", ("show",)): "sesutil show",
                ("mdadm", ("--detail", "--scan")): "mdadm --detail --scan",
                ("ubntstorage", ("disk", "inspect")): "ubntstorage disk inspect",
                ("ubntstorage", ("space", "inspect")): "ubntstorage space inspect",
            },
        )
        self.assertEqual(
            parsers.CASEFOLDED_SSH_COMMAND_PREFIXES,
            {
                ("esxcli", ("storage", "core", "adapter", "list")): "esxcli storage core adapter list",
                ("esxcli", ("storage", "core", "device", "list")): "esxcli storage core device list",
                ("esxcli", ("storage", "core", "path", "list")): "esxcli storage core path list",
                ("esxcli", ("storage", "filesystem", "list")): "esxcli storage filesystem list",
                ("esxcli", ("storage", "vmfs", "extent", "list")): "esxcli storage vmfs extent list",
                ("esxcli", ("storage", "san", "sas", "list")): "esxcli storage san sas list",
                ("esxcli", ("hardware", "pci", "pcipassthru", "list")): "esxcli hardware pci pcipassthru list",
            },
        )
        self.assertEqual(parsers.SIMPLE_SSH_EXECUTABLES, {"lspci": "lspci"})
        self.assertEqual(
            parsers.TRAILING_CASEFOLDED_SSH_COMMAND_PREFIXES,
            {
                ("nvme", ("list-subsys", "-o", "json")): "nvme list-subsys -o json",
                ("nvme", ("list", "-o", "json")): "nvme list -o json",
            },
        )

    def test_canonicalize_all_simple_command_rows(self) -> None:
        cases = {
            "/sbin/glabel status -s": "glabel status",
            "/sbin/gmultipath list verbose": "gmultipath list",
            "sudo -n /usr/sbin/sesutil map extra": "sesutil map",
            "/usr/sbin/sesutil show extra": "sesutil show",
            "/sbin/mdadm --detail --scan --verbose": "mdadm --detail --scan",
            "/usr/sbin/ubntstorage disk inspect --json": "ubntstorage disk inspect",
            "/usr/sbin/ubntstorage space inspect --json": "ubntstorage space inspect",
            "esxcli STORAGE CORE ADAPTER LIST --formatter csv": "esxcli storage core adapter list",
            "esxcli storage core device list --device naa.example": "esxcli storage core device list",
            "esxcli storage core path list --device naa.example": "esxcli storage core path list",
            "esxcli storage filesystem list --formatter csv": "esxcli storage filesystem list",
            "esxcli storage vmfs extent list --formatter csv": "esxcli storage vmfs extent list",
            "esxcli storage san sas list --formatter csv": "esxcli storage san sas list",
            "esxcli hardware pci pcipassthru list --formatter csv": "esxcli hardware pci pcipassthru list",
            "/usr/bin/lspci -nn": "lspci",
            "/sbin/nvme list-subsys -o JSON ignored": "nvme list-subsys -o json",
            "/sbin/nvme list -o JsOn ignored": "nvme list -o json",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(canonicalize_ssh_command(command), expected)

    def test_ssh_output_dispatch_table_covers_only_one_to_one_assignments(self) -> None:
        self.assertEqual(
            {
                command: (attribute, parser.__name__)
                for command, (attribute, parser) in parsers.SSH_OUTPUT_PARSERS.items()
            },
            {
                "glabel status": ("glabel", "parse_glabel_status"),
                "zpool status -gP": ("zpool_members", "parse_zpool_status"),
                "lsblk -OJ": ("linux_blockdevices", "parse_lsblk_json"),
                "mdadm --detail --scan": ("linux_mdadm_arrays", "parse_mdadm_detail_scan"),
                "nvme list-subsys -o json": ("linux_nvme_subsystems", "parse_nvme_list_subsys_json"),
                "ubntstorage disk inspect": ("ubntstorage_disks", "parse_ubntstorage_json"),
                "ubntstorage space inspect": ("ubntstorage_spaces", "parse_ubntstorage_json"),
                "gpio debug": ("unifi_led_states", "parse_unifi_gpio_debug"),
                "esxcli storage core adapter list": ("esxi_storage_adapters", "parse_esxcli_table"),
                "esxcli storage core device list": ("esxi_storage_devices", "parse_esxcli_key_value_sections"),
                "esxcli storage core path list": ("esxi_storage_paths", "parse_esxcli_key_value_sections"),
                "esxcli storage filesystem list": ("esxi_filesystems", "parse_esxcli_table"),
                "esxcli storage vmfs extent list": ("esxi_vmfs_extents", "parse_esxcli_table"),
                "esxcli storage san sas list": ("esxi_sas_adapters", "parse_esxcli_key_value_sections"),
                "gmultipath list": ("multipath_info", "parse_gmultipath_list"),
            },
        )

    def test_parse_ssh_outputs_uses_dispatch_table_and_shared_camcontrol_loop(self) -> None:
        with patch.dict(
            parsers.SSH_OUTPUT_PARSERS,
            {"synthetic parser": ("linux_blockdevices", lambda output: [{"raw": output}])},
        ):
            parsed = parse_ssh_outputs(
                {
                    "synthetic parser": "table row",
                    "camcontrol devlist": "scbus0 on mpr0 bus 0:\n<A First R1> at scbus0 target 0 lun 0 (da0)",
                    "camcontrol devlist -v": "scbus1 on mpr1 bus 0:\n<B Second R2> at scbus1 target 1 lun 0 (da1)",
                },
                slot_count=0,
                enclosure_filter=None,
            )

        self.assertEqual(parsed.linux_blockdevices, [{"raw": "table row"}])
        self.assertEqual(parsed.camcontrol_models, {"da1": "B Second R2"})
        source = inspect.getsource(parsers.parse_ssh_outputs)
        self.assertIn("SSH_OUTPUT_PARSERS.items()", source)
        self.assertEqual(source.count("parse_camcontrol_devlist("), 1)

    def test_control_target_helper_preserves_strict_and_lax_policies(self) -> None:
        base = [
            {
                "ssh_host": " host-a ",
                "ses_device": "/dev/sg1",
                "ses_element_id": 4,
                "ses_slot_number": 5,
            },
            {
                "ssh_host": "host-b",
                "ses_device": None,
                "ses_element_id": "raw-element",
                "ses_slot_number": "raw-slot",
            },
        ]
        overlay = [
            {
                "ssh_host": "host-a",
                "ses_device": "/dev/sg1",
                "ses_element_id": 4,
                "ses_slot_number": 99,
            }
        ]

        self.assertEqual(
            parsers._merge_control_targets(base, overlay, strict=True),
            [
                {
                    "ses_device": "/dev/sg1",
                    "ses_element_id": 4,
                    "ses_slot_number": 5,
                    "ssh_host": "host-a",
                }
            ],
        )
        self.assertEqual(
            parsers._merge_control_targets(base, overlay, strict=False),
            [
                {
                    "ses_device": "/dev/sg1",
                    "ses_element_id": 4,
                    "ses_slot_number": 5,
                    "ssh_host": "host-a",
                },
                {
                    "ses_device": None,
                    "ses_element_id": "raw-element",
                    "ses_slot_number": None,
                    "ssh_host": "host-b",
                },
            ],
        )

    def test_candidate_map_uses_lax_control_target_policy(self) -> None:
        merged = merge_slot_candidate_maps(
            {},
            {
                0: {
                    "ses_targets": [
                        {
                            "ssh_host": "host-b",
                            "ses_device": None,
                            "ses_element_id": "raw-element",
                            "ses_slot_number": "raw-slot",
                        }
                    ]
                }
            },
        )

        self.assertEqual(
            merged[0]["ses_targets"],
            [
                {
                    "ses_device": None,
                    "ses_element_id": "raw-element",
                    "ses_slot_number": None,
                    "ssh_host": "host-b",
                }
            ],
        )
        source = inspect.getsource(parsers.merge_slot_candidate_maps)
        self.assertIn("_merge_control_targets(", source)
        self.assertIn("strict=False", source)


class DellMd1280ProfileInferenceTests(unittest.TestCase):
    def test_en8435_enclosure_name_infers_md1280_drawer_profile(self) -> None:
        output = """
  DELL      EN-8435A-E6EBD    3535
  Primary enclosure logical identifier (hex): 5eeeeeee00000084
Additional element status diagnostic page:
  additional element status descriptor list
    Element type: Array device slot, subenclosure id: 0 [ti=0]
      Element index: 0  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 0
        phy index: 0
          SAS device type: no SAS device attached
          target port for: SATA_device
          attached SAS address: 0x5eeeeeee00000001
          SAS address: 0x5eeeeeee00000002
      Element index: 1  eiioe=0
        Transport protocol: SAS
        number of phys: 1, not all phys: 1, device slot number: 1
        phy index: 0
          SAS device type: no SAS device attached
          target port for: SATA_device
          attached SAS address: 0x5eeeeeee00000001
          SAS address: 0x5eeeeeee00000003
""".strip()

        parsed = parse_sg_ses_aes(output, "sg_ses aes /dev/sg1")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        from app.services.profile_registry import DELL_MD1280_PROFILE_ID

        self.assertEqual(parsed.profile_id, DELL_MD1280_PROFILE_ID)
        self.assertEqual(parsed.enclosure_label, "Dell MD1280 84 Bay")
        self.assertEqual(parsed.layout_rows, 6)
        self.assertEqual(parsed.layout_columns, 14)
        assert parsed.slot_layout is not None
        self.assertEqual(len(parsed.slot_layout), 6)
        # Stacked drawers per the physical chassis (deployment manual Figure
        # 6): bays 1-42 are the top drawer band and 43-84 the bottom band,
        # each back row first with the front row at the drawer-pull edge.
        self.assertEqual(parsed.slot_layout[0], list(range(28, 42)))
        self.assertEqual(parsed.slot_layout[2], list(range(0, 14)))
        self.assertEqual(parsed.slot_layout[3], list(range(70, 84)))
        self.assertEqual(parsed.slot_layout[5], list(range(42, 56)))
        flattened = sorted(slot for row in parsed.slot_layout for slot in row)
        self.assertEqual(flattened, list(range(84)))
