import unittest

from app.services.parsers import (
    canonicalize_ssh_command,
    parse_camcontrol_devlist,
    parse_gmultipath_list,
    parse_sg_ses_aes,
    parse_smart_test_results,
    parse_smartctl_summary,
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

    def test_canonicalize_sg_ses_command_preserves_target_device(self) -> None:
        command = "sudo -n /usr/bin/sg_ses -p aes /dev/sg27"

        self.assertEqual(canonicalize_ssh_command(command), "sg_ses aes /dev/sg27")

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
        self.assertEqual(parsed.layout_rows, 4)
        self.assertEqual(parsed.layout_columns, 6)
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
        self.assertEqual(parsed.slots[2].sas_address, "0")
        self.assertFalse(parsed.slots[2].present)

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

    def test_parse_smartctl_summary_handles_invalid_json(self) -> None:
        parsed = parse_smartctl_summary("not-json")

        self.assertFalse(parsed["available"])
        self.assertEqual(parsed["message"], "SMART JSON parsing failed.")


if __name__ == "__main__":
    unittest.main()
