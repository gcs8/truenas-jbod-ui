import unittest

from app.services.parsers import (
    canonicalize_ssh_command,
    parse_camcontrol_devlist,
    parse_gmultipath_list,
    parse_lsblk_json,
    parse_mdadm_detail_scan,
    parse_nvme_id_ctrl_summary,
    parse_nvme_id_ns_summary,
    parse_nvme_list_subsys_json,
    parse_nvme_smart_log_summary,
    parse_ssh_outputs,
    parse_sesutil_show_enclosures,
    parse_ubntstorage_json,
    parse_sg_ses_aes,
    parse_sg_ses_enclosure_status,
    parse_smart_test_results,
    parse_smartctl_text_enrichment,
    parse_smartctl_summary,
    parse_unifi_gpio_debug,
)


class ParserTests(unittest.TestCase):
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

    def test_canonicalize_sg_ses_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses -p aes /dev/sg27"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses aes /dev/sg27")

    def test_canonicalize_sg_ses_ec_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses -p ec /dev/sg38"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses ec /dev/sg38")

    def test_canonicalize_linux_inventory_commands(self) -> None:
        self.assertEqual(canonicalize_ssh_command("/usr/bin/lsblk -OJ"), "lsblk -OJ")
        self.assertEqual(canonicalize_ssh_command("sudo -n /usr/sbin/mdadm --detail --scan"), "mdadm --detail --scan")
        self.assertEqual(canonicalize_ssh_command("/usr/bin/nvme list-subsys -o json"), "nvme list-subsys -o json")
        self.assertEqual(canonicalize_ssh_command("/usr/sbin/ubntstorage disk inspect"), "ubntstorage disk inspect")
        self.assertEqual(canonicalize_ssh_command("/usr/sbin/ubntstorage space inspect"), "ubntstorage space inspect")
        self.assertEqual(canonicalize_ssh_command("cat /sys/kernel/debug/gpio"), "gpio debug")

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
        self.assertIsNone(parsed["annualized_bytes_written"])

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
        self.assertEqual(parsed["annualized_bytes_written"], 19831300795214)

    def test_parse_smartctl_text_enrichment_extracts_ata_cache_health_and_link_metadata(self) -> None:
        output = """
=== START OF INFORMATION SECTION ===
SMART overall-health self-assessment test result: PASSED
SATA Version is: SATA 3.1, 6.0 Gb/s (current: 6.0 Gb/s)
Rd look-ahead is: Enabled
Write cache is: Enabled
""".strip()

        parsed = parse_smartctl_text_enrichment(output)

        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["smart_health_status"], "PASSED")
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


if __name__ == "__main__":
    unittest.main()
