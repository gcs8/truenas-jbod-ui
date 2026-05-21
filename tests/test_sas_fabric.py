import tempfile
import unittest
from pathlib import Path

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import (
    InventorySnapshot,
    MultipathMember,
    MultipathView,
    SasFabricAlias,
    SourceStatus,
    SlotState,
    SlotView,
)
from app.services.inventory import InventoryService
from app.services.parsers import canonicalize_ssh_command
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.sas_fabric import (
    CORE_DMIDECODE_SLOT_COMMAND,
    CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND,
    CORE_MESSAGES_TAIL_SUDO_COMMAND,
    CORE_MPR_DMESG_EVENTS_COMMAND,
    CORE_MPR_SYSCTL_LOCATION_COMMAND,
    CORE_PCICONF_LV_COMMAND,
    CORE_PCICONF_LV_OPTIONAL_COMMAND,
    build_core_mprutil_unit_commands,
    build_sas_fabric_snapshot,
    discover_mpr_units_from_adapter_summary,
    parse_dmidecode_slots,
    parse_mpr_adapter_summary,
    parse_mpr_devices,
    parse_mpr_dmesg_events,
    parse_mpr_expanders,
    parse_mpr_sysctl_locations,
    parse_pciconf_sas_controllers,
)
from app.services.ssh_probe import SSHCommandResult


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sas_fabric"


MPR_ADAPTERS = """
Adapter     Chip           Board Name        Firmware
/dev/mpr0   SAS3516        Broadcom 9305-16e 16.00.12.00
/dev/mpr1   SAS3516        Broadcom 9305-16e 16.00.12.00
""".strip()


MPR0_ADAPTER = """
mpr0 Adapter:
  Board Name: Broadcom 9305-16e A
  Firmware Revision: 16.00.12.00
  Temperature: 43 C
0 0001 000a N 12.0 1.5 12.0 End Device
1 0001 000b N 12.0 1.5 12.0 End Device
""".strip()


MPR1_ADAPTER = """
mpr1 Adapter:
  Board Name: Broadcom 9305-16e B
  Firmware Revision: 16.00.12.00
  Temperature: 41 C
0 0001 000c N 12.0 1.5 12.0 End Device
""".strip()


MPR0_EXPANDERS = """
Num Phys SAS Address      DevHandle Parent EncHandle SAS Level
36       50030480090c4f7f 0009      0001   0002      1
  0 1 000a 12.0 1.5 12.0 End Device
  1 2 000b 12.0 1.5 12.0 No Device
""".strip()


MPR1_EXPANDERS = """
Num Phys SAS Address      DevHandle Parent EncHandle SAS Level
36       50030480090c8f7f 0019      0001   0012      1
  0 1 000c 12.0 1.5 12.0 End Device
""".strip()


MPR0_ENCLOSURES = """
Slots Logical ID       SEPHandle EncHandle Type
36    50030480090c4f7f 0008      0002     SES
""".strip()


MPR1_ENCLOSURES = """
Slots Logical ID       SEPHandle EncHandle Type
36    50030480090c4f7f 0018      0012     SES
""".strip()


MPR0_DEVICES = """
5000c50000000001 000a 0002 SAS Target 12.0 0002 0
""".strip()


MPR1_DEVICES = """
5000c50000000001 000c 0012 SAS Target 12.0 0012 0
""".strip()


MPR0_DEVICES_WITH_BUS_TARGET = """
B____T    SAS Address      Handle  Parent    Device        Speed Enc  Slot  Wdt
00   180  5000c50000000001 000a    0002      SAS Target    12    0002 0     1
""".strip()


MPR_DMESG_EVENTS = """
mpr0: Controller reported scsi ioc terminated tgt 180 SMID 142 loginfo 31120302
(da60:mpr0:0:180:0): WRITE(16). CDB: 8a 00 00 00 00 01 75 5d d9 60 00 00 00 f8 00 00
(da60:mpr0:0:180:0): CAM status: CCB request completed with an error
(da60:mpr0:0:180:0): SCSI sense: ABORTED COMMAND asc:4b,4 (NAK received)
(da60:mpr0:0:180:0): Retrying command (per sense data)
""".strip()


DMIDECODE_SLOT_OUTPUT = """
# dmidecode 3.3
Handle 0x000C, DMI type 9, 17 bytes
System Slot Information
        Designation: CPU2 SLOT1 PCI-E 3.0 X8
        Type: x8 PCI Express 3 x8
        Current Usage: In Use
        Length: Short
        ID: 1
        Characteristics:
                3.3 V is provided
                PME signal is supported
        Bus Address: 0000:82:00.0

Handle 0x000D, DMI type 9, 17 bytes
System Slot Information
        Designation: CPU2 SLOT2 PCI-E 3.0 X8
        Type: x8 PCI Express 3 x8
        Current Usage: In Use
        Length: Short
        ID: 2
        Bus Address: 0000:83:00.0

Handle 0x0011, DMI type 9, 17 bytes
System Slot Information
        Designation: CPU2 SLOT6 PCI-E 3.0 X8
        Type: x8 PCI Express 3 x8
        Current Usage: Available
        Length: Short
        ID: 6
        Bus Address: 0000:ff:00.0
""".strip()


PCICONF_MPR_OUTPUT = """
mpr0@pci0:130:0:0:      class=0x010700 rev=0x01 hdr=0x00 vendor=0x1000 device=0x00ab subvendor=0x1000 subdevice=0x3040
    vendor     = 'Broadcom / LSI'
    device     = 'SAS3516 Fusion-MPT Tri-Mode RAID On Chip (ROC)'
    class      = mass storage
    subclass   = SAS
mpr1@pci0:131:0:0:      class=0x010700 rev=0x01 hdr=0x00 vendor=0x1000 device=0x00ab subvendor=0x1000 subdevice=0x3040
    vendor     = 'Broadcom / LSI'
    device     = 'SAS3516 Fusion-MPT Tri-Mode RAID On Chip (ROC)'
    class      = mass storage
    subclass   = SAS
""".strip()


MPR_SYSCTL_LOCATIONS = r"""
dev.mpr.0.%location: slot=0 function=0 dbsf=pci0:130:0:0 handle=\_SB_.PCI1.QR3A.H000
dev.mpr.1.%location: slot=0 function=0 dbsf=pci0:131:0:0 handle=\_SB_.PCI1.QR3C.H000
dev.mpr.0.%parent: pci15
dev.mpr.1.%parent: pci16
""".strip()


ARCHIVE_CORE_BAD_CABLE_DMESG = (FIXTURES_DIR / "archive_core_bad_cable_dmesg.txt").read_text()


MPR0_EXPANDERS_WITH_UNRELATED = """
Num Phys SAS Address      DevHandle Parent EncHandle SAS Level
36       50030480090c4f7f 0009      0001   0002      1
  0 1 000a 6.0 1.5 12.0 End Device
36       500304801f715f3f 000b      0001   0004      1
  0 1 000d 12.0 1.5 12.0 End Device
""".strip()


MPR1_EXPANDERS_WITH_UNRELATED = """
Num Phys SAS Address      DevHandle Parent EncHandle SAS Level
36       50030480090c4fff 0019      0001   0012      1
  0 1 000c 6.0 1.5 12.0 End Device
36       500304801f715fbf 001b      0001   0014      1
  0 1 001d 12.0 1.5 12.0 End Device
""".strip()


MPR0_ENCLOSURES_WITH_UNRELATED = """
Slots Logical ID       SEPHandle EncHandle Type
24    50030480090c4f7f 0008      0002     SES
60    500304801f715f3f 000a      0004     SES
""".strip()


MPR1_ENCLOSURES_WITH_UNRELATED = """
Slots Logical ID       SEPHandle EncHandle Type
24    50030480090c4f7f 0018      0012     SES
60    500304801f715f3f 001a      0014     SES
""".strip()


class SasFabricParserTests(unittest.TestCase):
    def test_canonicalize_mprutil_unit_command_keeps_unit_shape(self) -> None:
        command = "sudo -n /usr/sbin/mprutil -u 1 show expanders"

        self.assertEqual(canonicalize_ssh_command(command), "mprutil -u 1 show expanders")

    def test_build_core_mprutil_unit_commands_discovers_each_adapter(self) -> None:
        commands = build_core_mprutil_unit_commands(
            MPR_ADAPTERS,
            seen_commands={"mprutil -u 0 show adapter"},
        )

        self.assertNotIn("sudo -n /usr/sbin/mprutil -u 0 show adapter", commands)
        self.assertIn("sudo -n /usr/sbin/mprutil -u 0 show expanders", commands)
        self.assertIn("sudo -n /usr/sbin/mprutil -u 1 show adapter", commands)
        self.assertIn("sudo -n /usr/sbin/mprutil -u 1 show iocfacts", commands)

    def test_core_mpr_paths_allow_multi_digit_adapter_units(self) -> None:
        adapters = """
Adapter     Chip           Board Name        Firmware
/dev/mpr0   SAS3516        Broadcom 9305-16e 16.00.12.00
/dev/mpr10  SAS3816        Broadcom 9500-16e 28.00.00.00
""".strip()
        commands = build_core_mprutil_unit_commands(adapters)
        sysctl_locations = parse_mpr_sysctl_locations(
            "dev.mpr.10.%location: slot=0 function=0 dbsf=pci0:132:0:0 handle=\\_SB_.PCI1.QR3D.H000\n"
            "dev.mpr.10.%parent: pci17"
        )
        events = parse_mpr_dmesg_events(
            "mpr10: Controller reported scsi ioc terminated tgt 180 SMID 142 loginfo 31120302\n"
            "(da600:mpr10:0:180:0): CAM status: CCB request completed with an error"
        )

        self.assertEqual(discover_mpr_units_from_adapter_summary(adapters), [0, 10])
        self.assertIn("sudo -n /usr/sbin/mprutil -u 10 show adapter", commands)
        self.assertIn("sudo -n /usr/sbin/mprutil -u 10 show expanders", commands)
        self.assertEqual(
            canonicalize_ssh_command("sudo -n /usr/sbin/mprutil -u 10 show iocfacts"),
            "mprutil -u 10 show iocfacts",
        )
        self.assertEqual(sysctl_locations["mpr10"]["pci_location"], "pci0:132:0:0")
        self.assertEqual(sysctl_locations["mpr10"]["pci_parent"], "pci17")
        self.assertIn("mpr10", events["by_controller"])
        self.assertIn("mpr10:180", events["by_controller_target"])
        self.assertIn("da600", events["by_device"])

    def test_parse_mpr_adapter_and_expander_rows(self) -> None:
        adapters = parse_mpr_adapter_summary(MPR_ADAPTERS)
        expanders = parse_mpr_expanders(MPR0_EXPANDERS)

        self.assertEqual([row["unit"] for row in adapters], ["0", "1"])
        self.assertEqual(expanders[0]["sas_address"], "50030480090c4f7f")
        self.assertEqual(expanders[0]["linked_phys"], 1)
        self.assertEqual(expanders[0]["device_counts"]["End Device"], 1)

    def test_parse_pciconf_and_dmidecode_join_keys_for_supermicro_slots(self) -> None:
        controllers = parse_pciconf_sas_controllers(PCICONF_MPR_OUTPUT)
        slots = parse_dmidecode_slots(DMIDECODE_SLOT_OUTPUT)
        sysctl_locations = parse_mpr_sysctl_locations(MPR_SYSCTL_LOCATIONS)

        self.assertEqual(controllers[0]["controller"], "mpr0")
        self.assertEqual(controllers[0]["pci_location"], "pci0:130:0:0")
        self.assertEqual(controllers[0]["pci_address"], "0000:82:00.0")
        self.assertEqual(controllers[0]["device_name"], "SAS3516 Fusion-MPT Tri-Mode RAID On Chip (ROC)")
        self.assertEqual(slots[0]["designation"], "CPU2 SLOT1 PCI-E 3.0 X8")
        self.assertEqual(slots[0]["bus_address"], "0000:82:00.0")
        self.assertIn("PME signal is supported", slots[0]["characteristics"])
        self.assertEqual(sysctl_locations["mpr0"]["pci_location"], "pci0:130:0:0")
        self.assertEqual(sysctl_locations["mpr0"]["pci_address"], "0000:82:00.0")
        self.assertEqual(sysctl_locations["mpr0"]["pci_parent"], "pci15")
        self.assertEqual(sysctl_locations["mpr1"]["acpi_handle"], r"\_SB_.PCI1.QR3C.H000")

    def test_parse_mpr_devices_keeps_bus_target_when_reported(self) -> None:
        devices = parse_mpr_devices(MPR0_DEVICES_WITH_BUS_TARGET)

        self.assertEqual(devices[0]["bus"], "00")
        self.assertEqual(devices[0]["target"], "180")
        self.assertEqual(devices[0]["slot"], "0")

    def test_parse_mpr_dmesg_events_summarizes_controller_and_disk_faults(self) -> None:
        events = parse_mpr_dmesg_events(MPR_DMESG_EVENTS)

        controller = events["by_controller"]["mpr0"]
        device = events["by_device"]["da60"]
        target = events["by_controller_target"]["mpr0:180"]

        self.assertEqual(events["event_count"], 5)
        self.assertEqual(controller["ioc_terminated_count"], 1)
        self.assertEqual(controller["sense_counts"]["NAK received"], 1)
        self.assertEqual(device["error_count"], 2)
        self.assertEqual(device["retry_count"], 1)
        self.assertEqual(target["loginfo_counts"]["31120302"], 1)
        self.assertEqual(target["fault_family_counts"]["sas_transport"], 1)
        self.assertEqual(target["fault_family_counts"]["write_io"], 1)
        self.assertEqual(target["fault_family_counts"]["sas_protocol"], 1)
        self.assertEqual(target["operation_counts"]["WRITE(16)"], 1)
        self.assertEqual(target["primary_fault"]["family"], "sas_protocol")
        self.assertIn("NAK received", target["operator_summary"])
        ioc_event = next(event for event in target["recent_events"] if event["event_type"] == "ioc_terminated")
        self.assertNotIn("decoded", ioc_event)
        ioc_record = next(record for record in target["decoded_records"] if record["event_type"] == "ioc_terminated")
        self.assertEqual(ioc_record["family"], "sas_transport")
        self.assertIn("Wrong relative offset or frame length", ioc_record["label"])
        self.assertEqual(ioc_record["decode_confidence"], "vendor-reference-partial")
        self.assertEqual(ioc_record["decode_source"], "baruch_lsi_decode_loginfo")
        self.assertEqual(ioc_record["source_attribution"]["license"], "MIT")
        self.assertEqual(ioc_record["decoded"]["lsi_loginfo"]["code_symbol"], "PL_LOGINFO_CODE_ABORT")
        self.assertEqual(
            ioc_record["decoded"]["lsi_loginfo"]["sub_code_symbol"],
            "PL_LOGINFO_SUB_CODE_WRONG_REL_OFF_OR_FRAME_LENGTH",
        )
        cdb_record = next(record for record in target["decoded_records"] if record["event_type"] == "cdb")
        self.assertEqual(cdb_record["operation"], "WRITE(16)")
        self.assertEqual(cdb_record["decode_confidence"], "standard")
        self.assertEqual(cdb_record["decode_source"], "t10_scsi_operation_codes")
        self.assertEqual(cdb_record["source_attribution"]["url"], "https://www.t10.org/lists/op-num.htm")
        self.assertEqual(cdb_record["direction"], "write")
        self.assertEqual(cdb_record["lba"], 6264052064)
        self.assertEqual(cdb_record["transfer_blocks"], 248)
        sense_record = next(record for record in target["decoded_records"] if record["event_type"] == "scsi_sense")
        self.assertEqual(sense_record["family"], "sas_protocol")
        self.assertEqual(sense_record["decode_confidence"], "standard")
        self.assertEqual(sense_record["decode_source"], "t10_scsi_asc_ascq")
        self.assertEqual(sense_record["source_attribution"]["url"], "https://www.t10.org/lists/asc-num.htm")
        self.assertEqual(sense_record["likely_layer"], "SAS path, cable, expander, or target port")
        self.assertEqual(target["event_table"]["total_count"], 5)
        self.assertEqual(target["event_table"]["page_size"], 25)
        self.assertEqual(target["event_table"]["rows"][0]["event_id"], target["decoded_records"][0]["event_id"])

    def test_parse_mpr_dmesg_events_decodes_lsi_loginfo_open_failure_details(self) -> None:
        events = parse_mpr_dmesg_events(
            """
mpr0: Controller reported scsi ioc terminated tgt 180 SMID 140 loginfo 3112010a
mpr0: Controller reported scsi ioc terminated tgt 180 SMID 141 loginfo 3112010c
mpr0: Controller reported scsi ioc terminated tgt 181 SMID 142 loginfo 31110e05
""".strip()
        )

        controller = events["by_controller"]["mpr0"]
        decoded_by_loginfo = {
            record["loginfo"]: record["decoded"]
            for record in controller["decoded_records"]
            if record.get("loginfo")
        }

        self.assertEqual(decoded_by_loginfo["3112010a"]["lsi_loginfo"]["detail_symbol"], "PL_LOGINFO_SUB_CODE_OPEN_FAIL_BREAK")
        self.assertEqual(decoded_by_loginfo["3112010a"]["family"], "sas_transport")
        self.assertEqual(decoded_by_loginfo["3112010a"]["decode_confidence"], "vendor-reference")
        self.assertEqual(decoded_by_loginfo["3112010c"]["lsi_loginfo"]["detail_symbol"], "PL_LOGINFO_SUB_CODE_OPEN_FAIL_OPEN_TIMEOUT_EXP")
        self.assertEqual(decoded_by_loginfo["3112010c"]["family"], "timeout")
        self.assertEqual(decoded_by_loginfo["3112010c"]["decode_confidence"], "vendor-reference")
        self.assertEqual(decoded_by_loginfo["31110e05"]["lsi_loginfo"]["code_symbol"], "PL_LOGINFO_CODE_RESET")
        self.assertEqual(decoded_by_loginfo["31110e05"]["lsi_loginfo"]["sub_code_symbol"], "PL_LOGINFO_SUB_CODE_DISCOVERY_SATA_ERR")
        self.assertEqual(decoded_by_loginfo["31110e05"]["lsi_loginfo"]["unparsed"], "0x00000005")
        self.assertEqual(decoded_by_loginfo["31110e05"]["decode_confidence"], "vendor-reference-partial")

    def test_parse_mpr_dmesg_events_preserves_source_timestamps_when_present(self) -> None:
        events = parse_mpr_dmesg_events(
            """
May 19 13:45:02 The-Archive kernel: mpr0: Controller reported scsi ioc terminated tgt 180 SMID 140 loginfo 31120302
May 19 21:14:06 The-Archive mpr0: Controller reported scsi ioc terminated tgt 181 SMID 141 loginfo 31120302
May 19 21:16:03 The-Archive (da44:mpr0:0:180:0): CAM status: CCB request completed with an error
[12345.678901] (da44:mpr0:0:180:0): Retrying command (per sense data)
""".strip()
        )

        rows = events["by_controller"]["mpr0"]["event_table"]["rows"]

        self.assertEqual(rows[0]["timestamp_raw"], "May 19 13:45:02")
        self.assertEqual(rows[1]["timestamp_raw"], "May 19 21:14:06")
        self.assertEqual(rows[2]["timestamp_raw"], "May 19 21:16:03")
        self.assertEqual(rows[3]["timestamp_raw"], "[12345.678901]")

    def test_parse_mpr_dmesg_events_decodes_cam_and_scsi_status_examples(self) -> None:
        events = parse_mpr_dmesg_events(
            """
(da44:mpr0:0:180:0): CAM status: CCB request completed with an error
(da46:mpr0:0:182:0): CAM status: SCSI Status Error
(da46:mpr0:0:182:0): SCSI status: Check Condition
(da47:mpr0:0:183:0): SCSI status: 0x40
""".strip()
        )

        rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
        ccb_status = rows[0]
        cam_scsi_status = rows[1]
        scsi_status = rows[2]
        task_aborted = rows[3]

        self.assertEqual(ccb_status["label"], "CAM completed command with an error")
        self.assertEqual(ccb_status["cam_status"], "CCB request completed with an error")
        self.assertEqual(cam_scsi_status["label"], "CAM reported SCSI status error")
        self.assertEqual(cam_scsi_status["cam_status"], "SCSI Status Error")
        self.assertEqual(scsi_status["family"], "scsi_status")
        self.assertEqual(scsi_status["label"], "SCSI status: Check Condition")
        self.assertEqual(scsi_status["scsi_status"], "Check Condition")
        self.assertEqual(scsi_status["decode_confidence"], "standard")
        self.assertEqual(scsi_status["decode_source"], "t10_scsi_status")
        self.assertEqual(scsi_status["source_attribution"]["url"], "https://www.t10.org/lists/2status.htm")
        self.assertEqual(scsi_status["severity"], "warning")
        self.assertEqual(task_aborted["label"], "SCSI status: Task Aborted")
        self.assertEqual(task_aborted["family"], "aborted_command")
        self.assertEqual(task_aborted["scsi_status_code"], "0x40")

    def test_parse_mpr_dmesg_events_decodes_cam_errno_retry_exhaustion(self) -> None:
        events = parse_mpr_dmesg_events(
            """
Jun  7 10:39:04 truenas (da1:mpr0:0:9:0): WRITE(10). CDB: 2a 00 00 40 00 80 00 08 00 00
Jun  7 10:39:04 truenas (da1:mpr0:0:9:0): CAM status: CCB request completed with an error
Jun  7 10:39:04 truenas (da1:mpr0:0:9:0): Error 5, Retries exhausted
Jun  7 10:39:04 truenas mpr0: Controller reported scsi ioc terminated tgt 9 SMID 439 loginfo 3112010c
Jun  7 10:39:05 truenas mpr0: Controller reported scsi ioc terminated tgt 12 SMID 510 loginfo 31120302
""".strip()
        )

        da1 = events["by_device"]["da1"]
        rows = da1["event_table"]["rows"]
        cam_error = next(record for record in rows if record["event_type"] == "cam_error")

        self.assertEqual(events["event_count"], 5)
        self.assertEqual(cam_error["label"], "CAM EIO: Retries exhausted")
        self.assertEqual(cam_error["family"], "cam_error")
        self.assertEqual(cam_error["cam_error_code"], 5)
        self.assertEqual(cam_error["errno_name"], "EIO")
        self.assertEqual(cam_error["errno_label"], "Input/output error")
        self.assertEqual(cam_error["decode_confidence"], "observed")
        self.assertEqual(cam_error["decode_source"], "freebsd_cam_errno")
        self.assertEqual(cam_error["source_attribution"]["url"], "https://man.freebsd.org/cgi/man.cgi?apropos=0&manpath=freebsd&query=intro&sektion=2")
        self.assertEqual(da1["operation_counts"]["WRITE(10)"], 1)
        self.assertEqual(events["by_controller"]["mpr0"]["loginfo_counts"]["31120302"], 1)

    def test_archive_core_bad_cable_fixture_builds_normalized_event_table(self) -> None:
        events = parse_mpr_dmesg_events(ARCHIVE_CORE_BAD_CABLE_DMESG)

        controller = events["by_controller"]["mpr0"]
        da44 = events["by_device"]["da44"]
        target_181 = events["by_controller_target"]["mpr0:181"]
        rows = controller["event_table"]["rows"]

        self.assertEqual(events["event_count"], 19)
        self.assertEqual(controller["loginfo_counts"]["31120302"], 1)
        self.assertEqual(controller["loginfo_counts"]["3112010a"], 1)
        self.assertEqual(controller["loginfo_counts"]["3112010c"], 1)
        self.assertEqual(controller["loginfo_counts"]["31110e05"], 1)
        self.assertEqual(controller["sense_counts"]["NAK received"], 1)
        self.assertEqual(controller["sense_counts"]["Connection lost"], 1)
        self.assertEqual(controller["sense_counts"]["ACK/NAK timeout"], 1)
        self.assertFalse(any("decoded" in event for event in controller["recent_events"]))
        self.assertEqual(controller["event_table"]["schema_version"], 1)
        self.assertEqual(controller["event_table"]["total_count"], len(controller["decoded_records"]))
        self.assertEqual([finding["severity"] for finding in controller["top_findings"][:4]], ["error", "error", "error", "error"])
        self.assertGreaterEqual(controller["top_findings"][0]["count"], controller["top_findings"][1]["count"])
        self.assertTrue(controller["top_findings"][0]["fingerprint"])
        self.assertIn("da44", da44["operator_summary"])

        log_sense = next(record for record in rows if record.get("operation") == "LOG SENSE")
        self.assertEqual(log_sense["decode_confidence"], "standard")
        self.assertEqual(log_sense["log_page"], "Protocol-Specific Port")
        self.assertEqual(log_sense["log_page_source"]["url"], "https://www.t10.org/lists/1spc-lst.htm")
        self.assertEqual(log_sense["sas_phy_log_concepts"][0]["name"], "Invalid dword count")

        service_action = next(record for record in rows if record.get("opcode") == "0x9e")
        self.assertEqual(service_action["operation"], "READ CAPACITY(16)")
        self.assertEqual(service_action["service_action"], "0x10")
        self.assertEqual(service_action["service_action_label"], "READ CAPACITY(16)")

        timeout_sense = next(record for record in target_181["decoded_records"] if record.get("asc") == "4b,3")
        self.assertEqual(timeout_sense["asc_label"], "ACK/NAK timeout")
        self.assertEqual(timeout_sense["family"], "timeout")

    def test_parse_mpr_dmesg_events_tracks_standard_observed_and_unconfirmed_decodes(self) -> None:
        events = parse_mpr_dmesg_events(
            """
(da61:mpr0:0:190:0): SCSI sense: HARDWARE ERROR asc:0c,0 (Write error)
(da62:mpr0:0:191:0): SCSI sense: MEDIUM ERROR asc:11,0 (Unrecovered read error)
(da63:mpr0:0:192:0): SCSI sense: ABORTED COMMAND asc:7f,7f (Vendor path wobble)
(da64:mpr0:0:193:0): SERVICE ACTION IN(16). CDB: 9e 1f 00 00 00 00 00 00 00 00 00 00 00 20 00 00
(da65:mpr0:0:195:0): SCSI sense: HARDWARE ERROR asc:3,2 (Excessive write errors)
(da66:mpr0:0:196:0): SECURITY PROTOCOL OUT. CDB: b5 00 00 00 00 00 00 00 00 00 00 00
(da67:mpr0:0:197:0): SERVICE ACTION IN(12). CDB: ab 1f 00 00 00 00 00 00 00 00 00 00
mpr0: Controller reported scsi ioc terminated tgt 194 SMID 143 loginfo 39999999
""".strip()
        )

        rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
        write_sense = next(record for record in rows if record.get("asc") == "0c,0")
        read_sense = next(record for record in rows if record.get("asc") == "11,0")
        observed_sense = next(record for record in rows if record.get("asc") == "7f,7f")
        partial_cdb = next(record for record in rows if record.get("opcode") == "0x9e")
        peripheral_write_fault = next(record for record in rows if record.get("asc") == "3,2")
        security_protocol_out = next(record for record in rows if record.get("opcode") == "0xb5")
        service_action_12 = next(record for record in rows if record.get("opcode") == "0xab")
        unknown_loginfo = next(record for record in rows if record.get("loginfo") == "39999999")

        self.assertEqual(write_sense["family"], "write_error")
        self.assertEqual(write_sense["severity"], "error")
        self.assertEqual(write_sense["decode_confidence"], "standard")
        self.assertEqual(read_sense["family"], "read_error")
        self.assertEqual(read_sense["severity"], "error")
        self.assertEqual(read_sense["decode_confidence"], "standard")
        self.assertEqual(observed_sense["label"], "Vendor path wobble")
        self.assertEqual(observed_sense["decode_confidence"], "observed")
        self.assertEqual(observed_sense["decode_source"], "kernel_message")
        self.assertIn("not in the current local lookup table", observed_sense["decoder_note"])
        self.assertEqual(partial_cdb["decode_confidence"], "standard-partial")
        self.assertEqual(partial_cdb["operation"], "SERVICE ACTION IN(16)")
        self.assertIn("service action", partial_cdb["decoder_note"])
        self.assertEqual(peripheral_write_fault["asc_label"], "Excessive write errors")
        self.assertEqual(peripheral_write_fault["family"], "write_error")
        self.assertEqual(peripheral_write_fault["decode_confidence"], "standard")
        self.assertEqual(security_protocol_out["operation"], "SECURITY PROTOCOL OUT")
        self.assertEqual(security_protocol_out["decode_confidence"], "standard")
        self.assertEqual(service_action_12["operation"], "SERVICE ACTION IN(12)")
        self.assertEqual(service_action_12["decode_confidence"], "standard-partial")
        self.assertEqual(service_action_12["service_action"], "0x1f")
        self.assertEqual(unknown_loginfo["decode_confidence"], "unconfirmed")
        self.assertEqual(unknown_loginfo["decode_source"], "baruch_lsi_decode_loginfo")
        self.assertIn("not in the current local LSI lookup table", unknown_loginfo["decoder_note"])

    def test_parse_mpr_dmesg_events_decodes_broader_standard_asc_ascq_families(self) -> None:
        events = parse_mpr_dmesg_events(
            """
(da70:mpr0:0:200:0): SCSI sense: RECOVERED ERROR asc:18,5 (Recovered data, recommend reassignment)
(da71:mpr0:0:201:0): SCSI sense: HARDWARE ERROR asc:0b,2 (Warning - enclosure degraded)
(da72:mpr0:0:202:0): SCSI sense: DATA PROTECT asc:27,7 (Space allocation failed write protect)
(da73:mpr0:0:203:0): SCSI sense: UNIT ATTENTION asc:2f,0 (Commands cleared by another initiator)
(da74:mpr0:0:204:0): SCSI sense: HARDWARE ERROR asc:5d,3 (Spare area exhaustion prediction threshold exceeded)
(da75:mpr0:0:205:0): SCSI sense: HARDWARE ERROR asc:3e,2 (Timeout on logical unit)
(da76:mpr0:0:206:0): SCSI sense: MEDIUM ERROR asc:31,0 (Medium format corrupted)
(da77:mpr0:0:207:0): SCSI sense: HARDWARE ERROR asc:5b,1 (Threshold condition met)
(da78:mpr0:0:208:0): SCSI sense: UNIT ATTENTION asc:29,7 (I_T nexus loss occurred)
(da79:mpr0:0:209:0): SCSI sense: NOT READY asc:4,0b (Logical unit not accessible, target port in standby state)
(da80:mpr0:0:210:0): SCSI sense: UNIT ATTENTION asc:3f,8 (Spare created or modified)
(da81:mpr0:0:211:0): SCSI sense: NO SENSE asc:5e,2 (Standby condition activated by timer)
""".strip()
        )

        rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
        rows_by_asc = {row["asc"]: row for row in rows}

        self.assertEqual(rows_by_asc["18,5"]["asc_label"], "Recovered data, recommend reassignment")
        self.assertEqual(rows_by_asc["18,5"]["family"], "recovered_data")
        self.assertEqual(rows_by_asc["18,5"]["severity"], "warning")
        self.assertEqual(rows_by_asc["0b,2"]["family"], "enclosure_warning")
        self.assertEqual(rows_by_asc["0b,2"]["severity"], "warning")
        self.assertEqual(rows_by_asc["27,7"]["family"], "write_protect")
        self.assertEqual(rows_by_asc["2f,0"]["family"], "unit_attention")
        self.assertEqual(rows_by_asc["5d,3"]["family"], "failure_prediction")
        self.assertEqual(rows_by_asc["3e,2"]["family"], "timeout")
        self.assertEqual(rows_by_asc["3e,2"]["severity"], "error")
        self.assertEqual(rows_by_asc["31,0"]["family"], "medium_format")
        self.assertEqual(rows_by_asc["5b,1"]["family"], "log_exception")
        self.assertEqual(rows_by_asc["29,7"]["family"], "link_loss")
        self.assertEqual(rows_by_asc["29,7"]["severity"], "error")
        self.assertEqual(rows_by_asc["4,0b"]["asc_label"], "Logical unit not accessible, target port in standby state")
        self.assertEqual(rows_by_asc["4,0b"]["family"], "device_path_exception")
        self.assertEqual(rows_by_asc["3f,8"]["family"], "unit_attention")
        self.assertEqual(rows_by_asc["5e,2"]["family"], "power_condition")
        self.assertTrue(all(row["decode_confidence"] == "standard" for row in rows_by_asc.values()))
        self.assertTrue(all(row["source_attribution"]["url"] == "https://www.t10.org/lists/asc-num.htm" for row in rows_by_asc.values()))

    def test_parse_mpr_dmesg_events_decodes_broader_lsi_loginfo_tables(self) -> None:
        events = parse_mpr_dmesg_events(
            """
mpr0: Controller reported scsi ioc terminated tgt 180 SMID 140 loginfo 31170000
mpr0: Controller reported scsi ioc terminated tgt 181 SMID 141 loginfo 312000d0
mpr0: Controller reported scsi ioc terminated tgt 182 SMID 142 loginfo 30030200
""".strip()
        )

        decoded_by_loginfo = {
            record["loginfo"]: record
            for record in events["by_controller"]["mpr0"]["decoded_records"]
            if record.get("loginfo")
        }

        self.assertEqual(decoded_by_loginfo["31170000"]["family"], "link_loss")
        self.assertIn("I-T Nexus Loss", decoded_by_loginfo["31170000"]["description"])
        self.assertEqual(decoded_by_loginfo["312000d0"]["family"], "ses_enclosure")
        self.assertIn("SEP I/O retries exhausted", decoded_by_loginfo["312000d0"]["label"])
        self.assertEqual(decoded_by_loginfo["30030200"]["family"], "controller_configuration")
        self.assertIn("Invalid Page Number", decoded_by_loginfo["30030200"]["description"])

    def test_parse_mpr_dmesg_events_decodes_deeper_scsi_service_actions_and_lengths(self) -> None:
        events = parse_mpr_dmesg_events(
            """
(da70:mpr0:0:200:0): MAINTENANCE IN. CDB: a3 0a 00 00 00 00 00 00 20 00 00 00
(da71:mpr0:0:201:0): MAINTENANCE IN. CDB: a3 0c 00 00 00 00 00 00 10 00 00 00
(da72:mpr0:0:202:0): PERSISTENT RESERVE IN. CDB: 5e 03 00 00 00 00 00 00 20 00
(da73:mpr0:0:203:0): PERSISTENT RESERVE OUT. CDB: 5f 05 00 00 00 00 00 00 18 00
(da74:mpr0:0:204:0): UNMAP. CDB: 42 00 00 00 00 00 00 00 18 00
(da75:mpr0:0:205:0): LOG SENSE. CDB: 4d 01 58 00 00 00 00 00 40 00
(da76:mpr0:0:206:0): THIRD-PARTY COPY IN. CDB: 84 04 07 00 00 00 00 00 00 00 00 00 00 40 00 00
(da77:mpr0:0:207:0): SANITIZE. CDB: 48 01 00 00 00 00 00 00 18 00
(da78:mpr0:0:208:0): READ ELEMENT STATUS. CDB: b8 00 00 00 00 00 00 00 20 00 00 00
(da79:mpr0:0:209:0): SPARE (IN). CDB: bc 00 00 00 00 00 00 00 20 00 00 00
""".strip()
        )

        rows = events["by_controller"]["mpr0"]["event_table"]["rows"]
        target_port_groups = next(record for record in rows if record.get("target") == "200")
        supported_ops = next(record for record in rows if record.get("target") == "201")
        persistent_reserve_in = next(record for record in rows if record.get("target") == "202")
        persistent_reserve_out = next(record for record in rows if record.get("target") == "203")
        unmap = next(record for record in rows if record.get("target") == "204")
        log_sense = next(record for record in rows if record.get("target") == "205")
        receive_copy_results = next(record for record in rows if record.get("target") == "206")
        sanitize = next(record for record in rows if record.get("target") == "207")
        read_element_status = next(record for record in rows if record.get("target") == "208")
        spare_in = next(record for record in rows if record.get("target") == "209")

        self.assertEqual(target_port_groups["operation"], "REPORT TARGET PORT GROUPS")
        self.assertEqual(target_port_groups["service_action"], "0x0a")
        self.assertEqual(target_port_groups["allocation_length"], 8192)
        self.assertEqual(target_port_groups["decode_confidence"], "standard")
        self.assertEqual(supported_ops["operation"], "REPORT SUPPORTED OPERATION CODES")
        self.assertEqual(supported_ops["service_action"], "0x0c")
        self.assertEqual(persistent_reserve_in["operation"], "READ FULL STATUS")
        self.assertEqual(persistent_reserve_in["allocation_length"], 32)
        self.assertEqual(persistent_reserve_out["operation"], "PREEMPT AND ABORT")
        self.assertEqual(persistent_reserve_out["parameter_list_length"], 24)
        self.assertEqual(unmap["operation"], "UNMAP")
        self.assertNotIn("lba", unmap)
        self.assertNotIn("transfer_blocks", unmap)
        self.assertEqual(unmap["parameter_list_length"], 24)
        self.assertEqual(log_sense["log_page"], "Protocol-Specific Port")
        self.assertEqual(log_sense["log_page_control"], "0x1")
        self.assertEqual(log_sense["log_page_control_label"], "Current threshold values")
        self.assertTrue(log_sense["log_save_parameters"])
        self.assertEqual(log_sense["allocation_length"], 64)
        self.assertEqual(receive_copy_results["operation"], "FAILED SEGMENT DETAILS")
        self.assertEqual(receive_copy_results["service_action"], "0x04")
        self.assertEqual(receive_copy_results["allocation_length"], 64)
        self.assertEqual(sanitize["operation"], "SANITIZE")
        self.assertEqual(sanitize["decode_confidence"], "standard")
        self.assertEqual(read_element_status["operation"], "READ ELEMENT STATUS")
        self.assertEqual(read_element_status["decode_source"], "t10_scsi_operation_codes")
        self.assertEqual(spare_in["operation"], "SPARE (IN)")
        self.assertEqual(spare_in["decode_confidence"], "standard")

    def test_parse_mpr_dmesg_events_decodes_deeper_lsi_reference_tables(self) -> None:
        events = parse_mpr_dmesg_events(
            """
mpr0: Controller reported scsi ioc terminated tgt 180 SMID 140 loginfo 3003e000
mpr0: Controller reported scsi ioc terminated tgt 181 SMID 141 loginfo 30010102
mpr0: Controller reported scsi ioc terminated tgt 182 SMID 142 loginfo 30060002
mpr0: Controller reported scsi ioc terminated tgt 183 SMID 143 loginfo 30070005
mpr0: Controller reported scsi ioc terminated tgt 184 SMID 144 loginfo 31110118
mpr0: Controller reported scsi ioc terminated tgt 185 SMID 145 loginfo 31110e01
mpr0: Controller reported scsi ioc terminated tgt 186 SMID 146 loginfo 31200102
mpr0: Controller reported scsi ioc terminated tgt 187 SMID 147 loginfo 32010035
""".strip()
        )

        decoded_by_loginfo = {
            record["loginfo"]: record
            for record in events["by_controller"]["mpr0"]["decoded_records"]
            if record.get("loginfo")
        }

        self.assertIn("Firmware upload, no flash available", decoded_by_loginfo["3003e000"]["label"])
        self.assertEqual(decoded_by_loginfo["3003e000"]["decode_confidence"], "vendor-reference")
        self.assertIn("Flash erase failed", decoded_by_loginfo["30010102"]["label"])
        self.assertEqual(decoded_by_loginfo["30010102"]["family"], "controller_configuration")
        self.assertIn("Invalid bus/id", decoded_by_loginfo["30060002"]["label"])
        self.assertEqual(decoded_by_loginfo["30060002"]["family"], "ses_enclosure")
        self.assertIn("Target mode abort exact IO request", decoded_by_loginfo["30070005"]["label"])
        self.assertEqual(decoded_by_loginfo["30070005"]["family"], "aborted_command")
        self.assertIn("STP resources busy", decoded_by_loginfo["31110118"]["label"])
        self.assertEqual(decoded_by_loginfo["31110118"]["decode_confidence"], "vendor-reference")
        self.assertIn("Discovery remote SEP reset", decoded_by_loginfo["31110e01"]["label"])
        self.assertEqual(decoded_by_loginfo["31110e01"]["family"], "ses_enclosure")
        self.assertIn("ISTWI interrupt received while idle", decoded_by_loginfo["31200102"]["label"])
        self.assertEqual(decoded_by_loginfo["31200102"]["family"], "ses_enclosure")
        self.assertIn("SATA 48-bit LBA not supported", decoded_by_loginfo["32010035"]["label"])
        self.assertEqual(decoded_by_loginfo["32010035"]["decode_confidence"], "vendor-reference")


class SasFabricAliasStoreTests(unittest.TestCase):
    def test_enclosure_alias_overrides_system_alias_for_same_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
            store.save_alias(
                SasFabricAlias(
                    system_id="archive-core",
                    object_id="backplane:0",
                    object_kind="backplane",
                    label="System backplane",
                )
            )
            store.save_alias(
                SasFabricAlias(
                    system_id="archive-core",
                    enclosure_id="enc-60",
                    object_id="backplane:0",
                    object_kind="backplane",
                    label="Front-left backplane",
                )
            )
            store.save_alias(
                SasFabricAlias(
                    system_id="archive-core",
                    object_id="controller:mpr0",
                    object_kind="controller",
                    label="Archive left HBA",
                )
            )

            selected = {alias.object_id: alias for alias in store.list_aliases("archive-core", "enc-60")}

            self.assertEqual(selected["backplane:0"].label, "Front-left backplane")
            self.assertEqual(selected["backplane:0"].enclosure_id, "enc-60")
            self.assertEqual(selected["controller:mpr0"].label, "Archive left HBA")
            self.assertTrue(store.clear_alias("archive-core", "enc-60", "backplane:0"))
            selected_after_clear = {alias.object_id: alias for alias in store.list_aliases("archive-core", "enc-60")}
            self.assertEqual(selected_after_clear["backplane:0"].label, "System backplane")


class SasFabricSnapshotTests(unittest.TestCase):
    def test_core_snapshot_applies_aliases_without_rewriting_raw_labels(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="da0",
            raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": 1},
            multipath=MultipathView(
                name="mpath0",
                device_name="multipath/disk0",
                members=[
                    MultipathMember(device_name="da0", state="ACTIVE", controller_label="mpr1"),
                    MultipathMember(device_name="da60", state="FAIL", controller_label="mpr0"),
                ],
            ),
        )
        snapshot = InventorySnapshot(
            slots=[slot],
            refresh_interval_seconds=30,
            selected_system_id="archive-core",
            selected_system_label="The Archive",
            selected_system_platform="core",
            selected_enclosure_id="enc-60",
            selected_enclosure_label="60 Bay",
        )
        system = SystemConfig(
            id="archive-core",
            label="The Archive",
            truenas=TrueNASConfig(platform="core"),
        )

        fabric = build_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                "sudo -n /usr/sbin/mprutil -u 0 show adapter": MPR0_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 0 show expanders": MPR0_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 0 show enclosures": MPR0_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 0 show devices": MPR0_DEVICES,
                "sudo -n /usr/sbin/mprutil -u 1 show adapter": MPR1_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 1 show expanders": MPR1_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 1 show enclosures": MPR1_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 1 show devices": MPR1_DEVICES,
            },
            aliases=[
                SasFabricAlias(
                    system_id="archive-core",
                    object_id="controller:mpr0",
                    object_kind="controller",
                    label="Archive left HBA",
                ),
                SasFabricAlias(
                    system_id="archive-core",
                    object_id="path:mpr0:fail",
                    object_kind="path",
                    label="Bad-cable path",
                ),
                SasFabricAlias(
                    system_id="archive-core",
                    enclosure_id="enc-60",
                    object_id="backplane:0",
                    object_kind="backplane",
                    label="Front-left backplane",
                ),
            ],
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}
        controllers = {row["id"]: row for row in fabric.controllers}
        paths = {row["id"]: row for row in fabric.paths}

        self.assertEqual(nodes["controller:mpr0"].label, "mpr0")
        self.assertEqual(nodes["controller:mpr0"].display_label, "Archive left HBA")
        self.assertEqual(nodes["controller:mpr0"].raw["operator_alias"], "Archive left HBA")
        self.assertEqual(controllers["controller:mpr0"]["display_label"], "Archive left HBA")
        self.assertEqual(traces["path:mpr0:fail"].display_label, "Bad-cable path")
        self.assertEqual(paths["path:mpr0:fail"]["display_label"], "Bad-cable path")
        self.assertEqual(nodes["backplane:0"].label, "Backplane Zone 1")
        self.assertEqual(nodes["backplane:0"].display_label, "Front-left backplane")
        self.assertIn("backplane:0", traces["bay:0"].node_ids)
        self.assertEqual({alias.object_id for alias in fabric.aliases}, {"controller:mpr0", "path:mpr0:fail", "backplane:0"})

    def test_core_snapshot_builds_traceable_dual_hba_path_graph(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="da0",
            pool_name="tank",
            vdev_name="raidz2-0",
            health="ONLINE",
            ssh_ses_device="/dev/ses4",
            ssh_ses_targets=[{"ses_device": "/dev/ses8"}],
            raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": 1},
            multipath=MultipathView(
                name="mpath0",
                device_name="multipath/disk0",
                members=[
                    MultipathMember(device_name="da0", state="ACTIVE", controller_label="mpr1"),
                    MultipathMember(device_name="da60", state="FAIL", controller_label="mpr0"),
                ],
            ),
        )
        snapshot = InventorySnapshot(
            slots=[slot],
            refresh_interval_seconds=30,
            selected_system_id="archive-core",
            selected_system_label="The Archive",
            selected_system_platform="core",
            selected_enclosure_id="enc-60",
            selected_enclosure_label="60 Bay",
        )
        system = SystemConfig(
            id="archive-core",
            label="The Archive",
            truenas=TrueNASConfig(platform="core"),
        )

        fabric = build_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                "sudo -n /usr/sbin/mprutil -u 0 show adapter": MPR0_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 0 show expanders": MPR0_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 0 show enclosures": MPR0_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 0 show devices": MPR0_DEVICES,
                "sudo -n /usr/sbin/mprutil -u 0 show iocfacts": "Max Chain Depth: 1",
                "sudo -n /usr/sbin/mprutil -u 1 show adapter": MPR1_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 1 show expanders": MPR1_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 1 show enclosures": MPR1_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 1 show devices": MPR1_DEVICES,
                "sudo -n /usr/sbin/mprutil -u 1 show iocfacts": "Max Chain Depth: 1",
                CORE_PCICONF_LV_COMMAND: PCICONF_MPR_OUTPUT,
                CORE_DMIDECODE_SLOT_COMMAND: DMIDECODE_SLOT_OUTPUT,
                CORE_MPR_SYSCTL_LOCATION_COMMAND: MPR_SYSCTL_LOCATIONS,
                CORE_MPR_DMESG_EVENTS_COMMAND: MPR_DMESG_EVENTS,
            },
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}
        links = {link.id: link for link in fabric.links}
        mpr0_expander_id = "expander:controller:mpr0:50030480090c4f7f"
        mpr1_expander_id = "expander:controller:mpr1:50030480090c8f7f"
        mpr0_enclosure_id = "mpr-enclosure:controller:mpr0:0002"
        mpr1_enclosure_id = "mpr-enclosure:controller:mpr1:0012"
        mpr0_path_expander_link = f"path-expander:path:mpr0:fail->{mpr0_expander_id}"
        mpr1_path_expander_link = f"path-expander:path:mpr1:active->{mpr1_expander_id}"
        mpr0_expander_enclosure_link = f"expander-enclosure:{mpr0_expander_id}->{mpr0_enclosure_id}"
        mpr1_expander_enclosure_link = f"expander-enclosure:{mpr1_expander_id}->{mpr1_enclosure_id}"
        mpr0_enclosure_bay_link = f"mpr-enclosure-bay:{mpr0_enclosure_id}->bay:0:0"
        mpr1_enclosure_bay_link = f"mpr-enclosure-bay:{mpr1_enclosure_id}->bay:0:0"

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.system_id, "archive-core")
        self.assertIn("controller:mpr0", nodes)
        self.assertIn("controller:mpr1", nodes)
        self.assertEqual(nodes["controller:mpr0"].metrics["pcie_slot"], "CPU2 SLOT1 PCI-E 3.0 X8")
        self.assertEqual(nodes["controller:mpr0"].metrics["pci_address"], "0000:82:00.0")
        self.assertEqual(nodes["controller:mpr0"].raw["pci_parent"], "pci15")
        self.assertEqual(nodes["controller:mpr0"].raw["acpi_handle"], r"\_SB_.PCI1.QR3A.H000")
        self.assertIn("sysctl dev.mpr.%location", nodes["controller:mpr0"].evidence)
        self.assertIn("dmidecode -t slot", nodes["controller:mpr0"].evidence)
        self.assertEqual(fabric.controllers[0]["pcie_slot"], "CPU2 SLOT1 PCI-E 3.0 X8")
        self.assertEqual(fabric.controllers[1]["pcie_slot"], "CPU2 SLOT2 PCI-E 3.0 X8")
        self.assertEqual(fabric.raw["pci_controllers"][0]["pci_address"], "0000:82:00.0")
        self.assertEqual(fabric.raw["pcie_slots"][0]["designation"], "CPU2 SLOT1 PCI-E 3.0 X8")
        self.assertEqual(fabric.raw["mpr_sysctl_locations"]["mpr1"]["pci_parent"], "pci16")
        self.assertEqual(nodes["controller:mpr0"].status, "degraded")
        self.assertIn("path:mpr0:fail", nodes)
        self.assertEqual(nodes["path:mpr0:fail"].related_slots, [0])
        self.assertIn("ses:/dev/ses4", nodes)
        self.assertIn("ses:/dev/ses8", nodes)
        self.assertEqual(nodes[mpr0_expander_id].related_slots, [0])
        self.assertEqual(nodes[mpr1_expander_id].related_slots, [0])
        self.assertEqual(nodes[mpr0_enclosure_id].related_slots, [0])
        self.assertEqual(nodes[mpr1_enclosure_id].related_slots, [0])
        self.assertIn("path:mpr0:fail", traces)
        self.assertEqual(traces["path:mpr0:fail"].slots, [0])
        self.assertIn(mpr0_expander_id, traces["path:mpr0:fail"].node_ids)
        self.assertIn(mpr0_enclosure_id, traces["path:mpr0:fail"].node_ids)
        self.assertIn(mpr0_path_expander_link, traces["path:mpr0:fail"].link_ids)
        self.assertIn(mpr0_expander_enclosure_link, traces["path:mpr0:fail"].link_ids)
        self.assertIn(mpr1_expander_id, traces["path:mpr1:active"].node_ids)
        self.assertIn(mpr1_enclosure_id, traces["path:mpr1:active"].node_ids)
        self.assertIn(mpr1_path_expander_link, traces["path:mpr1:active"].link_ids)
        self.assertIn(mpr1_expander_enclosure_link, traces["path:mpr1:active"].link_ids)
        self.assertIn("bay:0", traces)
        self.assertIn("controller:mpr0", traces["bay:0"].node_ids)
        self.assertIn("path:mpr0:fail", traces["bay:0"].node_ids)
        self.assertIn(mpr0_expander_id, traces["bay:0"].node_ids)
        self.assertIn(mpr0_enclosure_id, traces["bay:0"].node_ids)
        self.assertIn(mpr1_expander_id, traces["bay:0"].node_ids)
        self.assertIn(mpr1_enclosure_id, traces["bay:0"].node_ids)
        self.assertIn("ses:/dev/ses4", traces["bay:0"].node_ids)
        self.assertIn("host-controller:host->controller:mpr0", traces["bay:0"].link_ids)
        self.assertIn("controller-path:controller:mpr0->path:mpr0:fail", traces["bay:0"].link_ids)
        self.assertIn(mpr0_path_expander_link, traces["bay:0"].link_ids)
        self.assertIn(mpr0_expander_enclosure_link, traces["bay:0"].link_ids)
        self.assertIn(mpr0_enclosure_bay_link, traces["bay:0"].link_ids)
        self.assertIn(mpr1_enclosure_bay_link, traces["bay:0"].link_ids)
        self.assertIn("path-bay:path:mpr0:fail->bay:0:0", links)
        self.assertEqual(links[mpr0_path_expander_link].related_slots, [0])
        self.assertEqual(links[mpr0_expander_enclosure_link].related_slots, [0])
        self.assertEqual(links[mpr0_enclosure_bay_link].related_slots, [0])
        self.assertEqual(len(traces["bay:0"].metrics["mpr_devices"]), 2)
        mpr0_metric = next(
            metric
            for metric in traces["bay:0"].metrics["mpr_devices"]
            if metric["controller"] == "mpr0"
        )
        self.assertEqual(mpr0_metric["diagnostics"]["sense_counts"]["NAK received"], 1)
        self.assertEqual(mpr0_metric["diagnostics"]["fault_family_counts"]["sas_protocol"], 1)
        self.assertIn("operator_summary", mpr0_metric["diagnostics"])
        self.assertEqual(nodes["controller:mpr0"].metrics["kernel_diagnostics"]["event_count"], 5)

    def test_core_snapshot_scopes_mpr_infrastructure_to_selected_enclosure(self) -> None:
        disk_devices = {0: "da95", 6: "da96", 12: "da99", 18: "da100"}
        slots = [
            SlotView(
                slot=slot,
                slot_label=f"{slot:02d}",
                row_index=slot // 6,
                column_index=slot % 6,
                present=True,
                state=SlotState.healthy if slot in disk_devices else SlotState.empty,
                device_name=disk_devices.get(slot),
                model="MK001920GWHRU" if slot in disk_devices else None,
                serial=f"SATA{slot:02d}" if slot in disk_devices else None,
                ssh_ses_device="/dev/ses2",
                ssh_ses_targets=[{"ses_device": "/dev/ses3"}],
                raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": slot},
            )
            for slot in range(24)
        ]
        snapshot = InventorySnapshot(
            slots=slots,
            refresh_interval_seconds=30,
            selected_system_id="archive-core",
            selected_system_label="The Archive",
            selected_system_platform="core",
            selected_enclosure_id="50030480090c4f7f",
            selected_enclosure_label="Front 24 Bay",
        )
        system = SystemConfig(
            id="archive-core",
            label="The Archive",
            truenas=TrueNASConfig(platform="core"),
        )

        fabric = build_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                "sudo -n /usr/sbin/mprutil -u 0 show adapter": MPR0_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 0 show expanders": MPR0_EXPANDERS_WITH_UNRELATED,
                "sudo -n /usr/sbin/mprutil -u 0 show enclosures": MPR0_ENCLOSURES_WITH_UNRELATED,
                "sudo -n /usr/sbin/mprutil -u 1 show adapter": MPR1_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 1 show expanders": MPR1_EXPANDERS_WITH_UNRELATED,
                "sudo -n /usr/sbin/mprutil -u 1 show enclosures": MPR1_ENCLOSURES_WITH_UNRELATED,
            },
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}
        bay_slots = list(range(24))
        mpr0_selected_expander = "expander:controller:mpr0:50030480090c4f7f"
        mpr1_selected_expander = "expander:controller:mpr1:50030480090c4fff"
        mpr0_selected_enclosure = "mpr-enclosure:controller:mpr0:0002"
        mpr1_selected_enclosure = "mpr-enclosure:controller:mpr1:0012"

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.paths, [])
        self.assertEqual(fabric.raw["selected_enclosure_keys"], ["50030480090c4f7f"])
        self.assertEqual(fabric.raw["selected_disk_slots"], [0, 6, 12, 18])
        self.assertIn(mpr0_selected_expander, nodes)
        self.assertIn(mpr1_selected_expander, nodes)
        self.assertIn(mpr0_selected_enclosure, nodes)
        self.assertIn(mpr1_selected_enclosure, nodes)
        self.assertNotIn("expander:controller:mpr0:500304801f715f3f", nodes)
        self.assertNotIn("expander:controller:mpr1:500304801f715fbf", nodes)
        self.assertNotIn("mpr-enclosure:controller:mpr0:0004", nodes)
        self.assertNotIn("mpr-enclosure:controller:mpr1:0014", nodes)
        self.assertEqual(nodes[mpr0_selected_expander].related_slots, bay_slots)
        self.assertEqual(nodes[mpr1_selected_expander].related_slots, bay_slots)
        self.assertEqual(nodes["controller:mpr0"].related_slots, bay_slots)
        self.assertEqual(nodes["controller:mpr1"].related_slots, bay_slots)
        self.assertEqual(nodes[mpr0_selected_enclosure].metrics["selected_disk_count"], 4)
        self.assertIn("bay:0", traces)
        self.assertIn(mpr0_selected_expander, traces["bay:0"].node_ids)
        self.assertIn(mpr1_selected_enclosure, traces["bay:0"].node_ids)
        self.assertIn("controller:mpr0", traces["bay:0"].node_ids)

    def test_empty_non_core_snapshots_report_storage_fabric_boundary(self) -> None:
        platform_kinds = {
            "quantastor": "storage_quantastor",
            "esxi": "storage_esxi",
            "ipmi": "storage_bmc",
        }
        for platform, fabric_kind in platform_kinds.items():
            with self.subTest(platform=platform):
                fabric = build_sas_fabric_snapshot(
                    system=SystemConfig(id=platform, label=platform.upper(), truenas=TrueNASConfig(platform=platform)),
                    snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
                    ssh_outputs={},
                )

                self.assertFalse(fabric.available)
                self.assertEqual(fabric.platform, platform)
                self.assertEqual(fabric.nodes, [])
                self.assertEqual(fabric.links, [])
                self.assertEqual(fabric.traces, [])
                self.assertEqual(fabric.raw["fabric_domain"], "storage_fabric")
                self.assertEqual(fabric.raw["fabric_kind"], fabric_kind)
                self.assertIn("Storage Fabric evidence", fabric.warnings[0])

    def test_scale_snapshot_builds_linux_ses_graph(self) -> None:
        slots = [
            SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                enclosure_id="5003048001c1043f",
                enclosure_label="Front 24 Bay",
                enclosure_name="Front 24 Bay",
                present=True,
                state=SlotState.healthy,
                device_name="sda",
                serial="SCALE0000",
                model="WUH721414AL4204",
                size_bytes=14_000_519_643_136,
                size_human="12.7 TiB (14.0 TB)",
                pool_name="tank",
                vdev_name="raidz2-0",
                vdev_class="data",
                health="ONLINE",
                smart_device_names=["sda"],
                ssh_ses_device="/dev/sg26",
                ssh_ses_element_id=0,
                ssh_ses_targets=[{"ses_device": "/dev/sg26", "ses_element_id": 0}],
                mapping_source="scale_sg_ses",
                raw_status={"ses_device": "/dev/sg26", "enclosure_id": "5003048001c1043f", "sas_device_type": "end device"},
                sg_device="/dev/sg2",
                scsi_hctl="1:0:1:0",
                transport_protocol="sas",
                attached_sas_address="5003048001c1043f",
                phy_identifier="0x0",
                target_port_protocol="SSP",
                logical_unit_id="5000cca264d473d4",
                logical_block_size=512,
                physical_block_size=4096,
            ),
            SlotView(
                slot=1,
                slot_label="01",
                row_index=0,
                column_index=1,
                enclosure_id="5003048001c1043f",
                enclosure_label="Front 24 Bay",
                enclosure_name="Front 24 Bay",
                present=True,
                state=SlotState.healthy,
                device_name="sdb",
                serial="SCALE0001",
                pool_name="tank",
                vdev_name="raidz2-0",
                health="ONLINE",
                ssh_ses_device="/dev/sg26",
                ssh_ses_element_id=1,
                ssh_ses_targets=[{"ses_device": "/dev/sg26", "ses_element_id": 1}],
                mapping_source="scale_sg_ses",
            ),
        ]
        snapshot = InventorySnapshot(
            slots=slots,
            refresh_interval_seconds=30,
            selected_system_id="offsite-scale",
            selected_system_label="Offsite SCALE",
            selected_system_platform="scale",
            selected_enclosure_id="5003048001c1043f",
            selected_enclosure_label="Front 24 Bay",
            selected_enclosure_name="Front 24 Bay",
        )
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="offsite-scale", label="Offsite SCALE", truenas=TrueNASConfig(platform="scale")),
            snapshot=snapshot,
            ssh_outputs={},
            sources={"ssh": SourceStatus(enabled=True, ok=True, message="ok")},
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}
        links = {link.id: link for link in fabric.links}

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.platform, "scale")
        self.assertEqual(fabric.raw["fabric_kind"], "linux_ses")
        self.assertEqual(fabric.raw["ses_devices"], ["/dev/sg26"])
        self.assertIn("controller:linux-ses", nodes)
        self.assertIn("path:linux-ses:sg26", nodes)
        self.assertIn("ses:sg26", nodes)
        self.assertIn("bay:0", traces)
        self.assertIn("ses:sg26", traces["bay:0"].node_ids)
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["path_id"], "path:linux-ses:sg26")
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["sg_device"], "/dev/sg2")
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["scsi_hctl"], "1:0:1:0")
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["transport_protocol"], "sas")
        self.assertEqual(traces["bay:0"].metrics["model"], "WUH721414AL4204")
        self.assertEqual(traces["bay:0"].metrics["serial"], "SCALE0000")
        self.assertEqual(traces["bay:0"].metrics["size_human"], "12.7 TiB (14.0 TB)")
        self.assertEqual(traces["bay:0"].metrics["logical_unit_id"], "5000cca264d473d4")
        self.assertEqual(traces["bay:0"].metrics["vdev_class"], "data")
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["logical_block_size"], 512)
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["smart_device_names"], ["sda"])
        self.assertEqual(nodes["bay:0"].metrics["attached_sas_address"], "5003048001c1043f")
        self.assertEqual(nodes["bay:0"].metrics["phy_identifier"], "0x0")
        self.assertEqual(nodes["bay:0"].metrics["logical_unit_id"], "5000cca264d473d4")
        self.assertEqual(nodes["bay:0"].raw["sas_device_type"], "end device")
        self.assertIn("path-ses-enclosure:path:linux-ses:sg26->ses:sg26", traces["bay:0"].link_ids)
        self.assertIn("ses-bay:ses:sg26->bay:0:0", links)
        self.assertEqual(fabric.controllers[0]["board"], "TrueNAS SCALE Linux SES")
        self.assertEqual(fabric.paths[0]["slots"], [0, 1])
        self.assertIn("SES slot evidence", fabric.warnings[0])
        self.assertNotIn("TrueNAS CORE source data only", " ".join(fabric.warnings))

    def test_scale_snapshot_without_ses_reports_linux_ses_boundary(self) -> None:
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="offsite-scale", label="Offsite SCALE", truenas=TrueNASConfig(platform="scale")),
            snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
            ssh_outputs={},
        )

        self.assertFalse(fabric.available)
        self.assertEqual(fabric.raw["fabric_kind"], "storage_scale")
        self.assertEqual(fabric.raw["fabric_domain"], "storage_fabric")
        self.assertEqual(fabric.nodes, [])
        self.assertIn("Storage Fabric evidence", fabric.warnings[0])

    def test_linux_snapshot_without_ses_builds_storage_fabric_graph(self) -> None:
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="nvme0n1",
            smart_device_names=["/dev/nvme0n1"],
            model="NVMe",
            pool_name="/mnt/nvme_raid",
            vdev_name="md5",
            topology_label="/mnt/nvme_raid > md5 > data",
            raw_status={"device_names": ["nvme0n1", "10000:01:00.0"]},
        )

        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="gpu-linux", label="GPU Server Linux", truenas=TrueNASConfig(platform="linux")),
            snapshot=InventorySnapshot(
                slots=[slot],
                refresh_interval_seconds=30,
                selected_system_id="gpu-linux",
                selected_system_platform="linux",
                selected_enclosure_id="right-nvme-2",
                selected_enclosure_label="Right NVMe 2",
            ),
            ssh_outputs={},
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.platform, "linux")
        self.assertEqual(fabric.raw["fabric_kind"], "storage_linux")
        self.assertEqual(fabric.raw["fabric_domain"], "storage_fabric")
        self.assertIn("controller:linux-nvme", nodes)
        self.assertIn("storage-enclosure:linux-nvme:right-nvme-2", nodes)
        self.assertIn("bay:0", traces)
        self.assertEqual(traces["bay:0"].metrics["path_states"][0]["path_type"], "nvme")
        self.assertNotIn("Linux Linux SES", fabric.warnings[0])

    def test_quantastor_snapshot_builds_storage_fabric_graph(self) -> None:
        slot = SlotView(
            slot=12,
            slot_label="12",
            row_index=1,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="sdam",
            pool_name="HA-Pool-R10",
            vdev_name="mirror-0",
            topology_label="HA-Pool-R10 > mirror-0 > data (active on QSOSN-Right)",
            ssh_ses_device="/dev/sg11",
            ssh_ses_element_id=12,
            ssh_ses_targets=[{"ses_device": "/dev/sg11", "ses_element_id": 12}],
            operator_context={
                "selected_view_label": "QSOSN-Right",
                "pool_owner_label": "QSOSN-Right",
                "fence_owner_label": "QSOSN-Right",
                "visible_on_labels": ["QSOSN-Left", "QSOSN-Right"],
                "io_fencing_enabled": True,
            },
            raw_status={
                "ses_device": "/dev/sg11",
                "quantastor_hw_disk": {"name": "disk12"},
                "quantastor_cli_disk": {"name": "sdam"},
            },
        )

        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="qsosn-ha", label="QSOSN HA", truenas=TrueNASConfig(platform="quantastor")),
            snapshot=InventorySnapshot(
                slots=[slot],
                refresh_interval_seconds=30,
                selected_system_id="qsosn-ha",
                selected_system_platform="quantastor",
                selected_enclosure_id="qsosn-right",
                selected_enclosure_label="QSOSN-Right",
            ),
            ssh_outputs={},
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.raw["fabric_kind"], "storage_quantastor")
        self.assertIn("controller:quantastor-qsosn-right", nodes)
        self.assertIn("ses-enclosure:quantastor-qsosn-right:qsosn-right", nodes)
        self.assertEqual(fabric.controllers[0]["board"], "QSOSN-Right")
        self.assertIn("qs hw-disk-list", nodes["controller:quantastor-qsosn-right"].evidence)
        self.assertEqual(traces["bay:12"].metrics["path_states"][0]["source"], "quantastor")
        self.assertIn("Quantastor Storage Fabric", fabric.warnings[0])

    def test_esxi_snapshot_builds_storage_fabric_graph(self) -> None:
        slot = SlotView(
            slot=2,
            slot_label="02",
            row_index=0,
            column_index=2,
            present=True,
            state=SlotState.healthy,
            device_name="252:2",
            smart_device_names=["naa.5000cca07316765c"],
            pool_name="ESXi local JBOD",
            vdev_name="252:2",
            topology_label="ESXi local Enc > slot 252:2 > direct disk",
            temperature_c=34,
            raw_status={
                "storcli_physical_drive": {
                    "controller_id": "c1",
                    "slot_key": "252:2",
                    "connector_name": "Port 0 - 3 x1",
                    "connected_port": "0(path0)",
                },
                "storcli_enclosure_id": "252",
                "esxi_device_id": "naa.5000cca07316765c",
                "esxi_runtime_name": "vmhba2:C0:T27:L0",
                "esxi_transport": "sas",
            },
        )

        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="esxi-ft-node-2", label="ESXi FT Node 2", truenas=TrueNASConfig(platform="esxi")),
            snapshot=InventorySnapshot(
                slots=[slot],
                refresh_interval_seconds=30,
                selected_system_id="esxi-ft-node-2",
                selected_system_platform="esxi",
                selected_enclosure_id="supermicro-fat-twin-front-6",
                selected_enclosure_label="Front 6",
            ),
            ssh_outputs={},
        )

        nodes = {node.id: node for node in fabric.nodes}
        traces = {trace.id: trace for trace in fabric.traces}

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.raw["fabric_kind"], "storage_esxi")
        self.assertIn("controller:esxi-c1", nodes)
        self.assertIn("storage-enclosure:esxi-c1:252", nodes)
        self.assertEqual(fabric.controllers[0]["board"], "StorCLI c1")
        self.assertIn("StorCLI physical drive", nodes["controller:esxi-c1"].evidence)
        self.assertEqual(traces["bay:2"].metrics["path_states"][0]["path_type"], "storcli-member")

    def test_core_snapshot_carries_structured_command_failures_for_debug_output(self) -> None:
        failure_details = [
            {
                "command": "sudo -n /usr/sbin/mprutil -u 10 show iocfacts",
                "canonical_command": "mprutil -u 10 show iocfacts",
                "controller": "mpr10",
                "context": "sas_fabric_mprutil_iocfacts",
                "context_label": "IOC facts",
                "criticality": "enrichment",
                "exit_code": 1,
                "stderr": "mprutil: Device not configured",
                "stdout": "",
            }
        ]

        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="archive-core", label="The Archive", truenas=TrueNASConfig(platform="core")),
            snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
            ssh_outputs={"sudo -n /usr/sbin/mprutil show adapters": "/dev/mpr10 SAS3816 Broadcom 9500-16e 28.00.00.00"},
            warnings=["Storage Fabric enrichment probes had partial command failures."],
            command_failures=failure_details,
        )

        self.assertTrue(fabric.available)
        self.assertEqual(fabric.raw["command_failures"], failure_details)
        self.assertEqual(fabric.raw["commands"], ["mprutil show adapters"])
        self.assertEqual(fabric.controllers[0]["name"], "mpr10")


class SasFabricInventoryProbeTests(unittest.TestCase):
    @staticmethod
    def _service(platform: str = "core") -> InventoryService:
        service = object.__new__(InventoryService)
        service.system = SystemConfig(id="test", truenas=TrueNASConfig(platform=platform))
        return service

    def test_core_seed_probe_adds_adapter_summary_when_older_config_lacks_it(self) -> None:
        service = self._service()

        commands = service._core_mprutil_seed_probe_commands(
            [SSHCommandResult(command="sudo -n /usr/sbin/sesutil show", ok=True, stdout="")]
        )

        self.assertEqual(commands, ["sudo -n /usr/sbin/mprutil show adapters"])

    def test_core_seed_probe_skips_when_adapter_summary_is_already_configured(self) -> None:
        service = self._service()

        commands = service._core_mprutil_seed_probe_commands(
            [SSHCommandResult(command="/usr/sbin/mprutil show adapters", ok=True, stdout=MPR_ADAPTERS)]
        )

        self.assertEqual(commands, [])

    def test_core_seed_probe_skips_non_core_platforms(self) -> None:
        service = self._service("scale")

        commands = service._core_mprutil_seed_probe_commands([])

        self.assertEqual(commands, [])

    def test_core_dmesg_probe_adds_messages_first_command_with_dmesg_fallback(self) -> None:
        service = self._service()

        commands = service._core_mpr_dmesg_probe_commands([])

        self.assertEqual(commands, [CORE_MPR_DMESG_EVENTS_COMMAND])
        self.assertIn("messages=$({", commands[0])
        self.assertNotIn("messages=$((", commands[0])
        self.assertNotIn("true));", commands[0])
        self.assertIn("/var/log/messages", commands[0])
        self.assertIn(CORE_MESSAGES_TAIL_SUDO_COMMAND, commands[0])
        self.assertIn("dmesg -a", commands[0])
        self.assertEqual(canonicalize_ssh_command(commands[0]), "dmesg mpr events")

    def test_core_dmesg_probe_skips_when_already_collected(self) -> None:
        service = self._service()

        commands = service._core_mpr_dmesg_probe_commands(
            [SSHCommandResult(command=CORE_MPR_DMESG_EVENTS_COMMAND, ok=True, stdout="")]
        )

        self.assertEqual(commands, [])

    def test_core_dmesg_probe_upgrades_legacy_dmesg_only_command(self) -> None:
        service = self._service()

        commands = service._core_mpr_dmesg_probe_commands(
            [
                SSHCommandResult(
                    command="dmesg -a | egrep '^(mpr[0-9]+:|\\(da[0-9]+:mpr[0-9]+:)' | tail -n 400",
                    ok=True,
                    stdout="",
                )
            ]
        )

        self.assertEqual(commands, [CORE_MPR_DMESG_EVENTS_COMMAND])

    def test_core_pci_slot_probe_adds_optional_slot_mapping_commands(self) -> None:
        service = self._service()

        commands = service._core_pci_slot_probe_commands([])

        self.assertEqual(
            commands,
            [
                CORE_PCICONF_LV_OPTIONAL_COMMAND,
                CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND,
                CORE_MPR_SYSCTL_LOCATION_COMMAND,
            ],
        )
        self.assertIn("|| true", commands[0])
        self.assertIn("|| true", commands[1])
        self.assertIn("dev\\.mpr", commands[2])

    def test_core_pci_slot_probe_skips_when_already_collected(self) -> None:
        service = self._service()

        commands = service._core_pci_slot_probe_commands(
            [
                SSHCommandResult(command=CORE_PCICONF_LV_COMMAND, ok=True, stdout=""),
                SSHCommandResult(command=CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND, ok=True, stdout=""),
                SSHCommandResult(command=CORE_MPR_SYSCTL_LOCATION_COMMAND, ok=True, stdout=""),
            ]
        )

        self.assertEqual(commands, [])
