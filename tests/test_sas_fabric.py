import asyncio
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings, SystemConfig, TrueNASConfig
from app.models.domain import (
    InventorySnapshot,
    MultipathMember,
    MultipathView,
    SasFabricAlias,
    SourceStatus,
    SlotState,
    SlotView,
)
from app.services.inventory import InventoryService, InventorySourceBundle
from app.services.parsers import ParsedSSHData, canonicalize_ssh_command
from app.services.sas_diagnostics.decoder import (
    MAX_DIAGNOSTIC_COLLECTION_ITEMS,
    MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH,
    MAX_DIAGNOSTIC_TEXT_LENGTH,
    bound_diagnostic_value,
    finalize_mpr_event_summary,
    new_mpr_event_summary,
    record_mpr_event_summary,
)
from app.services.sas_diagnostics import lsi_loginfo as lsi_loginfo_module
from app.services.sas_diagnostics import scsi as scsi_module
from app.services.sas_diagnostics.common import FAULT_FAMILY_LABELS, fault_family_likely_layer
from app.services.sas_diagnostics.decoder import decode_mpr_dmesg_event
from app.services.sas_diagnostics.scsi import (
    _sense_likely_layer,
    decode_scsi_cdb_message,
    decode_scsi_sense_event,
    decode_scsi_status_value,
)
from app.services.mapping_store import MappingStore
from app.services.profile_registry import ProfileRegistry
from app.services.sas_fabric_alias_store import SasFabricAliasStore
from app.services.slot_detail_store import SlotDetailStore
from app.services.sas_fabric import (
    CORE_DMIDECODE_SLOT_COMMAND,
    CORE_DMIDECODE_SLOT_OPTIONAL_COMMAND,
    CORE_MESSAGES_TAIL_SUDO_COMMAND,
    CORE_MPR_DMESG_EVENTS_COMMAND,
    CORE_MPR_SYSCTL_LOCATION_COMMAND,
    CORE_PCICONF_LV_COMMAND,
    CORE_PCICONF_LV_OPTIONAL_COMMAND,
    _PLATFORM_STORAGE_ROUTE_PROVIDERS,
    _SAS_FABRIC_BUILDERS,
    _add_mpr_infrastructure,
    _build_sas_fabric_context,
    _build_storage_fabric_route_context,
    _device_name_candidates,
    _identifier_lookup_key,
    _lookup_mpr_device_context,
    _slot_location_number_candidates,
    _select_sas_fabric_builder,
    _select_sas_fabric_builder_key,
    _select_storage_fabric_route_provider,
    _scope_mpr_infrastructure,
    build_core_mprutil_unit_commands,
    build_sas_fabric_snapshot,
    discover_mpr_units_from_adapter_summary,
    parse_dmidecode_slots,
    parse_mpr_adapter_detail,
    parse_mpr_adapter_summary,
    parse_mpr_devices,
    parse_mpr_dmesg_events,
    parse_mpr_enclosures,
    parse_mpr_expanders,
    parse_mpr_sysctl_locations,
    parse_pciconf_sas_controllers,
)
from app.services.ssh_probe import SSHCommandResult
from app.services.truenas_ws import TrueNASRawData
from tests.test_inventory import build_inventory_service


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sas_fabric"


def _decoder_fault_families() -> set[str]:
    """Every fault family a diagnostics decoder can stamp on a record."""
    families = set(FAULT_FAMILY_LABELS)
    for classifier in (scsi_module._sense_fault_family, scsi_module._cdb_fault_family, scsi_module.decode_scsi_status_value):
        families.update(re.findall(r'"([a-z_]+)"', " ".join(re.findall(r'(?:return|family =) (.+)', inspect.getsource(classifier)))))
    for name, table in vars(lsi_loginfo_module).items():
        if name.startswith("LSI_SAS_") and isinstance(table, dict):
            for entry in table.values():
                if isinstance(entry, tuple) and len(entry) >= 3 and isinstance(entry[2], str):
                    families.add(entry[2])
    return families


class SasDiagnosticLayerTests(unittest.TestCase):
    # Command/sense classification families, not faults: the fabric-wide
    # default is the intended layer text for them.
    GENERIC_LAYER_FAMILIES = frozenset({"scsi_sense", "scsi_command", "capacity_query", "maintenance"})

    def test_every_decoder_fault_family_has_a_canonical_likely_layer(self) -> None:
        catch_all = fault_family_likely_layer("not_a_fault_family")
        families = _decoder_fault_families()

        # The label table is the family registry; a decoder must not mint a
        # family the table (and therefore the layer map) does not know.
        self.assertEqual(sorted(families - set(FAULT_FAMILY_LABELS)), [])
        self.assertIn("aborted_command", families)
        self.assertIn("device_path_exception", families)

        for family in sorted(families):
            with self.subTest(family=family):
                layer = fault_family_likely_layer(family)
                self.assertEqual(_sense_likely_layer(family), layer)
                if family in self.GENERIC_LAYER_FAMILIES:
                    self.assertEqual(layer, catch_all)
                elif family == "retry":
                    # The retry decoder stamps its layer text on the record itself.
                    self.assertEqual(
                        decode_mpr_dmesg_event({"event_type": "retry"})["likely_layer"],
                        "OS retry path",
                    )
                else:
                    self.assertNotEqual(
                        layer,
                        catch_all,
                        f"{family} falls through to the catch-all layer text",
                    )

    def test_aborted_command_and_device_path_exception_keep_specific_layer_text(self) -> None:
        self.assertEqual(fault_family_likely_layer("aborted_command"), "Target or transport aborted command")
        self.assertEqual(fault_family_likely_layer("device_path_exception"), "Target device or path state")

        sense_abort = decode_scsi_sense_event({"reason": "ABORTED COMMAND", "sense_key": "ABORTED COMMAND"})
        self.assertEqual(sense_abort["family"], "aborted_command")
        self.assertEqual(sense_abort["likely_layer"], "Target or transport aborted command")

        task_aborted = decode_scsi_status_value("TASK ABORTED")
        self.assertEqual(task_aborted["family"], "aborted_command")
        self.assertEqual(task_aborted["likely_layer"], "Target or transport aborted command")

        path_exception = decode_scsi_sense_event({"sense_key": "NOT READY", "asc": "4,0b"})
        self.assertEqual(path_exception["family"], "device_path_exception")
        self.assertEqual(path_exception["likely_layer"], "Target device or path state")

    def test_link_sense_codes_explain_meaning_source_and_limits(self) -> None:
        cases = {
            "4b,4": ("SPL-4 6.20.3.1", "bad CRC"),
            "4b,3": ("SPL-4 6.20.3.1", "1 ms"),
            "4b,6": ("SPL-4 9.2.7.4", "initiator response timeout"),
            "47,3": ("SAT-4 table 162", "Interface CRC Errors"),
        }
        for asc, (clause, phrase) in cases.items():
            with self.subTest(asc=asc):
                decoded = decode_scsi_sense_event({"sense_key": "ABORTED COMMAND", "asc": asc})
                description = decoded["description"]
                self.assertIn(clause, description)
                self.assertIn(phrase, description)
                # Each one says what the code cannot tell the operator.
                self.assertRegex(description, r"does not say (which|where)")
                self.assertNotIn("often path or signal", description)
                self.assertLessEqual(len(description), MAX_DIAGNOSTIC_TEXT_LENGTH)
                self.assertEqual(decoded["decode_source"], "t10_scsi_asc_ascq")

        # Codes without a written explanation keep their family sentence.
        other = decode_scsi_sense_event({"sense_key": "ABORTED COMMAND", "asc": "4b,0"})
        self.assertEqual(
            other["description"],
            "Data phase error is a SAS protocol/link symptom, often path or signal related.",
        )

    def test_mpr_nak_event_carries_the_link_explanation(self) -> None:
        events = [
            decode_mpr_dmesg_event({"event_type": "scsi_sense", "reason": "NAK received", "sense_key": "ABORTED COMMAND", "asc": "4b,4"}),
        ]
        self.assertIn("bad CRC", events[0]["description"])

    def test_read_capacity_commands_are_capacity_queries_not_read_io(self) -> None:
        messages = (
            "READ CAPACITY(10). CDB: 25 00 00 00 00 00 00 00 00 00",
            "SERVICE ACTION IN(16). CDB: 9e 10 00 00 00 00 00 00 00 00 00 00 00 20 00 00",
        )

        for message in messages:
            with self.subTest(message=message):
                decoded = decode_scsi_cdb_message(message)
                self.assertEqual(decoded["family"], "capacity_query")


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


class MprExpanderCascadeTests(unittest.TestCase):
    @staticmethod
    def _expander(handle="000a", parent: str | None = "0001", level: str | None = "1", name="A", enclosure="0002"):
        return {
            "dev_handle": handle, "parent": parent, "sas_level": level,
            "sas_address": f"SANITIZED-EXPANDER-{name}", "enc_handle": enclosure,
            "num_phys": "36", "linked_phys": 0, "phys": [],
        }

    def _infrastructure(self, rows_by_unit):
        nodes, links = {}, {}
        _add_mpr_infrastructure(
            nodes=nodes, links=links,
            controllers=[{"unit": unit, "id": f"controller:mpr{unit}"} for unit in rows_by_unit],
            unit_data={unit: {"expanders": rows} for unit, rows in rows_by_unit.items()},
        )
        return nodes, list(links.values())

    @staticmethod
    def _id(name, unit=0):
        return f"expander:controller:mpr{unit}:SANITIZED-EXPANDER-{name}"

    def test_cascade_uses_parent_handle_in_either_enumeration_order(self):
        parent = self._expander()
        child = self._expander("000b", "000a", "2", "B")
        for rows in ([parent, child], [child, parent]):
            with self.subTest(rows=rows):
                nodes, links = self._infrastructure({0: rows})
                self.assertEqual(
                    {(link.source, link.target, link.kind) for link in links},
                    {("controller:mpr0", self._id("A"), "controller-expander"),
                     (self._id("A"), self._id("B"), "expander-expander")},
                )
                self.assertEqual(nodes[self._id("B")].raw["parent"], "000a")
                self.assertEqual(nodes[self._id("B")].metrics["sas_level"], "2")
                self.assertEqual(nodes[self._id("B")].evidence, ["mprutil -u 0 show expanders"])
                self.assertTrue(all(link.evidence == ["mprutil -u 0 show expanders"] for link in links))

    def test_handles_are_controller_qualified(self):
        _, links = self._infrastructure({
            0: [self._expander(), self._expander("000b", "000a", "2", "B")],
            1: [self._expander(name="C"), self._expander("000b", "000a", "2", "D")],
        })
        self.assertEqual(
            {(link.source, link.target) for link in links if link.kind == "expander-expander"},
            {(self._id("A"), self._id("B")), (self._id("C", 1), self._id("D", 1))},
        )
        _, links = self._infrastructure({0: [self._expander()], 1: [self._expander("000b", "000a", "2", "B")]})
        self.assertFalse(any(link.target == self._id("B", 1) for link in links))

    def test_direct_attached_positive_case_and_case_insensitive_handle(self):
        _, links = self._infrastructure({0: [self._expander(), self._expander("000b", "000A", "2", "B")]})
        self.assertEqual(len(links), 2)
        self.assertIn((self._id("A"), self._id("B")), {(link.source, link.target) for link in links})

    def test_unresolved_and_missing_parent_do_not_fall_back_to_hba(self):
        for parent in ("000f", None, ""):
            with self.subTest(parent=parent):
                nodes, links = self._infrastructure({0: [self._expander("000b", parent, "2", "B")]})
                self.assertEqual(links, [])
                self.assertEqual(nodes[self._id("B")].raw["parent_link_state"], "unknown")

    def test_duplicate_handle_or_address_blocks_ambiguous_edges_and_descendants(self):
        parent = self._expander()
        for duplicate in (dict(parent), self._expander(name="C"), self._expander("000c")):
            for rows in ([parent, duplicate], [duplicate, parent]):
                with self.subTest(rows=rows):
                    _, links = self._infrastructure({0: [*rows, self._expander("000b", "000a", "2", "B")]})
                    self.assertEqual(links, [])

    def test_cycles_self_parent_and_contradictory_levels_have_no_edges(self):
        cases = (
            [self._expander(parent="000a")],
            [self._expander(parent="000b"), self._expander("000b", "000a", "2", "B")],
            [self._expander(), self._expander("000b", "000a", "1", "B")],
            [self._expander(), self._expander("000b", "000a", "3", "B")],
            [self._expander(level="0")],
            [self._expander(level=None)],
            [self._expander(level="9" * 5000)],
        )
        for rows in cases:
            with self.subTest(rows=rows):
                _, links = self._infrastructure({0: rows})
                # An independent valid root is still admitted.
                expected = 1 if len(rows) == 2 and rows[0]["parent"] == "0001" else 0
                self.assertEqual(len(links), expected)

    def test_invalid_ancestor_does_not_certify_downstream_connectivity(self):
        _, links = self._infrastructure({0: [
            self._expander(parent="000f", level="2"),
            self._expander("000b", "000a", "3", "B"),
        ]})
        self.assertEqual(links, [])

    def test_deeper_cascade_is_resolved_without_order_dependence(self):
        _, links = self._infrastructure({0: [
            self._expander("000c", "000b", "3", "C"),
            self._expander("000b", "000a", "2", "B"), self._expander(),
        ]})
        self.assertEqual(len(links), 3)
        self.assertEqual(sum(link.kind == "expander-expander" for link in links), 2)

    def test_public_builder_retains_only_verified_ancestors_of_selected_enclosure(self):
        # Invented identifiers, formatted to the parser's wire shape.
        addresses = {name: f"{number:016x}" for number, name in enumerate("ABC", 10)}
        rows = [
            f"36 {addresses['B']} 000b 000a 0003 2",
            f"36 {addresses['C']} 000c 0001 0004 1",
            f"36 {addresses['A']} 000a 0001 0002 1",
        ]
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=False, state=SlotState.empty)
        snapshot = InventorySnapshot(
            slots=[slot], refresh_interval_seconds=30, selected_system_platform="core",
            selected_enclosure_id=addresses["B"],
        )
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
            snapshot=snapshot,
            ssh_outputs={"mprutil -u 0 show expanders": "\n".join(rows)},
        )
        nodes = {node.id: node for node in fabric.nodes}
        parent_id = f"expander:controller:mpr0:{addresses['A']}"
        child_id = f"expander:controller:mpr0:{addresses['B']}"
        self.assertIn(parent_id, nodes)
        self.assertIn(child_id, nodes)
        self.assertNotIn(f"expander:controller:mpr0:{addresses['C']}", nodes)
        self.assertEqual(nodes[parent_id].raw["fabric_scope"], "unscoped")
        self.assertFalse(nodes[parent_id].raw["selected_enclosure_candidate"])
        self.assertEqual(nodes[child_id].raw["fabric_scope"], "selected_enclosure")
        self.assertEqual(nodes[parent_id].related_slots, [0])
        self.assertIn((parent_id, child_id, "expander-expander"), {(link.source, link.target, link.kind) for link in fabric.links})
        self.assertNotIn(("controller:mpr0", child_id, "controller-expander"), {(link.source, link.target, link.kind) for link in fabric.links})
        self.assertFalse(any(link.kind.endswith("-bay") and link.source == parent_id for link in fabric.links))
        trace = next(trace for trace in fabric.traces if trace.id == "bay:0")
        self.assertIn(parent_id, trace.node_ids)
        trace_links = [link for link in fabric.links if link.id in trace.link_ids]
        self.assertTrue(all(link.source in trace.node_ids and link.target in trace.node_ids for link in trace_links))
        self.assertIn(("controller:mpr0", parent_id), {(link.source, link.target) for link in trace_links})
        # Exercise the shipped trace renderer, not a hand-built graph consumer.
        result = subprocess.run(
            ["node", "-e", """
const fs = require('node:fs');
const {load} = require('./tests/js/fabric_aggregate_provenance.test.js');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const ui = load(data);
process.stdout.write(ui.renderTraceMode(data.fabric));
"""],
            input=json.dumps({"fabric": fabric.model_dump(mode="json"), "snapshot": snapshot.model_dump(mode="json")}),
            text=True, capture_output=True, timeout=20, cwd=Path(__file__).resolve().parents[1], check=True,
        )
        self.assertIn(f'data-fabric-node="{parent_id}"', result.stdout)
        self.assertNotIn(f'data-fabric-node="expander:controller:mpr0:{addresses["C"]}"', result.stdout)

    def test_member_derived_bay_and_path_traces_include_only_their_verified_ancestors(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                rows = [f"36 {10:016x} 000a 0001 0002 1", f"36 {11:016x} 000b 000a 0003 2",
                        f"36 {12:016x} 000c 000a 0004 2"]
                fabric = build_sas_fabric_snapshot(
                    system=SystemConfig(id="synthetic-core", truenas=TrueNASConfig(platform="core")),
                    snapshot=InventorySnapshot(slots=[SlotView(
                        slot=0, slot_label="0", row_index=0, column_index=0, present=True,
                        state=SlotState.healthy, device_name="da0", multipath=MultipathView(
                            name="mpath0", device_name="multipath/disk0", members=[MultipathMember(
                                device_name="da0", state="ACTIVE", controller_label="mpr0")]))],
                        refresh_interval_seconds=30, selected_enclosure_id=f"{11:016x}"),
                    ssh_outputs={
                        "mprutil -u 0 show expanders": "\n".join(rows[::-1] if reverse else rows),
                        "mprutil -u 0 show enclosures": f"60 {11:016x} 0008 0003 SES",
                        "mprutil -u 0 show devices": f"00 0 {20:016x} 0020 000b SAS Target 12.0 0003 0",
                        "camcontrol devlist -v": "scbus12 on mpr0 bus 0:\n<Synthetic disk 0001> at scbus12 target 0 lun 0 (da0,pass0)",
                    },
                )
                for trace in fabric.traces:
                    self.assertIn(f"expander:controller:mpr0:{10:016x}", trace.node_ids)
                    self.assertNotIn(f"expander:controller:mpr0:{12:016x}", trace.node_ids)
                    for link in fabric.links:
                        if link.id in trace.link_ids:
                            self.assertIn(link.source, trace.node_ids)
                            self.assertIn(link.target, trace.node_ids)
                parent = next(node for node in fabric.nodes if node.id == f"expander:controller:mpr0:{10:016x}")
                self.assertFalse(parent.raw["selected_enclosure_candidate"])
                self.assertNotIn("selected_enclosure", parent.metrics)

    def test_public_builder_ancestry_limits_are_inclusive_and_fail_closed(self):
        module = importlib.import_module("app.services.sas_fabric")
        self.assertEqual(module._MAX_MPR_ANCESTRY_MEMBERSHIPS, 16384)
        self.assertEqual(module._MAX_MPR_ANCESTRY_NODES, 4096)
        for node_limit, memberships, limited in ((3, 6, False), (2, 6, True), (3, 5, True)):
            with self.subTest(node_limit=node_limit, memberships=memberships):
                with patch.object(module, "_MAX_MPR_ANCESTRY_NODES", node_limit), patch.object(
                    module, "_MAX_MPR_ANCESTRY_MEMBERSHIPS", memberships,
                ):
                    fabric = build_sas_fabric_snapshot(
                        system=SystemConfig(id="synthetic-core", truenas=TrueNASConfig(platform="core")),
                        snapshot=InventorySnapshot(slots=[SlotView(
                            slot=i, slot_label=str(i), row_index=0, column_index=i, present=False,
                            state=SlotState.empty) for i in range(2)], refresh_interval_seconds=30,
                            selected_enclosure_id=f"{3:016x}"),
                        ssh_outputs={"mprutil -u 0 show expanders": "\n".join([
                            f"36 {1:016x} 0001 ffff 0000 1", f"36 {2:016x} 0002 0001 0000 2",
                            f"36 {3:016x} 0003 0002 0000 3"])},
                    )
                self.assertEqual(bool(fabric.warnings), limited)
                self.assertEqual(sum(node.kind == "expander" for node in fabric.nodes), 1 if limited else 3)
                self.assertEqual(any(link.kind == "expander-expander" for link in fabric.links), not limited)

    def test_public_builder_caps_propagated_memberships_without_partial_ancestry(self):
        # Scaled version of the public finding, not the 65k-node stress input.
        depth, slot_count = 512, 60
        rows = [f"36 {i + 1:016x} {i + 1:04x} {i:04x} 0000 {i + 1}" for i in range(depth)]
        rows[0] = f"36 {1:016x} 0001 ffff 0000 1"
        output = "\n".join(rows)
        from app.services.ssh_probe import MAX_SSH_OUTPUT_BYTES
        self.assertLess(len(output.encode()), MAX_SSH_OUTPUT_BYTES)
        self.assertEqual(len(parse_mpr_expanders(output)), depth)
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", truenas=TrueNASConfig(platform="core")),
            snapshot=InventorySnapshot(
                slots=[SlotView(slot=i, slot_label=str(i), row_index=0, column_index=i,
                                present=False, state=SlotState.empty) for i in range(slot_count)],
                refresh_interval_seconds=30, selected_enclosure_id=f"{depth:016x}",
            ),
            ssh_outputs={"mprutil -u 0 show expanders": output},
        )
        self.assertTrue(any("ancestry" in warning and "limit" in warning for warning in fabric.warnings))
        self.assertFalse(any(link.kind in {"expander-expander", "controller-expander"} for link in fabric.links))
        expanders = [node for node in fabric.nodes if node.kind == "expander"]
        self.assertEqual(len(expanders), 1)
        self.assertEqual(expanders[0].raw["parent_link_state"], "unknown")
        self.assertEqual(expanders[0].raw["parent_link_reason"], "resource_limit")
        self.assertLessEqual(sum(len(node.related_slots) for node in expanders), 16384)
        self.assertLess(len(fabric.model_dump_json()), 250000)

    def test_selected_scope_seed_memberships_are_bounded_before_copying(self):
        module = importlib.import_module("app.services.sas_fabric")
        with patch.object(module, "_MAX_MPR_ANCESTRY_MEMBERSHIPS", 5):
            fabric = build_sas_fabric_snapshot(
                system=SystemConfig(id="synthetic-core", truenas=TrueNASConfig(platform="core")),
                snapshot=InventorySnapshot(slots=[SlotView(
                    slot=i, slot_label=str(i), row_index=0, column_index=i, present=False,
                    state=SlotState.empty) for i in range(2)], refresh_interval_seconds=30,
                    selected_enclosure_id=f"{10:016x}"),
                ssh_outputs={
                    "mprutil -u 0 show expanders": "\n".join(
                        f"36 {i + 1:016x} {i + 1:04x} ffff 000a 1" for i in range(3)),
                    "mprutil -u 0 show enclosures": f"2 {10:016x} 000f 000a SES",
                },
            )
        self.assertTrue(fabric.warnings)
        expanders = [node for node in fabric.nodes if node.kind == "expander"]
        self.assertEqual(len(expanders), 3)
        self.assertTrue(all(node.related_slots == [] for node in expanders))
        self.assertTrue(all(node.raw["parent_link_reason"] == "resource_limit" for node in expanders))
        self.assertFalse(any(link.kind in {"controller-expander", "expander-expander"} for link in fabric.links))
        self.assertTrue(all(not any(node_id.startswith("expander:") for node_id in trace.node_ids)
                            for trace in fabric.traces))

    def test_comb_scope_materializes_slots_a_bounded_number_of_times(self):
        # The real typed-helper reproducer: 128 chain nodes, 128 seeded leaves,
        # and a root. Each leaf has a distinct slot and a distinct chain parent.
        depth = 128
        rows = [self._expander("0001", "ffff", "1", "root")]
        rows.extend(self._expander(f"{i + 2:04x}", f"{i + 1:04x}", str(i + 2), f"chain{i}") for i in range(depth))
        rows.extend(self._expander(f"{i + depth + 2:04x}", f"{i + 2:04x}", str(i + 3), f"leaf{i}") for i in range(depth))
        nodes, link_list = self._infrastructure({0: rows})
        links = {link.id: link for link in link_list}
        for i in range(depth):
            nodes[self._id(f"leaf{i}")].related_slots = [i]
        fabric_module = importlib.import_module("app.services.sas_fabric")
        dedupe = fabric_module._dedupe_ints
        calls = 0

        def counted(values):
            nonlocal calls
            calls += 1
            return dedupe(values)

        with patch.object(fabric_module, "_dedupe_ints", counted):
            _scope_mpr_infrastructure(nodes=nodes, links=links, traces={}, snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30))
        self.assertEqual(nodes[self._id("root")].related_slots, list(range(depth)))
        for i in range(depth):
            self.assertEqual(nodes[self._id(f"chain{i}")].related_slots, list(range(i, depth)))
            self.assertEqual(nodes[self._id(f"leaf{i}")].related_slots, [i])
        self.assertLessEqual(calls, len(nodes) + 2 * len(links) + 10)

    def test_public_builder_accepts_comb_and_deep_chain_with_normal_slots(self):
        from app.services.ssh_probe import MAX_SSH_OUTPUT_BYTES

        for depth, branching in ((128, True), (1100, False)):
            with self.subTest(depth=depth, branching=branching):
                rows = [f"36 {1:016x} 0001 ffff 0000 1"]
                rows.extend(f"36 {i + 2:016x} {i + 2:04x} {i + 1:04x} 0000 {i + 2}" for i in range(depth))
                slots, devices = [], []
                for i in range(depth if branching else 1):
                    leaf = depth + 2 + i
                    parent = i + 2 if branching else depth + 1
                    level = i + 3 if branching else depth + 2
                    rows.append(f"36 {leaf:016x} {leaf:04x} {parent:04x} {leaf:04x} {level}")
                    devices.append(f"00 {i} {leaf + 10000:016x} {leaf + 20000:04x} {leaf:04x} SAS Target 12.0 {leaf:04x} {i}")
                    slots.append(SlotView(
                        slot=i, slot_label=str(i), row_index=0, column_index=i,
                        present=True, state=SlotState.healthy, device_name=f"da{i}",
                        multipath=MultipathView(name=f"mpath{i}", device_name=f"multipath/disk{i}", members=[
                            MultipathMember(device_name=f"da{i}", state="ACTIVE", controller_label="mpr0"),
                        ]),
                    ))
                outputs = {
                    "mprutil -u 0 show expanders": "\n".join(rows),
                    "mprutil -u 0 show devices": "\n".join(devices),
                    "camcontrol devlist -v": "scbus12 on mpr0 bus 0:\n" + "\n".join(
                        f"<Synthetic disk 0001> at scbus12 target {i} lun 0 (da{i},pass{i})" for i in range(len(slots))
                    ),
                }
                for text in outputs.values():
                    self.assertLess(len(text.encode()), MAX_SSH_OUTPUT_BYTES)
                self.assertEqual(len(parse_mpr_expanders(outputs["mprutil -u 0 show expanders"])), len(rows))
                self.assertEqual(len(parse_mpr_devices(outputs["mprutil -u 0 show devices"])), len(slots))
                fabric = build_sas_fabric_snapshot(
                    system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
                    snapshot=InventorySnapshot(slots=slots, refresh_interval_seconds=30, selected_system_platform="core"),
                    ssh_outputs=outputs,
                )
                nodes = {node.id: node for node in fabric.nodes}
                self.assertEqual(sum(node.kind == "expander" for node in nodes.values()), len(rows))
                for i in range(depth + 1):
                    node = nodes[f"expander:controller:mpr0:{i + 1:016x}"]
                    self.assertEqual(node.related_slots, list(range(max(0, i - 1), depth)) if branching else [0])
                    self.assertEqual(node.raw["fabric_scope"], "unscoped")
                    self.assertFalse(node.raw["selected_enclosure_candidate"])
                self.assertFalse(any(link.kind.endswith("-bay") and link.source == f"expander:controller:mpr0:{1:016x}" for link in fabric.links))

    def test_scope_slot_sets_match_ancestor_oracle_for_permuted_forests(self):
        # Every seed subset on a branching forest, multiple roots/controllers,
        # and invalid evidence. Expected ancestry comes only from admitted edges.
        rows = [self._expander(), self._expander("000b", "000a", "2", "B"),
                self._expander("000c", "000a", "2", "C"), self._expander("000d", "000b", "3", "D"),
                self._expander("000e", "0001", "1", "E"), self._expander("000f", "000e", "2", "F"),
                self._expander("0010", "0011", "2", "cycle1"), self._expander("0011", "0010", "3", "cycle2"),
                self._expander("0012", "ffff", "2", "missing"), self._expander("0013", "000a", "9", "badlevel"),
                self._expander("0014", "0001", "1", "duplicate1"), self._expander("0014", "0001", "1", "duplicate2")]
        for reverse in (False, True):
            for mask in range(16):
                with self.subTest(reverse=reverse, mask=mask):
                    nodes, link_list = self._infrastructure({0: rows[::-1] if reverse else rows, 1: rows if reverse else rows[::-1]})
                    links = {link.id: link for link in link_list}
                    parents = {link.target: link.source for link in link_list if link.kind == "expander-expander"}
                    expected = {node_id: set() for node_id in nodes}
                    seeds = [(self._id("B"), [7, 2, 7]), (self._id("D"), [3, 2]),
                             (self._id("F", 1), [11]), (self._id("cycle1"), [9])]
                    initial = {}
                    context = set()
                    for bit, (node_id, slots) in enumerate(seeds):
                        if mask & (1 << bit):
                            nodes[node_id].related_slots = slots[:]
                            initial[node_id] = slots[:]
                            expected[node_id].update(slots)
                            while node_id in parents:
                                node_id = parents[node_id]
                                context.add(node_id)
                                expected[node_id].update(slots)
                    expected_nodes = {
                        node_id: sorted(slots) if node_id in context else initial.get(node_id, [])
                        for node_id, slots in expected.items() if slots
                    }
                    expected_links = {}
                    for link in link_list:
                        if (link.source in nodes and link.source not in expected_nodes) or link.target not in expected_nodes:
                            continue
                        expected_links[link.id] = sorted(expected.get(link.source, set()) | expected[link.target]) if link.source in context or link.target in context else []
                    _scope_mpr_infrastructure(nodes=nodes, links=links, traces={}, snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30))
                    self.assertEqual({node_id: node.related_slots for node_id, node in nodes.items()}, expected_nodes)
                    self.assertEqual({link_id: link.related_slots for link_id, link in links.items()}, expected_links)
                    self.assertTrue(all(not node.raw["selected_enclosure_candidate"] for node in nodes.values()))
                    self.assertTrue(all(node.raw["fabric_scope"] == "unscoped" for node in nodes.values()))


class MprLinkReportedLaneTests(unittest.TestCase):
    """Reported lanes on parent links come only from reciprocal PHY rows (#937)."""

    # Invented addresses, built at runtime in the sanitized 5000c500... form.
    ADDRESS_A = f"5000c500{0xa1:08x}"
    ADDRESS_B = f"5000c500{0xb1:08x}"
    ADDRESS_C = f"5000c500{0xc1:08x}"

    # HBA phy -> expander A phy. Deliberately not an identity or monotonic map.
    HBA_TO_A = {0: 11, 1: 10, 2: 9, 3: 8}
    # Expander A phy -> expander B phy.
    A_TO_B = {12: 4, 13: 5, 14: 6, 15: 7}

    @staticmethod
    def _hba_row(phy, device_handle="0009", controller_handle="0001", speed="12.0"):
        return f"{phy} {controller_handle} {device_handle} N {speed} 3.0 12.0 SAS Initiator"

    @staticmethod
    def _expander_row(phy, remote, handle, device, speed="12.0"):
        return f"  {phy} {remote} {handle} {speed} 1.5 12.0 {device}"

    def _hba_rows(self):
        rows = [self._hba_row(phy) for phy in sorted(self.HBA_TO_A)]
        rows.extend(f"{phy} 0000 0000 N 0 3.0 12.0 No Device" for phy in range(4, 8))
        return rows

    def _a_rows(self):
        rows = [self._expander_row(local, hba, "0001", "SAS Initiator") for hba, local in self.HBA_TO_A.items()]
        rows.extend(self._expander_row(local, remote, "000a", "Edge Expander") for local, remote in self.A_TO_B.items())
        return rows

    def _b_rows(self):
        return [self._expander_row(local, remote, "0009", "Edge Expander") for remote, local in self.A_TO_B.items()]

    def _outputs(self, *, hba_rows=None, a_rows=None, b_rows=None, extra_expanders=()):
        adapter = "\n".join(["mpr0 Adapter:", "  Board Name: Synthetic 9300-8e", *(self._hba_rows() if hba_rows is None else hba_rows)])
        expanders = [
            "Num Phys SAS Address      DevHandle Parent EncHandle SAS Level",
            f"36 {self.ADDRESS_A} 0009 0001 0002 1",
            *(self._a_rows() if a_rows is None else a_rows),
            f"36 {self.ADDRESS_B} 000a 0009 0003 2",
            *(self._b_rows() if b_rows is None else b_rows),
        ]
        for header, rows in extra_expanders:
            expanders.extend([header, *rows])
        return {
            "sudo -n /usr/sbin/mprutil show adapters": "/dev/mpr0 SAS3008 Synthetic 16.00.12.00",
            "sudo -n /usr/sbin/mprutil -u 0 show adapter": adapter,
            "sudo -n /usr/sbin/mprutil -u 0 show expanders": "\n".join(expanders),
        }

    def _fabric(self, outputs=None):
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=False, state=SlotState.empty)
        snapshot = InventorySnapshot(
            slots=[slot], refresh_interval_seconds=30, selected_system_platform="core",
            selected_enclosure_id=self.ADDRESS_B,
        )
        return build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
            snapshot=snapshot,
            ssh_outputs=self._outputs() if outputs is None else outputs,
        )

    def _parent_links(self, fabric):
        expander_a = f"expander:controller:mpr0:{self.ADDRESS_A}"
        expander_b = f"expander:controller:mpr0:{self.ADDRESS_B}"
        links = {(link.source, link.target, link.kind): link for link in fabric.links}
        return (links[("controller:mpr0", expander_a, "controller-expander")],
                links[(expander_a, expander_b, "expander-expander")])

    def test_consistent_reciprocal_rows_populate_reported_lanes(self) -> None:
        hba_link, cascade_link = self._parent_links(self._fabric())

        hba = hba_link.reported_lanes
        self.assertIsNotNone(hba)
        self.assertEqual(hba.basis, "reciprocal_reported_phy_rows")
        self.assertEqual(hba.lane_count, 4)
        self.assertEqual(hba.source_phys, [0, 1, 2, 3])
        self.assertEqual(hba.target_phys, [8, 9, 10, 11])
        self.assertEqual(
            [(lane.source_phy, lane.target_phy, lane.negotiated_rate_gbps) for lane in hba.lanes],
            [(0, 11, 12.0), (1, 10, 12.0), (2, 9, 12.0), (3, 8, 12.0)],
        )
        self.assertEqual(hba.negotiated_rate_gbps, 12.0)
        self.assertIsNone(hba.source_sas_address)
        self.assertEqual(hba.attached_sas_address, self.ADDRESS_A)
        self.assertEqual(hba.evidence, ["mprutil -u 0 show adapter", "mprutil -u 0 show expanders"])

        cascade = cascade_link.reported_lanes
        self.assertIsNotNone(cascade)
        self.assertEqual(cascade.lane_count, 4)
        self.assertEqual(cascade.source_phys, [12, 13, 14, 15])
        self.assertEqual(cascade.target_phys, [4, 5, 6, 7])
        self.assertEqual([(lane.source_phy, lane.target_phy) for lane in cascade.lanes], sorted(self.A_TO_B.items()))
        self.assertEqual(cascade.negotiated_rate_gbps, 12.0)
        self.assertEqual(cascade.source_sas_address, self.ADDRESS_A)
        self.assertEqual(cascade.attached_sas_address, self.ADDRESS_B)
        self.assertEqual(cascade.evidence, ["mprutil -u 0 show expanders"])
        # Existing link fields keep their values.
        self.assertEqual(hba_link.evidence, ["mprutil -u 0 show expanders"])
        self.assertEqual(cascade_link.evidence, ["mprutil -u 0 show expanders"])

    def test_reported_lanes_do_not_depend_on_row_order(self) -> None:
        expected = [link.reported_lanes for link in self._parent_links(self._fabric())]
        reordered = self._fabric(self._outputs(
            hba_rows=self._hba_rows()[::-1], a_rows=self._a_rows()[::-1], b_rows=self._b_rows()[::-1],
        ))
        self.assertEqual([link.reported_lanes for link in self._parent_links(reordered)], expected)

    def test_controller_preserves_parsed_hba_phy_rows(self) -> None:
        fabric = self._fabric()
        outputs = self._outputs()
        parsed = parse_mpr_adapter_detail(outputs["sudo -n /usr/sbin/mprutil -u 0 show adapter"])["phy_rows"]
        controller = next(node for node in fabric.nodes if node.id == "controller:mpr0")
        self.assertEqual(len(parsed), 8)
        self.assertEqual(controller.raw["phy_rows"], parsed)
        self.assertEqual(fabric.controllers[0]["phy_rows"], parsed)
        # The pre-existing count metrics are unchanged.
        self.assertEqual(controller.metrics["phy_count"], 8)
        self.assertEqual(controller.metrics["linked_phy_count"], 4)

    def test_missing_endpoint_evidence_leaves_reported_lanes_null(self) -> None:
        outputs = self._outputs()
        outputs.pop("sudo -n /usr/sbin/mprutil -u 0 show adapter")
        hba_link, cascade_link = self._parent_links(self._fabric(outputs))
        self.assertIsNone(hba_link.reported_lanes)
        self.assertIsNotNone(cascade_link.reported_lanes)
        self.assertEqual(next(node for node in self._fabric(outputs).nodes if node.id == "controller:mpr0").raw["phy_rows"], [])

        cases = {
            "no expander rows toward the HBA": dict(a_rows=self._a_rows()[4:]),
            "no child rows toward the parent": dict(b_rows=[]),
            "no parent rows toward the child": dict(a_rows=self._a_rows()[:4]),
            "HBA phys not attached to the expander": dict(hba_rows=[self._hba_row(phy, "0000") for phy in range(4)]),
        }
        expected = {
            "no expander rows toward the HBA": (None, "set"),
            "no child rows toward the parent": ("set", None),
            "no parent rows toward the child": ("set", None),
            "HBA phys not attached to the expander": (None, "set"),
        }
        for name, overrides in cases.items():
            with self.subTest(case=name):
                links = self._parent_links(self._fabric(self._outputs(**overrides)))
                for link, state in zip(links, expected[name], strict=True):
                    if state is None:
                        self.assertIsNone(link.reported_lanes)
                    else:
                        self.assertIsNotNone(link.reported_lanes)

    def test_contradictory_endpoint_evidence_leaves_reported_lanes_null(self) -> None:
        a_rows = self._a_rows()
        b_rows = self._b_rows()
        hba_rows = self._hba_rows()
        hba_cases = {
            "expander reports fewer HBA lanes": dict(a_rows=a_rows[1:]),
            "HBA reports fewer lanes than the expander": dict(hba_rows=hba_rows[1:]),
            "expander names a different HBA phy": dict(
                a_rows=[self._expander_row(11, 5, "0001", "SAS Initiator"), *a_rows[1:]]),
            "expander names one HBA phy twice": dict(
                a_rows=[self._expander_row(11, 1, "0001", "SAS Initiator"), *a_rows[1:]]),
            "HBA lane uses another controller handle": dict(
                hba_rows=[self._hba_row(0, controller_handle="0002"), *hba_rows[1:]]),
            "duplicate HBA phy row": dict(hba_rows=[*hba_rows, self._hba_row(0, "0000")]),
            "malformed HBA device handle": dict(hba_rows=[self._hba_row(0, "zz"), *hba_rows[1:]]),
            "another expander claims an HBA phy": dict(extra_expanders=[(
                f"36 {self.ADDRESS_C} 000c 0001 0004 1",
                [self._expander_row(0, 0, "0001", "SAS Initiator")],
            )]),
            # Adapter rows never count as claims, so a valid HBA-linked child phy
            # has zero; another expander attaching to expander A phy 11 (HBA phy
            # 0's lane) contradicts the HBA link.
            "another expander claims the child's HBA-linked phy": dict(extra_expanders=[(
                f"36 {self.ADDRESS_C} 000c 0009 0004 2",
                [self._expander_row(0, 11, "0009", "Edge Expander")],
            )]),
        }
        cascade_cases = {
            "child names a different parent phy": dict(
                b_rows=[self._expander_row(4, 20, "0009", "Edge Expander"), *b_rows[1:]]),
            "child reports fewer lanes": dict(b_rows=b_rows[1:]),
            "parent and child pairings disagree": dict(
                b_rows=[self._expander_row(4, 13, "0009", "Edge Expander"),
                        self._expander_row(5, 12, "0009", "Edge Expander"), *b_rows[2:]]),
            "child row is not an expander attachment": dict(
                b_rows=[self._expander_row(4, 12, "0009", "End Device"), *b_rows[1:]]),
            "child row lacks a parseable remote phy": dict(b_rows=["  4 0009 12.0 1.5 12.0 Edge Expander", *b_rows[1:]]),
            "another expander claims a parent phy": dict(extra_expanders=[(
                f"36 {self.ADDRESS_C} 000c 0009 0004 2",
                [self._expander_row(0, 12, "0009", "Edge Expander")],
            )]),
        }
        for name, overrides in hba_cases.items():
            with self.subTest(case=name):
                hba_link, _ = self._parent_links(self._fabric(self._outputs(**overrides)))
                self.assertIsNone(hba_link.reported_lanes)
        for name, overrides in cascade_cases.items():
            with self.subTest(case=name):
                fabric = self._fabric(self._outputs(**overrides))
                _, cascade_link = self._parent_links(fabric)
                self.assertIsNone(cascade_link.reported_lanes)
        with self.subTest(case="reused local expander phy"):
            duplicate = self._fabric(self._outputs(a_rows=[*a_rows, self._expander_row(8, 3, "0001", "SAS Initiator")]))
            self.assertEqual([link.reported_lanes for link in self._parent_links(duplicate)], [None, None])

    def test_disagreeing_rates_keep_pairing_but_no_guessed_rate(self) -> None:
        hba_rows = [self._hba_row(0, speed="6.0"), *self._hba_rows()[1:]]
        hba_link, _ = self._parent_links(self._fabric(self._outputs(hba_rows=hba_rows)))
        lanes = hba_link.reported_lanes
        self.assertIsNotNone(lanes)
        self.assertEqual(lanes.lane_count, 4)
        self.assertIsNone(lanes.negotiated_rate_gbps)
        self.assertEqual([lane.negotiated_rate_gbps for lane in lanes.lanes], [None, 12.0, 12.0, 12.0])
        # A uniform but different rate on every lane is reported as observed.
        hba_rows = [self._hba_row(phy, speed="6.0") for phy in range(4)]
        a_rows = [self._expander_row(local, hba, "0001", "SAS Initiator", speed="6") for hba, local in self.HBA_TO_A.items()]
        hba_link, _ = self._parent_links(self._fabric(self._outputs(hba_rows=hba_rows, a_rows=[*a_rows, *self._a_rows()[4:]])))
        self.assertEqual(hba_link.reported_lanes.negotiated_rate_gbps, 6.0)

    def test_infrastructure_tolerates_non_text_phy_evidence(self) -> None:
        malformed_values = ([], {}, 7, None, True, "9" * 5000, "-1")
        for value in malformed_values:
            with self.subTest(value=value):
                parent = {"dev_handle": "0009", "parent": "0001", "sas_level": "1", "sas_address": self.ADDRESS_A,
                          "enc_handle": "0002", "phys": [{"phy": "8", "remote_phy": value, "dev_handle": "0001",
                                                          "speed": "12.0", "device": "SAS Initiator"}]}
                child = {"dev_handle": "000a", "parent": "0009", "sas_level": "2", "sas_address": self.ADDRESS_B,
                         "enc_handle": "0003", "phys": [{"phy": value, "remote_phy": "12", "dev_handle": value,
                                                         "speed": value, "device": value}]}
                adapter = {"phy_rows": [{"phy": value, "controller_handle": value, "device_handle": value, "speed": value}]}
                links: dict = {}
                _add_mpr_infrastructure(
                    nodes={}, links=links, controllers=[{"unit": 0, "id": "controller:mpr0"}],
                    unit_data={0: {"adapter_detail": adapter, "expanders": [parent, child]}},
                )
                self.assertEqual(len(links), 2)
                self.assertTrue(all(link.reported_lanes is None for link in links.values()))

    def test_existing_fixture_links_gain_only_a_null_reported_lanes_field(self) -> None:
        enclosure_id = parse_mpr_enclosures(MPR0_ENCLOSURES)[0]["logical_id"]
        snapshot = InventorySnapshot(
            slots=[SlotView(
                slot=0, slot_label="00", row_index=0, column_index=0, present=True,
                state=SlotState.healthy, device_name="da60",
                raw_status={"enclosure_id": enclosure_id, "ses_slot_number": 1},
                multipath=MultipathView(
                    name="mpath0", device_name="multipath/disk0",
                    members=[MultipathMember(device_name="da60", state="ACTIVE", controller_label="mpr0")],
                ),
            )],
            refresh_interval_seconds=30, selected_system_platform="core", selected_enclosure_id=enclosure_id,
        )
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
            snapshot=snapshot,
            ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                "sudo -n /usr/sbin/mprutil -u 0 show adapter": MPR0_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 0 show expanders": MPR0_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 0 show enclosures": MPR0_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 0 show devices": MPR0_DEVICES,
            },
        )
        payload = fabric.model_dump(mode="json")
        self.assertTrue(any(link["kind"] == "controller-expander" for link in payload["links"]))
        for link in payload["links"]:
            self.assertEqual(
                set(link),
                {"id", "source", "target", "kind", "status", "slot", "related_slots", "evidence", "reported_lanes"},
            )
            # The fixture's HBA phys name disk handles, not the expander: no lanes.
            self.assertIsNone(link["reported_lanes"])
        controller = next(node for node in payload["nodes"] if node["id"] == "controller:mpr0")
        self.assertEqual(controller["raw"]["phy_rows"], parse_mpr_adapter_detail(MPR0_ADAPTER)["phy_rows"])
        self.assertEqual(controller["metrics"]["phy_count"], 2)

    def test_reported_lanes_survive_strict_snapshot_round_trip(self) -> None:
        from app.models.domain import SasFabricSnapshot

        fabric = self._fabric()
        restored = SasFabricSnapshot.model_validate_json(fabric.model_dump_json(), strict=True)
        self.assertEqual(restored.model_dump(mode="json"), fabric.model_dump(mode="json"))
        hba_link = next(link for link in restored.links if link.kind == "controller-expander")
        self.assertEqual(hba_link.reported_lanes.lane_count, 4)


class SasFabricParserTests(unittest.TestCase):
    def test_focused_parser_module_owns_public_text_parsers_and_fabric_reexports_them(self) -> None:
        parser_module = importlib.import_module("app.services.sas_fabric_parsers")
        fabric_module = importlib.import_module("app.services.sas_fabric")
        parser_names = (
            "parse_mpr_adapter_summary",
            "parse_mpr_adapter_detail",
            "parse_mpr_devices",
            "parse_mpr_enclosures",
            "parse_mpr_expanders",
            "parse_mpr_iocfacts",
            "parse_pciconf_sas_controllers",
            "parse_dmidecode_slots",
            "parse_mpr_sysctl_locations",
            "parse_mpr_dmesg_events",
            "discover_mpr_units_from_adapter_summary",
            "build_core_mprutil_unit_commands",
            "_split_mpr_dmesg_timestamp",
            "_strip_syslog_kernel_prefix",
            "_mpr_dmesg_event_type",
            "_mpr_dmesg_severity",
            "_parse_mpr_sense_message",
            "_pciconf_bus_address",
            "_freebsd_pci_location_to_address",
            "_mpr_unit_from_text",
        )

        for name in parser_names:
            with self.subTest(name=name):
                parser = getattr(parser_module, name)
                self.assertIs(getattr(fabric_module, name), parser)
                self.assertEqual(parser.__module__, "app.services.sas_fabric_parsers")

        self.assertIs(fabric_module.CORE_MPRUTIL_UNIT_SUBCOMMANDS, parser_module.CORE_MPRUTIL_UNIT_SUBCOMMANDS)

    def test_device_name_candidates_ignore_empty_split_tokens(self) -> None:
        self.assertEqual(_device_name_candidates("/dev/da0"), ["da0", "/dev/da0"])
        self.assertEqual(_device_name_candidates("(da0)"), ["da0"])

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

    def test_core_snapshot_parses_pci_inventory_sources_once(self) -> None:
        system = SystemConfig(
            id="archive-core",
            label="The Archive",
            truenas=TrueNASConfig(platform="core"),
        )
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            selected_system_id="archive-core",
            selected_system_label="The Archive",
            selected_system_platform="core",
        )

        with (
            patch(
                "app.services.sas_fabric.parse_pciconf_sas_controllers",
                wraps=parse_pciconf_sas_controllers,
            ) as parse_pciconf,
            patch(
                "app.services.sas_fabric.parse_dmidecode_slots",
                wraps=parse_dmidecode_slots,
            ) as parse_dmidecode,
            patch(
                "app.services.sas_fabric.parse_mpr_sysctl_locations",
                wraps=parse_mpr_sysctl_locations,
            ) as parse_sysctl,
        ):
            fabric = build_sas_fabric_snapshot(
                system=system,
                snapshot=snapshot,
                ssh_outputs={
                    CORE_PCICONF_LV_COMMAND: PCICONF_MPR_OUTPUT,
                    CORE_DMIDECODE_SLOT_COMMAND: DMIDECODE_SLOT_OUTPUT,
                    CORE_MPR_SYSCTL_LOCATION_COMMAND: MPR_SYSCTL_LOCATIONS,
                },
            )

        self.assertEqual(parse_pciconf.call_count, 1)
        self.assertEqual(parse_dmidecode.call_count, 1)
        self.assertEqual(parse_sysctl.call_count, 1)
        self.assertEqual(fabric.raw["pci_controllers"][0]["pci_address"], "0000:82:00.0")
        self.assertEqual(fabric.raw["pcie_slots"][0]["designation"], "CPU2 SLOT1 PCI-E 3.0 X8")
        self.assertEqual(fabric.raw["mpr_sysctl_locations"]["mpr1"]["pci_parent"], "pci16")

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
        records = target["event_table"]["rows"]
        ioc_record = next(record for record in records if record["event_type"] == "ioc_terminated")
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
        cdb_record = next(record for record in records if record["event_type"] == "cdb")
        self.assertEqual(cdb_record["operation"], "WRITE(16)")
        self.assertEqual(cdb_record["decode_confidence"], "standard")
        self.assertEqual(cdb_record["decode_source"], "t10_scsi_operation_codes")
        self.assertEqual(cdb_record["source_attribution"]["url"], "https://www.t10.org/lists/op-num.htm")
        self.assertEqual(cdb_record["direction"], "write")
        self.assertEqual(cdb_record["lba"], 6264052064)
        self.assertEqual(cdb_record["transfer_blocks"], 248)
        sense_record = next(record for record in records if record["event_type"] == "scsi_sense")
        self.assertEqual(sense_record["family"], "sas_protocol")
        self.assertEqual(sense_record["decode_confidence"], "standard")
        self.assertEqual(sense_record["decode_source"], "t10_scsi_asc_ascq")
        self.assertEqual(sense_record["source_attribution"]["url"], "https://www.t10.org/lists/asc-num.htm")
        self.assertEqual(sense_record["likely_layer"], "SAS link/path")
        self.assertEqual(target["event_table"]["total_count"], 5)
        self.assertEqual(target["event_table"]["page_size"], 25)
        self.assertEqual(target["event_table"]["rows"][0]["event_id"], "mpr-dmesg-0001")

    def test_mpr_event_tables_ship_only_a_bounded_recent_sample(self) -> None:
        events = parse_mpr_dmesg_events(
            "\n".join(
                f"mpr0: Controller reported scsi ioc terminated tgt 180 SMID {index} loginfo 31120302"
                for index in range(40)
            )
        )

        for summary in (
            events["by_controller"]["mpr0"],
            events["by_controller_target"]["mpr0:180"],
        ):
            table = summary["event_table"]
            self.assertNotIn("decoded_records", summary)
            self.assertEqual(table["total_count"], 40)
            self.assertEqual(table["sample_count"], 25)
            self.assertEqual(table["sample_limit"], 25)
            self.assertEqual(table["sample_kind"], "recent")
            self.assertTrue(table["truncated"])
            self.assertEqual(len(table["rows"]), 25)
            self.assertEqual(table["rows"][0]["event_id"], "mpr-dmesg-0016")
            self.assertEqual(table["rows"][-1]["event_id"], "mpr-dmesg-0040")

    def test_mpr_event_tables_bound_controller_device_and_target_scopes(self) -> None:
        events = parse_mpr_dmesg_events(
            "\n".join(
                f"(da1:mpr0:0:180:0): Retrying command event-{index}"
                for index in range(40)
            )
        )

        for summary in (
            events["by_controller"]["mpr0"],
            events["by_device"]["da1"],
            events["by_controller_target"]["mpr0:180"],
        ):
            table = summary["event_table"]
            self.assertEqual(table["total_count"], 40)
            self.assertEqual(table["sample_count"], 25)
            self.assertTrue(table["truncated"])
            self.assertEqual(table["rows"][0]["event_id"], "mpr-dmesg-0016")
            self.assertEqual(table["rows"][-1]["event_id"], "mpr-dmesg-0040")

    def test_mpr_event_table_boundaries_report_sample_semantics(self) -> None:
        empty = parse_mpr_dmesg_events("")
        self.assertEqual(empty["event_count"], 0)
        self.assertEqual(empty["by_controller"], {})

        for event_count, expected_first, expected_truncated in (
            (25, "mpr-dmesg-0001", False),
            (26, "mpr-dmesg-0002", True),
        ):
            with self.subTest(event_count=event_count):
                events = parse_mpr_dmesg_events(
                    "\n".join(
                        f"mpr0: Controller reported scsi ioc terminated tgt 180 SMID {index} loginfo 31120302"
                        for index in range(event_count)
                    )
                )
                table = events["by_controller"]["mpr0"]["event_table"]

                self.assertEqual(table["total_count"], event_count)
                self.assertEqual(table["sample_count"], min(event_count, 25))
                self.assertEqual(table["sample_limit"], 25)
                self.assertEqual(table["sample_kind"], "recent")
                self.assertEqual(table["truncated"], expected_truncated)
                self.assertEqual(table["rows"][0]["event_id"], expected_first)
                self.assertEqual(table["rows"][-1]["event_id"], f"mpr-dmesg-{event_count:04d}")

    def test_mpr_event_payload_bounds_oversized_text(self) -> None:
        oversized_suffix = "X" * 8_000
        events = parse_mpr_dmesg_events(
            "\n".join(
                f"(da{index % 32}:mpr0:0:{index % 64}:0): Retrying command {oversized_suffix}"
                for index in range(400)
            )
        )

        serialized = json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")

        self.assertLessEqual(len(serialized), 2 * 1024 * 1024)
        for scope_name in ("by_controller", "by_device", "by_controller_target"):
            for summary in events[scope_name].values():
                for row in summary["event_table"]["rows"]:
                    self.assertLessEqual(len(str(row.get("message") or "")), MAX_DIAGNOSTIC_TEXT_LENGTH)
                    self.assertLessEqual(len(str(row.get("raw_line") or "")), MAX_DIAGNOSTIC_TEXT_LENGTH)
                    self.assertLessEqual(len(str(row.get("label") or "")), MAX_DIAGNOSTIC_TEXT_LENGTH)

    def test_diagnostic_value_bounds_nested_collection_cardinality(self) -> None:
        bounded = bound_diagnostic_value({
            "items": [f"item-{index}" for index in range(100)],
            "mapping": {f"key-{index}": index for index in range(100)},
        })

        self.assertEqual(len(bounded["items"]), MAX_DIAGNOSTIC_COLLECTION_ITEMS)
        self.assertEqual(len(bounded["mapping"]), MAX_DIAGNOSTIC_COLLECTION_ITEMS)

    def test_core_snapshot_serializes_one_bounded_controller_event_sample(self) -> None:
        event_output = "\n".join(
            f"mpr0: Controller reported scsi ioc terminated tgt 180 SMID {index} loginfo 31120302"
            for index in range(40)
        )
        system = SystemConfig(
            id="archive-core",
            label="The Archive",
            truenas=TrueNASConfig(platform="core"),
        )
        snapshot = InventorySnapshot(
            slots=[],
            refresh_interval_seconds=30,
            selected_system_id="archive-core",
            selected_system_label="The Archive",
            selected_system_platform="core",
        )

        fabric = build_sas_fabric_snapshot(
            system=system,
            snapshot=snapshot,
            ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                CORE_MPR_DMESG_EVENTS_COMMAND: event_output,
            },
        )

        controller = next(item for item in fabric.controllers if item["name"] == "mpr0")
        controller_node = next(node for node in fabric.nodes if node.id == "controller:mpr0")
        diagnostics = controller_node.metrics["kernel_diagnostics"]
        serialized = json.loads(fabric.model_dump_json())
        serialized_text = json.dumps(serialized, sort_keys=True)
        self.assertNotIn("kernel_diagnostics", controller)
        self.assertNotIn("kernel_diagnostics", controller_node.raw)
        self.assertNotIn('"decoded_records"', serialized_text)
        self.assertEqual(diagnostics["event_table"]["total_count"], 40)
        self.assertEqual(len(diagnostics["event_table"]["rows"]), 25)
        self.assertEqual(serialized_text.count('"event_id": "mpr-dmesg-0040"'), 1)

    def test_slot_location_candidates_do_not_guess_shifted_alias(self) -> None:
        slot = SlotView(
            slot=5,
            slot_label="05",
            row_index=0,
            column_index=5,
            present=True,
            state=SlotState.healthy,
            device_name="da5",
            raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": 5},
        )

        self.assertEqual(_slot_location_number_candidates(slot), [5])

    def test_mpr_location_lookup_returns_the_exact_bay_not_its_neighbour(self) -> None:
        enclosure_key = _identifier_lookup_key("50030480090c4f7f")
        assert enclosure_key is not None
        trace_index = {
            "devices": {},
            "devices_by_location": {
                ("mpr0", enclosure_key, 4): {"dev_handle": "0x0010", "slot": "4"},
                ("mpr0", enclosure_key, 5): {"dev_handle": "0x0011", "slot": "5"},
            },
        }
        slot = SlotView(
            slot=5,
            slot_label="05",
            row_index=0,
            column_index=5,
            present=True,
            state=SlotState.healthy,
            device_name="da5",
            raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": 5},
        )

        context = _lookup_mpr_device_context(
            trace_index,
            "mpr0",
            "da5",
            slot_enclosure_ids=["50030480090c4f7f"],
            slot_location_numbers=_slot_location_number_candidates(slot),
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context["dev_handle"], "0x0011")

    def test_core_snapshot_bounds_controller_and_mapped_member_diagnostics(self) -> None:
        oversized_suffix = "X" * 8_000
        event_output = "\n".join(
            f"(da60:mpr0:0:180:0): Retrying command {oversized_suffix}"
            for _index in range(400)
        )
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="da60",
            raw_status={"enclosure_id": "50030480090c4f7f", "ses_slot_number": 1},
            multipath=MultipathView(
                name="mpath0",
                device_name="multipath/disk0",
                members=[MultipathMember(device_name="da60", state="FAIL", controller_label="mpr0")],
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
                CORE_MPR_DMESG_EVENTS_COMMAND: event_output,
            },
        )

        serialized = fabric.model_dump(mode="json")
        event_tables: list[dict[str, object]] = []

        def collect_event_tables(value: object) -> None:
            if isinstance(value, dict):
                table = value.get("event_table")
                if isinstance(table, dict):
                    event_tables.append(table)
                for nested in value.values():
                    collect_event_tables(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_event_tables(nested)

        collect_event_tables(serialized)
        serialized_bytes = json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode("utf-8")

        self.assertEqual(len(event_tables), 2)
        for table in event_tables:
            self.assertEqual(table["total_count"], 400)
            rows = table["rows"]
            self.assertIsInstance(rows, list)
            assert isinstance(rows, list)
            self.assertEqual(len(rows), 25)
        self.assertNotIn(b"decoded_records", serialized_bytes)
        self.assertLessEqual(len(serialized_bytes), 256 * 1024)

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
            for record in controller["event_table"]["rows"]
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

    # Pin application severity independently of the decoder's label/code tables.
    SCSI_STATUS_CASES = (
        (0x00, "GOOD", "info"),
        (0x02, "CHECK CONDITION", "warning"),
        (0x04, "CONDITION MET", "info"),
        (0x08, "BUSY", "warning"),
        (0x18, "RESERVATION CONFLICT", "warning"),
        (0x28, "TASK SET FULL", "warning"),
        (0x30, "ACA ACTIVE", "warning"),
        (0x40, "TASK ABORTED", "error"),
    )

    def test_scsi_status_rows_have_canonical_severity_for_all_supported_forms(self) -> None:
        self.assertEqual(dict((code, text) for code, text, _ in self.SCSI_STATUS_CASES), scsi_module.SCSI_STATUS_CODES)
        self.assertEqual({text for _, text, _ in self.SCSI_STATUS_CASES}, set(scsi_module.SCSI_STATUS_LABELS))
        for code, text, severity in self.SCSI_STATUS_CASES:
            forms = (text, text.lower(), text.replace(" ", "_"), text.replace(" ", "-"),
                     f"0x{code:02x}", f"{code:02x}", f"{code:02x}h", f"{code:x}")
            for form in forms:
                for prefix in ("(da60:mpr0:0:180:0):", "mpr0:"):
                    with self.subTest(status=text, form=form, prefix=prefix):
                        events = parse_mpr_dmesg_events(f"{prefix} SCSI status: {form}")
                        row = events["by_controller"]["mpr0"]["event_table"]["rows"][0]
                        self.assertEqual(row["severity"], severity)
                        self.assertEqual(events["recent_events"][0]["severity"], severity)
                        self.assertEqual(row["family"], "aborted_command" if code == 0x40 else "scsi_status")
                        self.assertEqual(row["scsi_status"].upper(), text)
                        self.assertEqual(row["decode_confidence"], "standard")
                        self.assertEqual(row["decode_source"], "t10_scsi_status")

    def test_scsi_status_summaries_use_canonical_severity_before_aggregation(self) -> None:
        for code, text, severity in self.SCSI_STATUS_CASES:
            for form in (text, f"0x{code:02x}", f"{code:02x}", f"{code:02x}h"):
                events = parse_mpr_dmesg_events(f"(da60:mpr0:0:180:0): SCSI status: {form}")
                for scope, key in (("by_controller", "mpr0"), ("by_device", "da60"),
                                   ("by_controller_target", "mpr0:180")):
                    with self.subTest(status=text, form=form, scope=scope):
                        summary = events[scope][key]
                        self.assertEqual(summary["event_count"], 1)
                        self.assertEqual(summary["error_count"], int(severity == "error"))
                        self.assertEqual(summary["primary_fault"]["severity"], severity)
                        self.assertEqual(summary["top_findings"][0]["severity"], severity)
                        self.assertEqual(summary["top_findings"][0]["count"], 1)
                        self.assertEqual(summary["recent_events"][0]["severity"], severity)
                        self.assertEqual(summary["event_table"]["rows"][0]["severity"], severity)
                        family = "aborted_command" if code == 0x40 else "scsi_status"
                        self.assertEqual(summary["fault_family_counts"], {family: 1})
                        self.assertIn(f"{int(severity == 'error')} errors, 0 retries", summary["operator_summary"])

    def test_scsi_status_full_snapshot_preserves_numeric_text_severity_parity(self) -> None:
        system = SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core"))
        snapshot = InventorySnapshot(
            slots=[SlotView(
                slot=0, slot_label="00", row_index=0, column_index=0, present=True,
                state=SlotState.healthy, device_name="da60",
                raw_status={
                    "enclosure_id": parse_mpr_enclosures(MPR0_ENCLOSURES)[0]["logical_id"],
                    "ses_slot_number": 1,
                },
                multipath=MultipathView(
                    name="mpath0", device_name="multipath/disk0",
                    members=[MultipathMember(device_name="da60", state="ACTIVE", controller_label="mpr0")],
                ),
            )],
            refresh_interval_seconds=30,
        )
        expected_severities = [severity for _, _, severity in self.SCSI_STATUS_CASES]
        expected_labels = [f"SCSI status: {text.title()}".replace("Aca", "ACA") for _, text, _ in self.SCSI_STATUS_CASES]
        for form_kind in ("text", "prefixed", "bare", "suffixed", "mixed"):
            statuses = []
            for index, (code, text, _) in enumerate(self.SCSI_STATUS_CASES):
                forms = {"text": text, "prefixed": f"0x{code:02x}", "bare": f"{code:02x}",
                         "suffixed": f"{code:02x}h", "mixed": text if index % 2 else f"0x{code:02x}"}
                statuses.append(forms[form_kind])
            event_output = "\n".join(f"(da60:mpr0:0:180:0): SCSI status: {value}" for value in statuses)
            fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs={
                "sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS,
                "sudo -n /usr/sbin/mprutil -u 0 show adapter": MPR0_ADAPTER,
                "sudo -n /usr/sbin/mprutil -u 0 show expanders": MPR0_EXPANDERS,
                "sudo -n /usr/sbin/mprutil -u 0 show enclosures": MPR0_ENCLOSURES,
                "sudo -n /usr/sbin/mprutil -u 0 show devices": MPR0_DEVICES,
                CORE_MPR_DMESG_EVENTS_COMMAND: event_output,
            })
            serialized = json.loads(fabric.model_dump_json())
            summaries = [(node["id"], node["metrics"]["kernel_diagnostics"])
                         for node in serialized["nodes"] if node.get("metrics", {}).get("kernel_diagnostics")]
            bay_trace = next(trace for trace in serialized["traces"] if trace["id"] == "bay:0")
            members = bay_trace["metrics"]["mpr_devices"]
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0]["member_device_name"], "da60")
            summaries.append(("bay:0/da60", members[0]["diagnostics"]))
            self.assertEqual(len(summaries), 2)  # Controller and mapped member, not helper-only output.
            self.assertIn("controller:mpr0", [node_id for node_id, _ in summaries])
            for node_id, summary in summaries:
                with self.subTest(form=form_kind, node=node_id):
                    self.assertEqual(summary["event_count"], 8)
                    self.assertEqual(summary["error_count"], 1)
                    self.assertEqual(summary["fault_family_counts"], {"scsi_status": 7, "aborted_command": 1})
                    table = summary["event_table"]
                    self.assertEqual(table["total_count"], 8)
                    self.assertFalse(table["truncated"])
                    self.assertEqual([row["severity"] for row in table["rows"]], expected_severities)
                    self.assertEqual([row["label"] for row in table["rows"]], expected_labels)
                    self.assertEqual(summary["primary_fault"]["label"], "SCSI status: Task Aborted")
                    self.assertEqual(summary["primary_fault"]["severity"], "error")
                    self.assertEqual([finding["severity"] for finding in summary["top_findings"]],
                                     ["error", "warning", "warning", "warning", "warning", "warning"])
                    self.assertIn("1 errors, 0 retries", summary["operator_summary"])

    def test_scsi_status_unknown_values_preserve_parser_fallback(self) -> None:
        # Bare numerals remain hexadecimal, not decimal; do not broaden accepted syntax.
        for value, severity in (("0xff", "info"), ("ffh", "info"), ("64", "info"),
                                ("0X40", "info"), ("unlisted status", "info"), ("unlisted error", "error")):
            with self.subTest(value=value):
                events = parse_mpr_dmesg_events(f"(da60:mpr0:0:180:0): SCSI status: {value}")
                row = events["by_device"]["da60"]["event_table"]["rows"][0]
                self.assertEqual(row["severity"], severity)
                self.assertEqual(row["scsi_status"], value)
                self.assertEqual(row["decode_confidence"], "observed")
                self.assertEqual(row["decode_source"], "kernel_message")
                self.assertIn("not in the current local status-code table", row["decoder_note"])
                self.assertNotIn("scsi_status_code", row)
                for scope, key in (("by_controller", "mpr0"), ("by_device", "da60"),
                                   ("by_controller_target", "mpr0:180")):
                    self.assertEqual(events[scope][key]["error_count"], int(severity == "error"))
                    self.assertEqual(events[scope][key]["primary_fault"]["severity"], severity)

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

    def test_oversized_cam_numeric_code_is_not_converted_and_keeps_bounded_evidence(self) -> None:
        oversized_code = "9" * 5_000
        events = parse_mpr_dmesg_events(
            f"(da1:mpr0:0:9:0): Error {oversized_code}, Retries exhausted"
        )

        record = events["by_device"]["da1"]["event_table"]["rows"][0]

        self.assertEqual(record["family"], "cam_error")
        self.assertNotIn("cam_error_code", record)
        self.assertLessEqual(len(record["cam_error_code_raw"]), MAX_DIAGNOSTIC_TEXT_LENGTH)
        self.assertTrue(record["cam_error_code_raw"].endswith("..."))
        self.assertIn("too long to decode", record["decoder_note"])

    def test_oversized_cam_target_tokens_are_not_converted_and_keep_bounded_evidence(self) -> None:
        oversized_target = "9" * 5_000
        bounded_target = f"{'9' * (MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH - 3)}..."

        for line in (
            f"(da1:mpr0:0:{oversized_target}:0): Retrying command",
            f"mpr0: Controller reported scsi ioc terminated tgt {oversized_target} SMID 1 loginfo 31120302",
        ):
            with self.subTest(source=line[:20]):
                events = parse_mpr_dmesg_events(line)
                controller = events["by_controller"]["mpr0"]
                record = controller["event_table"]["rows"][0]

                self.assertEqual(controller["targets"], [bounded_target])
                self.assertEqual(record["target"], bounded_target)
                self.assertIn(f"mpr0:{bounded_target}", events["by_controller_target"])

    def test_mpr_summary_orders_mixed_bounded_targets_without_losing_scope(self) -> None:
        bounded_target = f"{'9' * (MAX_DIAGNOSTIC_NUMERIC_TOKEN_LENGTH - 3)}..."
        summary = new_mpr_event_summary()

        for sequence, target in enumerate(("10", bounded_target, "2", "10", None)):
            event = {
                "controller": "mpr0",
                "target": target,
                "event_type": "ioc_terminated",
                "severity": "error",
            }
            record = {
                **event,
                "event_id": f"event-{sequence}",
                "family": "sas_transport",
                "fingerprint": "shared-finding",
                "label": "Shared SAS fault",
            }
            record_mpr_event_summary(summary, event, record)

        finalized = finalize_mpr_event_summary(summary)

        expected_targets = ["2", "10", bounded_target]
        self.assertEqual(finalized["targets"], expected_targets)
        self.assertEqual(finalized["top_findings"][0]["affected"]["targets"], expected_targets)
        self.assertIn(f"on target 2, 10, {bounded_target}", finalized["operator_summary"])
        self.assertEqual(finalized["event_count"], 5)
        self.assertEqual(finalized["top_findings"][0]["count"], 5)

    def test_aggregated_finding_severity_is_monotonic_across_event_order(self) -> None:
        info_event = "(da2:mpr0:0:10:0): SCSI sense: NO SENSE asc:7f,7e (Unknown hardware fault)"
        error_event = "(da2:mpr0:0:10:0): SCSI sense: HARDWARE ERROR asc:7f,7e (Unknown hardware fault)"

        for rows in ((info_event, error_event), (error_event, info_event)):
            with self.subTest(first=rows[0].split("SCSI sense: ", 1)[1].split(" asc:", 1)[0]):
                events = parse_mpr_dmesg_events("\n".join(rows))
                finding = events["by_controller"]["mpr0"]["top_findings"][0]

                self.assertEqual(finding["count"], 2)
                self.assertEqual(finding["severity"], "error")

    def test_unknown_hardware_error_sense_keeps_parser_error_severity(self) -> None:
        events = parse_mpr_dmesg_events(
            "(da2:mpr0:0:10:0): SCSI sense: HARDWARE ERROR asc:7f,7e (Unknown hardware fault)"
        )

        record = events["by_device"]["da2"]["event_table"]["rows"][0]

        self.assertEqual(record["family"], "scsi_sense")
        self.assertEqual(record["severity"], "error")

    def test_medium_format_sense_is_an_error(self) -> None:
        events = parse_mpr_dmesg_events(
            "(da3:mpr0:0:11:0): SCSI sense: MEDIUM ERROR asc:31,0 (Medium format corrupted)"
        )

        record = events["by_device"]["da3"]["event_table"]["rows"][0]

        self.assertEqual(record["family"], "medium_format")
        self.assertEqual(record["severity"], "error")

    def test_error_count_matches_final_sense_severities(self) -> None:
        events = parse_mpr_dmesg_events(
            "\n".join((
                "(da2:mpr0:0:10:0): SCSI sense: HARDWARE ERROR asc:7f,7e (Unknown hardware fault)",
                "(da3:mpr0:0:11:0): SCSI sense: MEDIUM ERROR asc:31,0 (Medium format corrupted)",
                "(da4:mpr0:0:12:0): SCSI sense: RECOVERED ERROR asc:18,5 (Recovered data, recommend reassignment)",
            ))
        )
        controller = events["by_controller"]["mpr0"]
        severities = [row["severity"] for row in controller["event_table"]["rows"]]

        self.assertEqual(severities, ["error", "error", "warning"])
        self.assertEqual(controller["error_count"], severities.count("error"))

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
        self.assertEqual(controller["event_table"]["schema_version"], 2)
        self.assertEqual(controller["event_table"]["sample_count"], len(controller["event_table"]["rows"]))
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

        timeout_sense = next(record for record in target_181["event_table"]["rows"] if record.get("asc") == "4b,3")
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
            for record in events["by_controller"]["mpr0"]["event_table"]["rows"]
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
        self.assertEqual(log_sense["log_page_control_label"], "Current cumulative values")
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

    @staticmethod
    def _decoded_cdb_rows(lines: list[str]) -> dict[str, dict]:
        events = parse_mpr_dmesg_events("\n".join(lines))
        return {
            record["target"]: record
            for record in events["by_controller"]["mpr0"]["event_table"]["rows"]
        }

    def test_log_sense_page_control_labels_follow_command_field_order(self) -> None:
        # LOG SENSE CDB byte 2 bits 7-6 (PC): 00b threshold, 01b cumulative,
        # 10b default threshold, 11b default cumulative. A device may support
        # only a subset; that never changes what each encoding means.
        expected = {
            0: ("0x0", "Current threshold values"),
            1: ("0x1", "Current cumulative values"),
            2: ("0x2", "Default threshold values"),
            3: ("0x3", "Default cumulative values"),
        }
        lines = [
            f"(da8{pc}:mpr0:0:30{pc}:0): LOG SENSE. CDB: "
            f"4d 00 {(pc << 6) | 0x18:02x} 00 00 00 00 00 40 00"
            for pc in expected
        ]
        rows = self._decoded_cdb_rows(lines)

        for pc, (raw, label) in expected.items():
            with self.subTest(page_control=pc):
                cdb = f"4d 00 {(pc << 6) | 0x18:02x} 00 00 00 00 00 40 00"
                decoded = decode_scsi_cdb_message(f"LOG SENSE. CDB: {cdb}")
                self.assertEqual(decoded["log_page_control"], raw)
                self.assertEqual(decoded["log_page_control_label"], label)
                self.assertEqual(decoded["log_page_code"], "0x18")
                row = rows[f"30{pc}"]
                self.assertEqual(row["log_page_control"], raw)
                self.assertEqual(row["log_page_control_label"], label)
                self.assertEqual(row["decoded"]["log_page_control"], raw)
                self.assertEqual(row["decoded"]["log_page_control_label"], label)

    def test_cdb_allocation_length_uses_each_command_field_width(self) -> None:
        rows = self._decoded_cdb_rows(
            [
                # REPORT LUNS: four-byte allocation length in bytes 6-9, MSB first.
                "(da90:mpr0:0:400:0): REPORT LUNS. CDB: a0 00 00 00 00 00 01 02 03 04 00 00",
                # INQUIRY: two-byte allocation length in bytes 3-4.
                "(da91:mpr0:0:401:0): INQUIRY. CDB: 12 01 00 01 02 00",
                # One-byte allocation lengths stay at byte 4.
                "(da92:mpr0:0:402:0): REQUEST SENSE. CDB: 03 00 00 01 12 00",
                "(da93:mpr0:0:403:0): MODE SENSE(6). CDB: 1a 00 3f 01 c0 00",
                # LOG SENSE keeps its two-byte field in bytes 7-8.
                "(da94:mpr0:0:404:0): LOG SENSE. CDB: 4d 00 58 00 00 00 09 01 02 07",
            ]
        )
        expected = {
            "400": ("REPORT LUNS", 0x01020304),
            "401": ("INQUIRY", 0x0102),
            "402": ("REQUEST SENSE", 0x12),
            "403": ("MODE SENSE(6)", 0xC0),
            "404": ("LOG SENSE", 0x0102),
        }
        for target, (operation, allocation_length) in expected.items():
            with self.subTest(operation=operation):
                row = rows[target]
                self.assertEqual(row["operation"], operation)
                self.assertEqual(row["allocation_length"], allocation_length)
                self.assertEqual(row["decoded"]["allocation_length"], allocation_length)

    def test_cdb_allocation_length_is_omitted_when_the_field_is_truncated(self) -> None:
        for message, cdb_hex in (
            ("REPORT LUNS. CDB: a0 00 00 00 00 00 01 02 03", "a0 00 00 00 00 00 01 02 03"),
            ("INQUIRY. CDB: 12 01 00 01", "12 01 00 01"),
        ):
            with self.subTest(message=message):
                decoded = decode_scsi_cdb_message(message)
                self.assertNotIn("allocation_length", decoded)
                self.assertEqual(decoded["cdb_hex"], cdb_hex)
                self.assertEqual(decoded["byte_count"], len(cdb_hex.split()))

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
            for record in events["by_controller"]["mpr0"]["event_table"]["rows"]
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
    def test_alias_store_tolerates_malformed_shapes_and_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sas_fabric_aliases.json"
            store = SasFabricAliasStore(path)
            for payload in (
                "{",
                "null",
                "[]",
                '{"sas_fabric_aliases": []}',
                '{"sas_fabric_aliases": {"bad": {"object_id": "", "label": ""}}}',
            ):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    self.assertEqual(store.load_all(), {})

    def test_alias_store_tolerates_read_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sas_fabric_aliases.json"
            path.write_text("{}", encoding="utf-8")
            store = SasFabricAliasStore(path)

            with patch("app.services.sas_fabric_alias_store.Path.open", side_effect=OSError("synthetic read failure")):
                self.assertEqual(store.load_all(), {})

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

    def test_canonical_pool_and_vdev_alias_updates_remove_legacy_rows(self) -> None:
        cases = (
            ("pool:Tank Main", "pool:Tank%20Main", "pool"),
            ("pool:tank-main", "pool:Tank%20Main", "pool"),
            ("vdev:Data/Mirror", "vdev:Tank%20Main/Data%2FMirror", "vdev"),
            ("vdev:data-mirror", "vdev:Tank%20Main/Data%2FMirror", "vdev"),
        )
        for legacy_id, canonical_id, object_kind in cases:
            with self.subTest(legacy_id=legacy_id), tempfile.TemporaryDirectory() as temp_dir:
                store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
                store.save_alias(
                    SasFabricAlias(
                        system_id="synthetic-system",
                        object_id=legacy_id,
                        object_kind=object_kind,
                        label="Legacy label",
                    )
                )
                service = FabricAliasMutationRegressionTests.make_service(store, [
                    FabricPathRegressionTests.slot(
                        device_name="sda", pool_name="Tank Main", vdev_name="Data/Mirror"),
                ])
                service.system = SystemConfig(
                    id="synthetic-system", truenas=TrueNASConfig(platform="scale"),
                )

                saved = service.save_sas_fabric_alias(
                    object_id=canonical_id,
                    object_kind=object_kind,
                    label="Updated label",
                )
                selected = store.list_aliases("synthetic-system")

                self.assertTrue(saved["ok"])
                self.assertEqual(
                    [(alias.object_id, alias.label) for alias in selected],
                    [(canonical_id, "Updated label")],
                )
                cleared = service.save_sas_fabric_alias(
                    object_id=canonical_id,
                    object_kind=object_kind,
                    label=None,
                )
                self.assertTrue(cleared["cleared"])
                self.assertEqual(store.list_aliases("synthetic-system"), [])


class FabricAliasMutationRegressionTests(unittest.TestCase):
    """Real public mutation entry points over disposable observed topology."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "aliases.json"
        journal = patch("app.services.sas_fabric_alias_store.record_config_change")
        self.journal = journal.start()
        self.addCleanup(journal.stop)
        self.store = SasFabricAliasStore(self.path)
        self.slots = [FabricPathRegressionTests.slot(
            device_name="sda", pool_name="Pool", vdev_name="mirror")]
        self.service = self.make_service(self.store, self.slots)

    @staticmethod
    def make_service(store, slots):
        service = object.__new__(InventoryService)
        service.system = SystemConfig(id="synthetic", truenas=TrueNASConfig(platform="linux"))
        service.sas_fabric_alias_store = store
        service._cache = {"synthetic-shelf": InventorySnapshot(
            slots=slots, refresh_interval_seconds=30, selected_enclosure_id="synthetic-shelf")}
        service._source_bundle = InventorySourceBundle(
            raw_data=TrueNASRawData([], [], [], {}, []), ssh_outputs={},
            ssh_collected=False, warnings=[], sources={},
            scale_ses_data=ParsedSSHData(), quantastor_ses_data=ParsedSSHData())
        service._snapshot_invalidated = set()
        return service

    def fabric(self, slots=None):
        # A newly opened store proves persistence, not an in-memory projection.
        return FabricPathRegressionTests.build(
            self.slots if slots is None else slots,
            aliases=SasFabricAliasStore(self.path).list_aliases("synthetic"))

    def seed(self, object_id, label="Legacy name", **values):
        self.store.save_alias(SasFabricAlias(
            system_id="synthetic", object_id=object_id,
            object_kind=values.pop("object_kind", "path"), label=label, **values))

    def public_fabric(self):
        self.service.sas_fabric_alias_store = SasFabricAliasStore(self.path)
        snapshot = self.service._cache["synthetic-shelf"]
        self.service._get_snapshot_result = AsyncMock(return_value=SimpleNamespace(
            value=snapshot, cache_state="fresh"))
        self.service._get_inventory_source_bundle_result = AsyncMock(return_value=SimpleNamespace(
            value=self.service._source_bundle, cache_state="fresh"))
        before = self.path.read_bytes() if self.path.exists() else None
        with patch("app.services.sas_fabric_alias_store.record_config_change") as journal:
            result = asyncio.run(self.service.get_sas_fabric_snapshot(
                selected_enclosure_id="synthetic-shelf"))
            journal.assert_not_called()
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        return result

    def public_alias(self, object_id, label, scope="system", kind="path"):
        with patch.object(self.service, "_get_snapshot_result", create=True) as snapshot_fetch, \
                patch.object(self.service, "_get_inventory_source_bundle_result", create=True) as source_fetch:
            result = self.service.save_sas_fabric_alias(
                object_id=object_id, object_kind=kind, label=label,
                scope=scope, selected_enclosure_id="synthetic-shelf")
            snapshot_fetch.assert_not_called()
            source_fetch.assert_not_called()
        return result

    def test_public_system_migration_preserves_legacy_enclosure_override(self):
        legacy_id = "path:linux-block:pool-mirror"
        self.seed(legacy_id, "System")
        self.seed(legacy_id, "Shelf", enclosure_id="synthetic-shelf")
        before = self.public_fabric()
        self.assertEqual(before.paths[0]["alias"], "Shelf")
        object_id = before.paths[0]["id"]
        retained = [a for a in self.store.load_all().values() if a.enclosure_id][0]
        self.public_alias(object_id, "New system")
        self.assertEqual(self.public_fabric().paths[0]["alias"], "Shelf")
        self.assertIn(retained, SasFabricAliasStore(self.path).load_all().values())
        self.assertTrue(self.public_alias(object_id, None)["cleared"])
        self.assertEqual(self.public_fabric().paths[0]["alias"], "Shelf")
        self.assertTrue(self.public_alias(object_id, None, "enclosure")["cleared"])
        self.assertIsNone(self.public_fabric().paths[0].get("alias"))
        self.assertEqual(SasFabricAliasStore(self.path).load_all(), {})

    def test_public_canonical_enclosure_override_preserves_legacy_system_fallback(self):
        self.seed("path:linux-block:pool-mirror", "System")
        object_id = self.public_fabric().paths[0]["id"]
        self.public_alias(object_id, "Shelf", "enclosure")
        self.assertEqual(self.public_fabric().paths[0]["alias"], "Shelf")
        self.assertTrue(self.public_alias(object_id, None, "enclosure")["cleared"])
        self.assertEqual(self.public_fabric().paths[0]["alias"], "System")
        self.public_alias(object_id, "Shelf", "enclosure")
        self.public_alias(object_id, "New system")
        self.assertEqual(self.public_fabric().paths[0]["alias"], "Shelf")
        self.public_alias(object_id, None, "enclosure")
        self.assertEqual(self.public_fabric().paths[0]["alias"], "New system")
        # Retain the established clear-through-system-fallback behavior.
        self.public_alias(object_id, None, "enclosure")
        self.assertIsNone(self.public_fabric().paths[0].get("alias"))
        self.assertEqual(SasFabricAliasStore(self.path).load_all(), {})

    def test_public_same_scope_canonical_priority_survives_newer_legacy_row(self):
        object_id = self.public_fabric().paths[0]["id"]
        for scope, enclosure in (("system", None), ("enclosure", "synthetic-shelf")):
            with self.subTest(scope=scope):
                self.public_alias(object_id, "Canonical", scope)
                self.seed("path:linux-block:pool-mirror", "Later legacy", enclosure_id=enclosure)
                self.assertEqual(self.public_fabric().paths[0]["alias"], "Canonical")
                self.public_alias(object_id, None, scope)
                self.assertIsNone(self.public_fabric().paths[0].get("alias"))
                self.assertEqual(SasFabricAliasStore(self.path).load_all(), {})

    def test_public_legacy_ties_and_scope_precedence_are_order_independent(self):
        slots = [FabricPathRegressionTests.slot(
            n, device_name=f"sd{chr(97 + n)}", pool_name="Pool", vdev_name="mirror",
            topology_label=label) for n, label in enumerate(("Route one", "Route two"))]
        aliases = (("path:linux-block:route-one", "One"), ("path:linux-block:route-two", "Two"))
        for slot_order in (slots, slots[::-1]):
            for alias_order in (aliases, aliases[::-1]):
                with self.subTest(slots=slot_order[0].slot, aliases=alias_order[0][1]):
                    self.service = self.make_service(self.store, slot_order)
                    with patch("app.services.sas_fabric_alias_store.datetime") as clock:
                        clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                        for key, label in alias_order:
                            self.seed(key, label, enclosure_id="synthetic-shelf")
                    self.seed(aliases[0][0], "Newer system")
                    view = self.public_fabric()
                    self.assertEqual(view.paths[0]["alias"], "Two")
                    object_id = view.paths[0]["id"]
                    self.public_alias(object_id, None, "enclosure")
                    self.assertEqual(self.public_fabric().paths[0]["alias"], "Newer system")
                    self.public_alias(object_id, None)
                    self.assertEqual(SasFabricAliasStore(self.path).load_all(), {})
                    with patch("app.services.sas_fabric_alias_store.datetime") as clock:
                        clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                        self.seed(aliases[0][0], "Older shelf", enclosure_id="synthetic-shelf")
                    self.seed(aliases[1][0], "Newer system")
                    self.assertEqual(self.public_fabric().paths[0]["alias"], "Older shelf")
                    self.public_alias(object_id, None, "enclosure")
                    self.assertEqual(self.public_fabric().paths[0]["alias"], "Newer system")
                    self.public_alias(object_id, None)
                    self.assertEqual(SasFabricAliasStore(self.path).load_all(), {})

    def test_public_scoped_canonical_provenance_is_not_another_routes_legacy_alias(self):
        legacy_id = "path:linux-block:pool-mirror"
        self.seed(legacy_id, "Different identity", enclosure_id="synthetic-shelf",
                  source="operator-canonical-v1")
        retained = list(self.store.load_all().values())
        view = self.public_fabric()
        self.assertIsNone(view.paths[0].get("alias"))
        object_id = view.paths[0]["id"]
        self.public_alias(object_id, "Own name")
        self.assertEqual(self.public_fabric().paths[0]["alias"], "Own name")
        self.public_alias(object_id, None)
        self.assertIsNone(self.public_fabric().paths[0].get("alias"))
        self.assertEqual(list(SasFabricAliasStore(self.path).load_all().values()), retained)

    def test_public_clear_legacy_route_alias_uses_observed_topology(self):
        self.seed("path:linux-block:pool-mirror")
        before = self.fabric()
        self.assertEqual(before.paths[0]["alias"], "Legacy name")
        result = self.service.save_sas_fabric_alias(
            object_id=before.paths[0]["id"], object_kind="path", label=None)
        self.assertTrue(result["cleared"])
        self.assertIsNone(self.fabric().paths[0].get("alias"))
        self.assertEqual(self.store.load_all(), {})

    def test_route_save_rebuilds_cached_fabrics_off_the_event_loop(self):
        # #813: the async alias route must not rebuild every cached fabric on
        # the event loop, and must reach the same legacy-ownership decision.
        self.seed("path:linux-block:pool-mirror")
        object_id = self.fabric().paths[0]["id"]
        build_threads = []

        def recording_build(**kwargs):
            build_threads.append(threading.get_ident())
            return build_sas_fabric_snapshot(**kwargs)

        async def save(label):
            loop_thread = threading.get_ident()
            result = await self.service.save_sas_fabric_alias_async(
                object_id=object_id, object_kind="path", label=label)
            return loop_thread, result

        with patch("app.services.inventory.build_sas_fabric_snapshot", side_effect=recording_build):
            loop_thread, saved = asyncio.run(save("New name"))
            self.assertTrue(build_threads)
            self.assertNotIn(loop_thread, build_threads)
            self.assertEqual(saved["alias"]["label"], "New name")
            self.assertEqual([a.object_id for a in self.store.list_aliases("synthetic")], [object_id])
            build_threads.clear()
            loop_thread, cleared = asyncio.run(save(None))
            self.assertNotIn(loop_thread, build_threads)
        self.assertTrue(cleared["cleared"])
        self.assertIsNone(self.fabric().paths[0].get("alias"))

    def test_route_save_uses_only_topology_that_is_still_current(self):
        # A refresh can replace the cache while the worker rebuilds; the save
        # must not decide from the replaced view.
        object_id = self.fabric().paths[0]["id"]
        real_nodes = self.service._observed_sas_fabric_nodes
        snapshot = self.service._cache["synthetic-shelf"]
        calls = []

        def save_with(change):
            def rebuild(bundle, snapshots):
                calls.append(len(snapshots))
                change(len(calls))
                return real_nodes(bundle, snapshots)

            with patch.object(self.service, "_observed_sas_fabric_nodes", side_effect=rebuild):
                return asyncio.run(self.service.save_sas_fabric_alias_async(
                    object_id=object_id, object_kind="path", label="New name"))

        def refreshed_once(call):
            if call == 1:  # an equal snapshot, but a new observation
                self.service._cache = {"synthetic-shelf": snapshot.model_copy()}

        self.assertEqual(save_with(refreshed_once)["alias"]["label"], "New name")
        self.assertEqual(len(calls), 2)
        self.assertTrue(self.public_alias(object_id, None)["cleared"])

        calls.clear()

        def view_dropped(_call):
            self.service._cache = {}

        with self.assertRaisesRegex(ValueError, "topology"):
            save_with(view_dropped)
        self.assertEqual(calls, [1, 0])
        self.assertEqual(self.store.load_all(), {})

    def test_public_save_then_clear_never_resurrects_legacy_name(self):
        self.seed("path:linux-block:pool-mirror")
        object_id = self.fabric().paths[0]["id"]
        self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label="New name")
        self.assertEqual(self.fabric().paths[0]["alias"], "New name")
        self.assertEqual([a.object_id for a in self.store.list_aliases("synthetic")], [object_id])
        self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label=None)
        self.assertIsNone(self.fabric().paths[0].get("alias"))

    def test_all_route_legacy_ids_are_aggregated_and_mutated_in_both_orders(self):
        slots = [FabricPathRegressionTests.slot(
            n, device_name=f"sd{chr(97 + n)}", pool_name="Pool", vdev_name="mirror",
            topology_label=label) for n, label in enumerate(("Route one", "Route two"))]
        for order in (slots, slots[::-1]):
            with self.subTest(order=[s.topology_label for s in order]):
                self.seed("path:linux-block:route-one", "Older name")
                self.seed("path:linux-block:route-two", "Newest name")
                service = self.make_service(self.store, order)
                before = self.fabric(order)
                self.assertEqual(before.paths[0]["alias"], "Newest name")
                node = next(n for n in before.nodes if n.kind == "path")
                self.assertEqual(set(node.raw["legacy_alias_ids"]), {
                    "path:linux-block:route-one", "path:linux-block:route-two"})
                service.save_sas_fabric_alias(object_id=node.id, object_kind="path", label=None)
                self.assertEqual(self.store.load_all(), {})
                self.assertIsNone(self.fabric(order).paths[0].get("alias"))

    def test_legacy_precedence_is_deterministic_including_timestamp_ties(self):
        slots = [FabricPathRegressionTests.slot(
            n, device_name=f"sd{chr(97 + n)}", pool_name="Pool", vdev_name="mirror",
            topology_label=label) for n, label in enumerate(("Route one", "Route two"))]
        for tied in (False, True):
            aliases = [SasFabricAlias(object_id=key, object_kind="path", label=label,
                       updated_at=datetime(2026, 1, day, tzinfo=timezone.utc))
                       for key, label, day in (("path:linux-block:route-one", "Older", 1),
                                              ("path:linux-block:route-two", "Winner", 1 if tied else 2))]
            for order in (slots, slots[::-1]):
                for alias_order in (aliases, aliases[::-1]):
                    with self.subTest(tied=tied, slots=order[0].slot, aliases=alias_order[0].label):
                        fabric = FabricPathRegressionTests.build(order, aliases=alias_order)
                        self.assertEqual(fabric.paths[0]["alias"], "Winner")
                        canonical = SasFabricAlias(object_id=fabric.paths[0]["id"], label="Canonical",
                                                   source="operator-canonical-v1")
                        named = FabricPathRegressionTests.build(order, aliases=[canonical, *alias_order])
                        self.assertEqual(named.paths[0]["alias"], "Canonical")

    def test_case_distinct_canonical_pool_names_survive_both_save_and_clear_orders(self):
        ids = ["pool:Pool-A", "pool:pool-a"]
        slots = [FabricPathRegressionTests.slot(n, device_name=f"sd{chr(97+n)}", pool_name=name)
                 for n, name in enumerate(("Pool-A", "pool-a"))]
        for save_order in (ids, ids[::-1]):
            for clear_order in (ids, ids[::-1]):
                with self.subTest(save_order=save_order, clear_order=clear_order):
                    service = self.make_service(self.store, slots)
                    for object_id in save_order:
                        service.save_sas_fabric_alias(object_id=object_id, object_kind="pool", label=object_id)
                    reopened = SasFabricAliasStore(self.path)
                    self.assertEqual({a.object_id for a in reopened.list_aliases("synthetic")}, set(ids))
                    self.assertTrue(all(a.source == "operator-canonical-v1" for a in reopened.load_all().values()))
                    nodes = {n.id: n for n in self.fabric(slots).nodes}
                    self.assertEqual([nodes[i].alias for i in ids], ids)
                    service.save_sas_fabric_alias(object_id=clear_order[0], object_kind="pool", label=None)
                    self.assertEqual([a.object_id for a in reopened.list_aliases("synthetic")], [clear_order[1]])
                    service.save_sas_fabric_alias(object_id=clear_order[1], object_kind="pool", label=None)
                    self.assertEqual(reopened.load_all(), {})

    def test_ambiguous_legacy_route_mutation_refuses_without_changing_bytes(self):
        slots = [FabricPathRegressionTests.slot(n, device_name=f"sd{chr(97+n)}",
                                               pool_name=name, vdev_name="mirror")
                 for n, name in enumerate(("Pool", "pool"))]
        self.seed("path:linux-block:pool-mirror")
        before = self.path.read_bytes()
        self.journal.reset_mock()
        for order in (slots, slots[::-1]):
            service = self.make_service(self.store, order)
            for route in self.fabric(order).paths:
                for label in (None, "New name"):
                    with self.subTest(route=route["id"], label=label):
                        with self.assertRaisesRegex(ValueError, "ownership|ambiguous"):
                            service.save_sas_fabric_alias(object_id=route["id"], object_kind="path", label=label)
                        self.assertEqual(self.path.read_bytes(), before)
        self.journal.assert_not_called()

    def test_ambiguous_pool_legacy_token_cannot_be_overwritten_as_exact_target(self):
        slots = [FabricPathRegressionTests.slot(n, device_name=f"sd{chr(97+n)}", pool_name=name)
                 for n, name in enumerate(("Pool-A", "pool-a"))]
        self.seed("pool:pool-a", object_kind="pool")
        before = self.path.read_bytes()
        service = self.make_service(self.store, slots)
        for object_id in ("pool:Pool-A", "pool:pool-a"):
            for label in (None, "New name"):
                with self.subTest(object_id=object_id, label=label):
                    with self.assertRaisesRegex(ValueError, "ownership|ambiguous"):
                        service.save_sas_fabric_alias(object_id=object_id, object_kind="pool", label=label)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_canonical_provenance_is_not_projected_to_another_route(self):
        self.seed("path:linux-block:pool-mirror", source="operator-canonical-v1")
        before = self.path.read_bytes()
        fabric = self.fabric()
        self.assertIsNone(fabric.paths[0].get("alias"))
        route_id = fabric.paths[0]["id"]
        self.service.save_sas_fabric_alias(object_id=route_id, object_kind="path", label="Own name")
        self.service.save_sas_fabric_alias(object_id=route_id, object_kind="path", label=None)
        alias = next(iter(self.store.load_all().values()))
        self.assertEqual(alias.object_id, "path:linux-block:pool-mirror")
        self.assertEqual(alias.source, "operator-canonical-v1")
        self.assertIsNone(self.fabric().paths[0].get("alias"))
        self.assertTrue(before)

    def test_unknown_topology_refuses_legacy_cleanup(self):
        self.seed("path:linux-block:pool-mirror")
        object_id = self.fabric().paths[0]["id"]
        self.service._cache = {}
        before = self.path.read_bytes()
        for label in (None, "New name"):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "topology|ownership"):
                    self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label=label)
                self.assertEqual(self.path.read_bytes(), before)

    def test_public_mutations_reject_malformed_authoritative_files(self):
        for raw in (b"{", b"null", b"[]", b"{}", b'{"sas_fabric_aliases": []}',
                    b'{"sas_fabric_aliases": {"bad": {"object_id": "", "label": ""}}}', b"\xff"):
            for label in (None, "New name"):
                with self.subTest(raw=raw, label=label):
                    self.path.write_bytes(raw)
                    self.assertEqual(self.store.list_aliases("synthetic"), [])
                    self.journal.reset_mock()
                    with patch.object(self.store, "_write", wraps=self.store._write) as write:
                        with self.assertRaises(ValueError):
                            self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label=label)
                        write.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), raw)
                    self.journal.assert_not_called()
                    self.assertEqual(list(self.path.parent.glob(f"{self.path.name}.*.tmp")), [])

    def test_public_mutations_reject_authoritative_oserror(self):
        self.seed("pool:Other", "Retained name", object_kind="pool")
        before = self.path.read_bytes()
        real_open = Path.open
        def fail_read(path, mode="r", *args, **kwargs):
            if path == self.path and mode == "r":
                raise OSError("synthetic read failure")
            return real_open(path, mode, *args, **kwargs)
        for label in (None, "New name"):
            with self.subTest(label=label), patch.object(Path, "open", fail_read):
                self.assertEqual(self.store.load_all(), {})
                with patch.object(self.store, "_write", wraps=self.store._write) as write:
                    with self.assertRaises(ValueError):
                        self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label=label)
                    write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_public_controller_and_storage_enclosure_alias_lifecycle(self):
        for node in self.fabric().nodes:
            if node.kind not in {"controller", "storage-enclosure"}:
                continue
            for override in (False, True):
                with self.subTest(kind=node.kind, override=override):
                    legacy_id = node.raw["legacy_alias_ids"][0]
                    self.seed(legacy_id, object_kind=node.kind)
                    before = next(n for n in self.fabric().nodes if n.id == node.id)
                    self.assertEqual(before.alias, "Legacy name")
                    if override:
                        self.service.save_sas_fabric_alias(object_id=node.id, object_kind=node.kind, label="New name")
                    result = self.service.save_sas_fabric_alias(object_id=node.id, object_kind=node.kind, label=None)
                    self.assertTrue(result["cleared"])
                    self.assertEqual(self.store.load_all(), {})

    def test_legacy_mutation_preserves_other_systems_and_scopes(self):
        legacy_id = "path:linux-block:pool-mirror"
        for system, enclosure in (("synthetic", None), ("synthetic", "other-shelf"), ("other", None)):
            self.store.save_alias(SasFabricAlias(system_id=system, enclosure_id=enclosure,
                                                object_id=legacy_id, label="Retained name"))
        object_id = self.fabric().paths[0]["id"]
        # No selected-view override exists: clear retains the established system fallback.
        result = self.service.save_sas_fabric_alias(
            object_id=object_id, object_kind="path", label=None,
            selected_enclosure_id="synthetic-shelf", scope="enclosure")
        self.assertTrue(result["cleared"])
        remaining = self.store.load_all()
        self.assertEqual({(a.system_id, a.enclosure_id) for a in remaining.values()},
                         {("synthetic", "other-shelf"), ("other", None)})

    def test_collisions_in_other_cached_views_also_refuse_mutation(self):
        self.seed("path:linux-block:pool-mirror")
        object_id = self.fabric().paths[0]["id"]
        other = self.service._cache["synthetic-shelf"].model_copy(update={
            "selected_enclosure_id": "other-shelf",
            "slots": [FabricPathRegressionTests.slot(device_name="sdb", pool_name="pool", vdev_name="mirror")],
        })
        self.service._cache["other-shelf"] = other
        before = self.path.read_bytes()
        for label in (None, "New name"):
            with self.assertRaisesRegex(ValueError, "ownership|ambiguous"):
                self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label=label)
            self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_or_invalidated_topology_withholds_legacy_ownership(self):
        self.seed("path:linux-block:pool-mirror")
        object_id = self.fabric().paths[0]["id"]
        before = self.path.read_bytes()
        for incomplete in (True, False):
            self.service._canonical_enclosure_options = {"other-shelf": None} if incomplete else {}
            self.service._snapshot_invalidated = set() if incomplete else {"synthetic-shelf"}
            for label in (None, "New name"):
                with self.subTest(incomplete=incomplete, label=label):
                    with self.assertRaisesRegex(ValueError, "ownership|topology"):
                        self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label=label)
                    self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_legacy_provenance_is_not_destructively_migrated(self):
        self.seed("path:linux-block:pool-mirror", source="unknown-future-source")
        object_id = self.fabric().paths[0]["id"]
        before = self.path.read_bytes()
        for label in (None, "New name"):
            with self.assertRaisesRegex(ValueError, "ownership"):
                self.service.save_sas_fabric_alias(object_id=object_id, object_kind="path", label=label)
            self.assertEqual(self.path.read_bytes(), before)

    def test_direct_store_compatibility_requires_ownership_but_protects_canonical_rows(self):
        self.seed("pool:pool-a", object_kind="pool")
        before = self.path.read_bytes()
        for clear in (False, True):
            with self.subTest(clear=clear):
                with self.assertRaisesRegex(ValueError, "ownership"):
                    if clear:
                        self.store.clear_alias("synthetic", None, "pool:Pool-A", ["pool:pool-a"])
                    else:
                        self.store.save_alias(SasFabricAlias(system_id="synthetic", object_id="pool:Pool-A",
                                              label="New name", source="operator-canonical-v1"), ["pool:pool-a"])
                self.assertEqual(self.path.read_bytes(), before)
        self.seed("pool:pool-a", object_kind="pool", source="operator-canonical-v1")
        self.store.save_alias(SasFabricAlias(system_id="synthetic", object_id="pool:Pool-A",
                              label="New name", source="operator-canonical-v1"), ["pool:pool-a"])
        self.assertTrue(self.store.clear_alias("synthetic", None, "pool:Pool-A", ["pool:pool-a"]))
        self.assertEqual([a.object_id for a in self.store.list_aliases("synthetic")], ["pool:pool-a"])

    def test_file_disappearing_during_read_is_not_valid_first_run(self):
        self.seed("pool:Other", object_kind="pool")
        before = self.path.read_bytes()
        class FailedRead:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                raise FileNotFoundError("synthetic read failure after open")
        real_open = Path.open
        def fail_read(path, mode="r", *args, **kwargs):
            if path == self.path and mode == "r":
                return FailedRead()
            return real_open(path, mode, *args, **kwargs)
        for label in (None, "New name"):
            with self.subTest(label=label), patch.object(Path, "open", fail_read):
                with patch.object(self.store, "_write", wraps=self.store._write) as write:
                    with self.assertRaisesRegex(ValueError, "authoritative"):
                        self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label=label)
                    write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_is_valid_first_run_for_save_and_clear(self):
        result = self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label=None)
        self.assertFalse(result["cleared"])
        self.assertFalse(self.path.exists())
        self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label="First name")
        self.assertEqual(self.store.list_aliases("synthetic")[0].label, "First name")
        self.service.save_sas_fabric_alias(object_id="pool:Pool", object_kind="pool", label=None)
        self.assertEqual(self.store.load_all(), {})


class VirtualFabricTests(unittest.TestCase):
    def snapshot(self, platform):
        system = SystemConfig(id="synthetic-virtual", truenas=TrueNASConfig(platform=platform))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = build_inventory_service(Settings(systems=[system]), system, AsyncMock(), AsyncMock(), temp_dir)
            prefix = "da" if platform == "core" else "sd"
            service._get_inventory_source_bundle = AsyncMock(return_value=InventorySourceBundle(
                raw_data=TrueNASRawData(
                    enclosures=[],
                    disks=[{"name": f"{prefix}{i}", "serial": f"SYNTH-{i}"} for i in range(4)],
                    pools=[], disk_temperatures={}, smart_test_results=[],
                ),
                ssh_outputs={}, ssh_collected=False, warnings=[],
                sources={"api": SourceStatus(enabled=True, ok=True)},
                scale_ses_data=ParsedSSHData(), quantastor_ses_data=ParsedSSHData(),
            ))
            snapshot = asyncio.run(service._build_snapshot())
        self.assertIsNone(snapshot.selected_profile)
        self.assertEqual(len(snapshot.slots), 4)
        self.assertTrue(all(not slot.physical_location_known for slot in snapshot.slots))
        return system, snapshot

    def test_real_virtual_payload_has_no_profile_backplanes(self):
        for platform in ("core", "scale"):
            with self.subTest(platform=platform):
                system, snapshot = self.snapshot(platform)
                payload = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs={}).model_dump(mode="json")
                self.assertFalse([node for node in payload["nodes"] if node["kind"] == "backplane"])
                self.assertFalse([link for link in payload["links"] if link["kind"] == "backplane-bay"])
                self.assertNotIn("profile slot layout", str(payload))
                self.assertNotIn("profile/storage view", str(payload))

    def test_real_virtual_payload_retains_disk_labels_and_location_provenance(self):
        for platform in ("core", "scale"):
            with self.subTest(platform=platform):
                system, snapshot = self.snapshot(platform)
                fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs={})
                nodes = {node.id: node for node in fabric.nodes}
                traces = {trace.id: trace for trace in fabric.traces}
                self.assertTrue(fabric.available)
                for slot in snapshot.slots:
                    node, trace = nodes[f"bay:{slot.slot}"], traces[f"bay:{slot.slot}"]
                    self.assertEqual(node.label, slot.slot_label)
                    self.assertEqual(trace.label, slot.slot_label)
                    self.assertFalse(node.metrics["physical_location_known"])
                    self.assertFalse(trace.metrics["physical_location_known"])
                    self.assertTrue(node.raw["virtual_enclosure"])
                    self.assertIn("physical location unavailable", node.evidence)
                    self.assertIn("physical location unavailable", trace.evidence)
                self.assertTrue(any("virtual" in warning and "physical location unavailable" in warning for warning in fabric.warnings))
                self.assertNotIn("controller:mpr-1", nodes)

    def test_virtual_disks_keep_observed_paths_pools_and_aliases(self):
        for platform in ("core", "scale"):
            with self.subTest(platform=platform):
                system, snapshot = self.snapshot(platform)
                slot = snapshot.slots[0]
                slot.pool_name = "Synthetic pool"
                slot.vdev_name = "mirror-0"
                outputs = {}
                if platform == "core":
                    assert slot.device_name is not None
                    slot.multipath = MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                        MultipathMember(device_name=slot.device_name, controller_label="mpr0", state="ACTIVE"),
                    ])
                    outputs = {"mprutil show adapters": MPR_ADAPTERS}
                alias = SasFabricAlias(system_id=system.id, object_id="bay:0", object_kind="bay", label="Logical data disk")
                fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs=outputs, aliases=[alias])
                nodes = {node.id: node for node in fabric.nodes}
                trace = next(trace for trace in fabric.traces if trace.id == "bay:0")
                self.assertEqual(nodes["bay:0"].label, "Disk 1")
                self.assertEqual(nodes["bay:0"].display_label, alias.label)
                self.assertEqual(trace.display_label, alias.label)
                self.assertIn("pool:Synthetic%20pool", trace.node_ids)
                self.assertIn("vdev:Synthetic%20pool/mirror-0", trace.node_ids)
                if platform == "core":
                    self.assertIn("path:mpr0:active", trace.node_ids)
                    self.assertIn("controller:mpr0", trace.node_ids)
                node_ids = set(nodes)
                link_ids = {link.id for link in fabric.links}
                for link in fabric.links:
                    self.assertIn(link.source, node_ids)
                    self.assertIn(link.target, node_ids)
                for trace in fabric.traces:
                    self.assertTrue(set(trace.node_ids) <= node_ids)
                    self.assertTrue(set(trace.link_ids) <= link_ids)



class SasFabricSnapshotTests(unittest.TestCase):
    def test_bay_nodes_and_zone_ranges_use_the_chassis_bay_labels(self) -> None:
        # Codex P2 on #1069: a UNVR Pro prints its bays 1-7 (slot_number_base
        # 1), so the Storage Fabric graph must call slot index 0 "Bay 01", not
        # "Bay 00". Profiles without a base keep their zero-based labels.
        from app.services.sas_fabric import _backplane_zones_for_slots

        def snapshot(base: int) -> InventorySnapshot:
            return InventorySnapshot(
                slots=[
                    SlotView(
                        slot=index,
                        slot_label=f"{index + base:02d}",
                        row_index=0 if index < 3 else 1,
                        column_index=index if index < 3 else index - 3,
                        present=index < 2,
                        state=SlotState.healthy if index < 2 else SlotState.empty,
                        device_name=f"sd{chr(97 + index)}" if index < 2 else None,
                    )
                    for index in range(7)
                ],
                refresh_interval_seconds=30,
            )

        system = SystemConfig(id="unvr-pro", label="UniFi UNVR Pro", truenas=TrueNASConfig(platform="linux"))
        for base, first, second in ((1, "Bay 01", "Bay 02"), (0, "Bay 00", "Bay 01")):
            with self.subTest(base=base):
                fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot(base), ssh_outputs={})
                bay_labels = {node.slot: node.label for node in fabric.nodes if node.kind == "bay"}
                self.assertEqual(bay_labels[0], first)
                self.assertEqual(bay_labels[1], second)
                zones = _backplane_zones_for_slots(snapshot(base).slots)
                self.assertEqual(zones[0]["range"], f"Bays {base:02d}-{1 + base:02d}")

    def test_select_sas_fabric_builder_key_keeps_platform_selection_explicit(self) -> None:
        def system(platform: str) -> SystemConfig:
            return SystemConfig(id=f"{platform}-system", label=platform.upper(), truenas=TrueNASConfig(platform=platform))

        empty_snapshot = InventorySnapshot(slots=[], refresh_interval_seconds=30)
        ses_snapshot = InventorySnapshot(
            slots=[
                SlotView(
                    slot=0,
                    slot_label="00",
                    row_index=0,
                    column_index=0,
                    present=True,
                    state=SlotState.healthy,
                    ssh_ses_device="/dev/ses0",
                )
            ],
            refresh_interval_seconds=30,
        )

        cases = [
            ("core", empty_snapshot, "core_mpr"),
            ("core", ses_snapshot, "core_mpr"),
            ("scale", ses_snapshot, "linux_ses"),
            ("scale", empty_snapshot, "platform_storage"),
            ("linux", ses_snapshot, "linux_ses"),
            ("linux", empty_snapshot, "platform_storage"),
            ("quantastor", ses_snapshot, "platform_storage"),
            ("esxi", ses_snapshot, "platform_storage"),
            ("ipmi", ses_snapshot, "platform_storage"),
        ]
        for platform, snapshot, expected in cases:
            with self.subTest(platform=platform, expected=expected):
                self.assertEqual(_select_sas_fabric_builder_key(system(platform), snapshot), expected)

    def test_sas_fabric_builder_registry_routes_through_named_wrappers(self) -> None:
        def system(platform: str) -> SystemConfig:
            return SystemConfig(id=f"{platform}-system", label=platform.upper(), truenas=TrueNASConfig(platform=platform))

        empty_snapshot = InventorySnapshot(slots=[], refresh_interval_seconds=30)
        ses_snapshot = InventorySnapshot(
            slots=[
                SlotView(
                    slot=0,
                    slot_label="00",
                    row_index=0,
                    column_index=0,
                    present=True,
                    state=SlotState.healthy,
                    ssh_ses_device="/dev/ses0",
                )
            ],
            refresh_interval_seconds=30,
        )

        self.assertEqual(
            {builder.key for builder in _SAS_FABRIC_BUILDERS},
            {"core_mpr", "linux_ses", "platform_storage"},
        )

        cases = [
            ("core", empty_snapshot, "core_mpr"),
            ("scale", ses_snapshot, "linux_ses"),
            ("scale", empty_snapshot, "platform_storage"),
            ("quantastor", ses_snapshot, "platform_storage"),
        ]
        for platform, snapshot, expected in cases:
            with self.subTest(platform=platform, expected=expected):
                context = _build_sas_fabric_context(
                    system=system(platform),
                    snapshot=snapshot,
                    ssh_outputs={"sudo -n /usr/sbin/mprutil show adapters": MPR_ADAPTERS},
                    sources={"ssh": SourceStatus(enabled=True, ok=True)},
                    warnings=["seed warning"],
                    command_failures=[{"command": "mprutil", "message": "no permission"}],
                    aliases=[
                        SasFabricAlias(
                            system_id=f"{platform}-system",
                            object_id="controller:mpr0",
                            object_kind="controller",
                            label="Archive left HBA",
                        )
                    ],
                )

                builder = _select_sas_fabric_builder(context)

                self.assertEqual(builder.key, expected)
                self.assertEqual(context.warnings, ["seed warning"])
                self.assertEqual(context.normalized_outputs.get("mprutil show adapters"), MPR_ADAPTERS)
                self.assertEqual(context.alias_map["controller:mpr0"].label, "Archive left HBA")

    def test_shared_evidence_labels_are_immutable_exact_tuples(self) -> None:
        fabric_module = importlib.import_module("app.services.sas_fabric")
        expected = {
            "_INVENTORY_SNAPSHOT_EVIDENCE": ("inventory snapshot",),
            "_PROFILE_SLOT_LAYOUT_EVIDENCE": ("profile slot layout",),
            "_SLOT_MULTIPATH_EVIDENCE": ("multipath members",),
            "_LINUX_SES_PATH_EVIDENCE": (
                "lsscsi -g",
                "lsscsi -g -t",
                "sg_ses AES/EC",
                "sg_ses --join --filter",
            ),
            "_LINUX_SES_ENCLOSURE_EVIDENCE": ("sg_ses AES/EC", "sg_ses --join --filter"),
            "_LINUX_SES_BAY_EVIDENCE": (
                "inventory snapshot",
                "lsblk --json",
                "lsscsi -g -t",
                "sg_ses AES/EC",
                "sg_ses --join --filter",
            ),
            "_SG_SES_AES_EVIDENCE": ("sg_ses AES/EC",),
            "_BMC_INVENTORY_EVIDENCE": ("BMC inventory",),
        }

        for name, labels in expected.items():
            with self.subTest(name=name):
                value = getattr(fabric_module, name)
                self.assertIsInstance(value, tuple)
                self.assertEqual(value, labels)

    def test_builder_wrappers_pass_the_frozen_context_without_kwargs_unpacking(self) -> None:
        fabric_module = importlib.import_module("app.services.sas_fabric")
        context = _build_sas_fabric_context(
            system=SystemConfig(id="core", truenas=TrueNASConfig(platform="core")),
            snapshot=InventorySnapshot(slots=[], refresh_interval_seconds=30),
            ssh_outputs={},
        )
        cases = (
            (fabric_module.CoreMprFabricBuilder(), "_build_core_mpr_fabric_snapshot"),
            (fabric_module.LinuxSesFabricBuilder(), "_build_linux_ses_fabric_snapshot"),
            (fabric_module.PlatformStorageFabricBuilder(), "_build_platform_storage_fabric_snapshot"),
        )

        for builder, target_name in cases:
            with self.subTest(builder=builder.key):
                expected = object()
                with patch.object(fabric_module, target_name, return_value=expected) as target:
                    self.assertIs(builder.build(context), expected)
                target.assert_called_once_with(context)

    def test_shared_snapshot_assembly_helpers_are_used_by_all_builders(self) -> None:
        fabric_module = importlib.import_module("app.services.sas_fabric")
        cases = (
            ("core", self._storage_id_slot()),
            ("scale", self._storage_id_slot(with_ses=True)),
            ("linux", self._storage_id_slot()),
        )

        with (
            patch.object(fabric_module, "_add_host_node", wraps=fabric_module._add_host_node) as add_host,
            patch.object(
                fabric_module,
                "_add_backplane_zone_nodes",
                wraps=fabric_module._add_backplane_zone_nodes,
            ) as add_backplanes,
            patch.object(
                fabric_module,
                "_add_pool_vdev_nodes_and_links",
                wraps=fabric_module._add_pool_vdev_nodes_and_links,
            ) as add_storage,
            patch.object(
                fabric_module,
                "_build_sas_fabric_result",
                wraps=fabric_module._build_sas_fabric_result,
            ) as build_result,
        ):
            for platform, slot in cases:
                self._build_storage_id_fabric(
                    system_id=f"synthetic-{platform}",
                    platform=platform,
                    slots=[slot],
                )

        self.assertEqual(add_host.call_count, 3)
        self.assertEqual(add_backplanes.call_count, 3)
        self.assertEqual(add_storage.call_count, 3)
        self.assertEqual(build_result.call_count, 3)

    def test_unavailable_snapshot_helper_is_used_by_bounded_builders(self) -> None:
        fabric_module = importlib.import_module("app.services.sas_fabric")
        empty_snapshot = InventorySnapshot(slots=[], refresh_interval_seconds=30)

        with patch.object(
            fabric_module,
            "_build_unavailable_sas_fabric_snapshot",
            wraps=fabric_module._build_unavailable_sas_fabric_snapshot,
        ) as unavailable:
            scale_context = _build_sas_fabric_context(
                system=SystemConfig(id="scale", truenas=TrueNASConfig(platform="scale")),
                snapshot=empty_snapshot,
                ssh_outputs={},
            )
            esxi_context = _build_sas_fabric_context(
                system=SystemConfig(id="esxi", truenas=TrueNASConfig(platform="esxi")),
                snapshot=empty_snapshot,
                ssh_outputs={},
            )
            fabric_module._build_linux_ses_fabric_snapshot(scale_context)
            fabric_module._build_platform_storage_fabric_snapshot(esxi_context)

        self.assertEqual(unavailable.call_count, 2)

    def test_platform_storage_route_registry_keeps_provider_selection_explicit(self) -> None:
        def system(platform: str) -> SystemConfig:
            return SystemConfig(id=f"{platform}-system", label=platform.upper(), truenas=TrueNASConfig(platform=platform))

        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            state=SlotState.healthy,
            device_name="sda",
            raw_status={"bmc_controller_id": "0"},
        )
        snapshot = InventorySnapshot(slots=[slot], refresh_interval_seconds=30)

        self.assertEqual(
            [provider.key for provider in _PLATFORM_STORAGE_ROUTE_PROVIDERS],
            ["quantastor", "esxi", "bmc", "linux"],
        )

        cases = [
            ("quantastor", "quantastor"),
            ("esxi", "esxi"),
            ("ipmi", "bmc"),
            ("scale", "linux"),
            ("linux", "linux"),
        ]
        for platform, expected in cases:
            with self.subTest(platform=platform, expected=expected):
                context = _build_storage_fabric_route_context(
                    system=system(platform),
                    snapshot=snapshot,
                    slot=slot,
                    fabric_kind=f"storage_{platform}",
                    platform_label=platform.upper(),
                )

                provider = _select_storage_fabric_route_provider(context)
                route = provider.route(context)

                self.assertEqual(provider.key, expected)
                self.assertIn("controller_id", route)
                self.assertIn("path_id", route)
                self.assertIn("enclosure_id", route)

    @staticmethod
    def _storage_id_slot(
        *,
        slot: int = 0,
        pool_name: str = "Tank Main",
        vdev_name: str = "Data/Mirror",
        with_ses: bool = False,
    ) -> SlotView:
        return SlotView(
            slot=slot,
            slot_label=f"{slot:02d}",
            row_index=0,
            column_index=slot,
            present=True,
            state=SlotState.healthy,
            device_name=f"sd{chr(ord('a') + slot)}",
            pool_name=pool_name,
            vdev_name=vdev_name,
            ssh_ses_device="/dev/sg26" if with_ses else None,
            ssh_ses_element_id=slot if with_ses else None,
            raw_status={"device_names": [f"sd{chr(ord('a') + slot)}"]},
        )

    @staticmethod
    def _build_storage_id_fabric(
        *,
        system_id: str,
        platform: str,
        slots: list[SlotView],
        aliases: list[SasFabricAlias] | None = None,
    ):
        return build_sas_fabric_snapshot(
            system=SystemConfig(
                id=system_id,
                label=system_id,
                truenas=TrueNASConfig(platform=platform),
            ),
            snapshot=InventorySnapshot(
                slots=slots,
                refresh_interval_seconds=30,
                selected_system_id=system_id,
                selected_system_platform=platform,
                selected_enclosure_id="synthetic-enclosure",
            ),
            ssh_outputs={},
            aliases=aliases,
        )

    def test_builder_snapshot_contracts_pin_ids_evidence_warnings_raw_and_metrics(self) -> None:
        cases = {
            "core": {
                "platform": "core",
                "with_ses": False,
                "node_ids": [
                    "backplane:0",
                    "bay:0",
                    "controller:mpr-1",
                    "host",
                    "pool:Tank%20Main",
                    "vdev:Tank%20Main/Data%2FMirror",
                ],
                "host_metrics": {"slot_count": 1},
                "host_evidence": [],
                "bay_evidence": ["inventory snapshot"],
                "warnings": [],
                "raw": {
                    "commands": [],
                    "path_counts": {},
                    "path_slots": {},
                    "selected_enclosure_keys": [],
                    "selected_bay_slots": [0],
                    "selected_disk_slots": [0],
                    "pci_controllers": [],
                    "pcie_slots": [],
                    "mpr_sysctl_locations": {},
                    "mpr_kernel_events": {"event_count": 0, "controllers": [], "devices": []},
                    "command_failures": [],
                },
            },
            "linux_ses": {
                "platform": "scale",
                "with_ses": True,
                "node_ids": [
                    "backplane:0",
                    "bay:0",
                    "controller:linux-ses",
                    "host",
                    "path:linux-ses:sg26",
                    "pool:Tank%20Main",
                    "ses:sg26",
                    "vdev:Tank%20Main/Data%2FMirror",
                ],
                "host_metrics": {
                    "slot_count": 1,
                    "selected_disk_count": 1,
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                },
                "host_evidence": ["inventory snapshot"],
                "bay_evidence": [
                    "inventory snapshot",
                    "lsblk --json",
                    "lsscsi -g -t",
                    "sg_ses AES/EC",
                    "sg_ses --join --filter",
                ],
                "warnings": [
                    "TrueNAS SCALE Storage Fabric map is built from Linux block, SCSI transport, and SES slot evidence. "
                    "HBA and expander hop detail is not exposed by this Linux SES evidence."
                ],
                "raw": {
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "linux_ses",
                    "ses_devices": ["/dev/sg26"],
                    "selected_bay_slots": [0],
                    "selected_disk_slots": [0],
                    "command_failures": [],
                },
            },
            "platform_storage": {
                "platform": "quantastor",
                "with_ses": False,
                "node_ids": [
                    "backplane:0",
                    "bay:0",
                    "controller:storage-v2:4ffb4b40f69f088abe6c90de4105cdd636e63fd2d13479ba4e1719c038f33a1d",
                    "host",
                    "path:storage-v2:b88a2d1f810b8d0475e023d69a19e8bf123536e01c7f2afe60cbdb4dad509595",
                    "pool:Tank%20Main",
                    "storage-enclosure:storage-v2:4ffb4b40f69f088abe6c90de4105cdd636e63fd2d13479ba4e1719c038f33a1d:synthetic-enclosure",
                    "vdev:Tank%20Main/Data%2FMirror",
                ],
                "host_metrics": {
                    "slot_count": 1,
                    "selected_disk_count": 1,
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "storage_quantastor",
                },
                "host_evidence": ["inventory snapshot"],
                "bay_evidence": ["inventory snapshot"],
                "warnings": [
                    "Quantastor Storage Fabric is built from Quantastor storage-system, HA-node, pool, disk and "
                    "optional SES data; controller and expander hops appear only when those sources report them."
                ],
                "raw": {
                    "fabric_domain": "storage_fabric",
                    "fabric_kind": "storage_quantastor",
                    "selected_bay_slots": [0],
                    "selected_disk_slots": [0],
                    "command_failures": [],
                },
            },
        }

        for name, expected in cases.items():
            with self.subTest(builder=name):
                platform = expected["platform"]
                fabric = self._build_storage_id_fabric(
                    system_id=f"synthetic-{platform}",
                    platform=platform,
                    slots=[self._storage_id_slot(with_ses=expected["with_ses"])],
                )
                nodes = {node.id: node for node in fabric.nodes}
                self.assertEqual([node.id for node in fabric.nodes], expected["node_ids"])
                self.assertEqual(nodes["host"].metrics, expected["host_metrics"])
                self.assertEqual(nodes["host"].evidence, expected["host_evidence"])
                self.assertEqual(nodes["bay:0"].evidence, expected["bay_evidence"])
                self.assertEqual(fabric.warnings, expected["warnings"])
                self.assertEqual(fabric.raw, expected["raw"])

    def test_pool_and_vdev_ids_are_identical_across_all_fabric_builders(self) -> None:
        fabrics = {
            "core_mpr": self._build_storage_id_fabric(
                system_id="synthetic-core",
                platform="core",
                slots=[self._storage_id_slot()],
            ),
            "linux_ses": self._build_storage_id_fabric(
                system_id="synthetic-scale",
                platform="scale",
                slots=[self._storage_id_slot(with_ses=True)],
            ),
            "platform_storage": self._build_storage_id_fabric(
                system_id="synthetic-scale",
                platform="scale",
                slots=[self._storage_id_slot()],
            ),
        }

        storage_ids = {
            builder: {node.id for node in fabric.nodes if node.kind in {"pool", "vdev"}}
            for builder, fabric in fabrics.items()
        }

        self.assertEqual(
            storage_ids,
            {
                builder: {"pool:Tank%20Main", "vdev:Tank%20Main/Data%2FMirror"}
                for builder in fabrics
            },
        )

    def test_pool_and_vdev_ids_keep_duplicate_labels_collision_safe(self) -> None:
        fabric = self._build_storage_id_fabric(
            system_id="synthetic-linux",
            platform="linux",
            slots=[
                self._storage_id_slot(slot=0, pool_name="Pool A", vdev_name="mirror-0"),
                self._storage_id_slot(slot=1, pool_name="Pool B", vdev_name="mirror-0"),
                self._storage_id_slot(slot=2, pool_name="Pool A", vdev_name="mirror-0"),
                self._storage_id_slot(slot=3, pool_name="pool-a", vdev_name="mirror-0"),
            ],
        )
        nodes = {node.id: node for node in fabric.nodes if node.kind in {"pool", "vdev"}}

        self.assertEqual(
            set(nodes),
            {
                "pool:Pool%20A",
                "pool:Pool%20B",
                "pool:pool-a",
                "vdev:Pool%20A/mirror-0",
                "vdev:Pool%20B/mirror-0",
                "vdev:pool-a/mirror-0",
            },
        )
        self.assertEqual(nodes["vdev:Pool%20A/mirror-0"].related_slots, [0, 2])

    def test_saved_pool_aliases_remain_scoped_to_their_system(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
            for system_id, label in (("system-a", "Primary tank"), ("system-b", "Replica tank")):
                store.save_alias(
                    SasFabricAlias(
                        system_id=system_id,
                        object_id="pool:Tank%20Main",
                        object_kind="pool",
                        label=label,
                    )
                )

            fabrics = {
                system_id: self._build_storage_id_fabric(
                    system_id=system_id,
                    platform="scale",
                    slots=[self._storage_id_slot()],
                    aliases=store.list_aliases(system_id, "synthetic-enclosure"),
                )
                for system_id in ("system-a", "system-b")
            }

        self.assertEqual(
            {
                system_id: next(node.display_label for node in fabric.nodes if node.id == "pool:Tank%20Main")
                for system_id, fabric in fabrics.items()
            },
            {"system-a": "Primary tank", "system-b": "Replica tank"},
        )

    def test_saved_legacy_pool_and_vdev_aliases_follow_builder_changes(self) -> None:
        legacy_alias_sets = {
            "raw_core_ids": (("pool:Tank Main", "Legacy pool"), ("vdev:Data/Mirror", "Legacy vdev")),
            "tokenized_platform_ids": (("pool:tank-main", "Legacy pool"), ("vdev:data-mirror", "Legacy vdev")),
        }
        builder_cases = {
            "core_mpr": ("core", False),
            "linux_ses": ("scale", True),
            "platform_storage": ("scale", False),
        }

        for alias_shape, saved_rows in legacy_alias_sets.items():
            with self.subTest(alias_shape=alias_shape), tempfile.TemporaryDirectory() as temp_dir:
                store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
                for object_id, label in saved_rows:
                    store.save_alias(
                        SasFabricAlias(
                            system_id="synthetic-system",
                            object_id=object_id,
                            object_kind=object_id.split(":", 1)[0],
                            label=label,
                        )
                    )
                aliases = store.list_aliases("synthetic-system", "synthetic-enclosure")

                for builder, (platform, with_ses) in builder_cases.items():
                    with self.subTest(alias_shape=alias_shape, builder=builder):
                        fabric = self._build_storage_id_fabric(
                            system_id="synthetic-system",
                            platform=platform,
                            slots=[self._storage_id_slot(with_ses=with_ses)],
                            aliases=aliases,
                        )
                        nodes = {node.id: node for node in fabric.nodes}
                        aliases_by_id = {alias.object_id: alias for alias in fabric.aliases}

                        self.assertEqual(nodes["pool:Tank%20Main"].display_label, "Legacy pool")
                        self.assertEqual(nodes["vdev:Tank%20Main/Data%2FMirror"].display_label, "Legacy vdev")
                        self.assertEqual(aliases_by_id["pool:Tank%20Main"].label, "Legacy pool")
                        self.assertEqual(
                            aliases_by_id["vdev:Tank%20Main/Data%2FMirror"].label,
                            "Legacy vdev",
                        )

    def test_newest_legacy_alias_wins_when_builder_drift_left_duplicate_rows(self) -> None:
        fabric = self._build_storage_id_fabric(
            system_id="synthetic-scale",
            platform="scale",
            slots=[self._storage_id_slot()],
            aliases=[
                SasFabricAlias(
                    system_id="synthetic-scale",
                    object_id="pool:Tank Main",
                    object_kind="pool",
                    label="Older raw-builder label",
                    updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                SasFabricAlias(
                    system_id="synthetic-scale",
                    object_id="pool:tank-main",
                    object_kind="pool",
                    label="Newer tokenized-builder label",
                    updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ),
            ],
        )

        pool = next(node for node in fabric.nodes if node.id == "pool:Tank%20Main")
        self.assertEqual(pool.alias, "Newer tokenized-builder label")

    def test_saved_canonical_alias_disambiguates_a_legacy_token_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SasFabricAliasStore(Path(temp_dir) / "sas_fabric_aliases.json")
            service = object.__new__(InventoryService)
            service.system = SystemConfig(
                id="synthetic-linux",
                truenas=TrueNASConfig(platform="linux"),
            )
            service.sas_fabric_alias_store = store
            service.save_sas_fabric_alias(
                object_id="pool:pool-a",
                object_kind="pool",
                label="Lowercase pool",
            )

            fabric = self._build_storage_id_fabric(
                system_id="synthetic-linux",
                platform="linux",
                slots=[
                    self._storage_id_slot(slot=0, pool_name="Pool A", vdev_name="mirror-0"),
                    self._storage_id_slot(slot=1, pool_name="pool-a", vdev_name="mirror-0"),
                ],
                aliases=store.list_aliases("synthetic-linux"),
            )

        pools = {node.id: node for node in fabric.nodes if node.kind == "pool"}
        self.assertIsNone(pools["pool:Pool%20A"].alias)
        self.assertEqual(pools["pool:pool-a"].alias, "Lowercase pool")

    def test_ambiguous_legacy_storage_aliases_are_not_applied_to_colliding_labels(self) -> None:
        fabric = self._build_storage_id_fabric(
            system_id="synthetic-linux",
            platform="linux",
            slots=[
                self._storage_id_slot(slot=0, pool_name="Pool A", vdev_name="mirror-0"),
                self._storage_id_slot(slot=1, pool_name="pool-a", vdev_name="mirror-0"),
            ],
            aliases=[
                SasFabricAlias(
                    system_id="synthetic-linux",
                    object_id="pool:pool-a",
                    object_kind="pool",
                    label="Ambiguous old pool alias",
                ),
                SasFabricAlias(
                    system_id="synthetic-linux",
                    object_id="vdev:mirror-0",
                    object_kind="vdev",
                    label="Ambiguous old vdev alias",
                ),
            ],
        )
        storage_nodes = [node for node in fabric.nodes if node.kind in {"pool", "vdev"}]

        self.assertEqual(len(storage_nodes), 4)
        self.assertTrue(all(node.alias is None for node in storage_nodes))
        self.assertTrue(all(node.display_label is None for node in storage_nodes))

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
                self.assertIn("nothing to map yet", fabric.warnings[0])

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

    def test_linux_transport_detail_helper_matches_every_bay_payload_location(self) -> None:
        fabric_module = importlib.import_module("app.services.sas_fabric")
        slot = self._storage_id_slot(with_ses=True)
        slot.sg_device = "/dev/sg2"
        slot.scsi_hctl = "1:0:1:0"
        slot.transport_protocol = "sas"
        slot.attached_sas_address = "5003048001c1043f"
        slot.phy_identifier = "0x0"
        slot.target_port_protocol = "SSP"
        expected = {
            "sg_device": "/dev/sg2",
            "scsi_hctl": "1:0:1:0",
            "transport_protocol": "sas",
            "attached_sas_address": "5003048001c1043f",
            "phy_identifier": "0x0",
            "target_port_protocol": "SSP",
        }

        self.assertEqual(fabric_module._slot_linux_transport_detail(slot), expected)
        fabric = self._build_storage_id_fabric(
            system_id="synthetic-scale",
            platform="scale",
            slots=[slot],
        )
        bay = next(node for node in fabric.nodes if node.id == "bay:0")
        path_state = next(trace for trace in fabric.traces if trace.id == "bay:0").metrics["path_states"][0]
        self.assertEqual({key: bay.metrics[key] for key in expected}, expected)
        self.assertEqual({key: bay.raw[key] for key in expected}, expected)
        self.assertEqual({key: path_state[key] for key in expected}, expected)

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
        self.assertIn("nothing to map yet", fabric.warnings[0])

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
        self.assertIn("controller:storage-v2:64635a31e5c72fe52f461f1e83c37f134a47b2fa2a9093a84ebf915d265d5ac4", nodes)
        self.assertIn("storage-enclosure:storage-v2:64635a31e5c72fe52f461f1e83c37f134a47b2fa2a9093a84ebf915d265d5ac4:right-nvme-2", nodes)
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
        self.assertIn("controller:storage-v2:7ac57170e136f4cd5a2b423bccf793dafe8562b0b73c1b5f54c4b3503749fe64", nodes)
        self.assertIn("ses-enclosure:storage-v2:7ac57170e136f4cd5a2b423bccf793dafe8562b0b73c1b5f54c4b3503749fe64:qsosn-right", nodes)
        self.assertEqual(fabric.controllers[0]["board"], "QSOSN-Right")
        self.assertIn("qs hw-disk-list", nodes["controller:storage-v2:7ac57170e136f4cd5a2b423bccf793dafe8562b0b73c1b5f54c4b3503749fe64"].evidence)
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
        self.assertIn("controller:storage-v2:2ae1f4742e3d47e48052b1e80e630b8f85442ceabaa39d10b2c400870f885802", nodes)
        self.assertIn("storage-enclosure:storage-v2:2ae1f4742e3d47e48052b1e80e630b8f85442ceabaa39d10b2c400870f885802:252", nodes)
        self.assertEqual(fabric.controllers[0]["board"], "StorCLI c1")
        self.assertIn("StorCLI physical drive", nodes["controller:storage-v2:2ae1f4742e3d47e48052b1e80e630b8f85442ceabaa39d10b2c400870f885802"].evidence)
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


class FabricPathRegressionTests(unittest.TestCase):
    # Invented numeric identifiers, not copied enclosure or drive identities.
    CORE_ENCLOSURE = f"{1:016x}"
    CORE_SAS_PORT = f"{2:016x}"

    @staticmethod
    def slot(number=0, **values):
        return SlotView(
            slot=number, slot_label=str(number), row_index=0, column_index=number,
            present=True, state=SlotState.healthy, **values,
        )

    @staticmethod
    def build(slots, platform="linux", outputs=None, aliases=None):
        return build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic", label="Synthetic", truenas=TrueNASConfig(platform=platform)),
            snapshot=InventorySnapshot(
                slots=slots, refresh_interval_seconds=30,
                selected_enclosure_id="synthetic-shelf", selected_enclosure_label="Synthetic shelf",
            ),
            ssh_outputs=outputs or {}, aliases=aliases,
        )

    def core_build(self, number, ses_number, rows, **values):
        slot = self.slot(
            number, device_name="da5",
            multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                MultipathMember(device_name="da5", controller_label="mpr0", state="ACTIVE")]),
            raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": ses_number},
            **values,
        )
        return self.build([slot], "core", {
            "mprutil show adapters": MPR_ADAPTERS,
            "mprutil -u 0 show enclosures": f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
            "mprutil -u 0 show devices": rows,
            CORE_MPR_DMESG_EVENTS_COMMAND: "mpr0: Controller reported scsi ioc terminated tgt 180 SMID 1 loginfo 31120302",
        })

    @classmethod
    def mpr_row(cls, location, device="SAS Target", target=180, enclosure="0002"):
        return f"00 {target} {cls.CORE_SAS_PORT} 000a 0002 {device} 12 {enclosure} {location} 1"

    def rear_shelf_build(self, bay, *, camcontrol_target=None, rows=None, enclosures=None):
        # Rear shelf of a cascaded JBOD: global bays 30-59 sit at MPR locations
        # 0-29 on the rear expander, and SES numbers them one-based (bay - 29).
        # Each MPR row's target is 150 + its location.
        if rows is None:
            rows = [self.mpr_row(location, target=150 + location) for location in range(30)]
        slot = self.slot(
            bay, device_name="da5",
            multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                MultipathMember(device_name="da5", controller_label="mpr0", state="ACTIVE")]),
            raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": bay - 29},
        )
        own, neighbour = 150 + (bay - 30), 150 + (bay - 29)
        events = [f"mpr0: Controller reported scsi ioc terminated tgt {own} SMID 1 loginfo 31120302"]
        events += [f"mpr0: Controller reported scsi ioc terminated tgt {neighbour} SMID {n} loginfo 31120302" for n in (2, 3, 4)]
        outputs = {
            "mprutil show adapters": MPR_ADAPTERS,
            "mprutil -u 0 show enclosures": enclosures or f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
            "mprutil -u 0 show devices": "\n".join(rows),
            CORE_MPR_DMESG_EVENTS_COMMAND: "\n".join(events),
        }
        if camcontrol_target is not None:
            outputs["camcontrol devlist -v"] = (
                "scbus12 on mpr0 bus 0:\n"
                f"<Synthetic disk 0001>  at scbus12 target {camcontrol_target} lun 0 (da5,pass5)"
            )
        fabric = self.build([slot], "core", outputs)
        return next(t for t in fabric.traces if t.id == f"bay:{bay}").metrics["mpr_devices"]

    def test_rear_shelf_bay_joins_its_own_mpr_row_by_cam_target(self):
        # #936: the bay's member disk is CAM target 150 + (bay - 30) on mpr0.
        for bay in (30, 33, 57, 59):
            with self.subTest(bay=bay):
                members = self.rear_shelf_build(bay, camcontrol_target=150 + (bay - 30))
                self.assertEqual([item["mpr_slot"] for item in members], [str(bay - 30)])
                # Its own one event, not the next bay's three.
                self.assertEqual(members[0]["diagnostics"]["ioc_terminated_count"], 1)

    def test_rear_shelf_bay_without_cam_identity_attaches_nothing(self):
        # SES numbering is one-based; the MPR location is zero-based per
        # expander. Without identity evidence the bay must not take the next
        # bay's row.
        for bay in (30, 33, 57):
            with self.subTest(bay=bay):
                self.assertEqual(self.rear_shelf_build(bay), [])
        slot = self.slot(33, raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": 4})
        self.assertEqual(_slot_location_number_candidates(slot), [33])

    def test_cam_target_join_refuses_contradictory_or_foreign_rows(self):
        rows = [self.mpr_row(location, target=150 + location) for location in range(30)]
        cases = {
            # Two rows claim the member's target.
            "duplicate target": rows + [self.mpr_row(9, target=153)],
            # The member's target row sits on another enclosure.
            "other enclosure": [row for row in rows if " 153 " not in row] + [self.mpr_row(3, target=153, enclosure="0012")],
            # The controller reports targets, but not the member's.
            "target missing": [row for row in rows if " 153 " not in row],
            # The row names a different disk.
            "named other disk": [row for row in rows if " 153 " not in row] + [self.mpr_row(3, "SAS Target da6", target=153)],
        }
        enclosures = f"36 {self.CORE_ENCLOSURE} 0008 0002 SES\n36 {3:016x} 0008 0012 SES"
        for name, case_rows in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    self.rear_shelf_build(33, camcontrol_target=153, rows=case_rows, enclosures=enclosures),
                    [],
                )

    def test_cam_target_on_another_controller_is_not_this_paths_identity(self):
        # da5 is target 153 on mpr1; the member path is on mpr0, so the mpr0
        # row with target 153 says nothing about it.
        members = self.rear_shelf_build(33, camcontrol_target=None)
        self.assertEqual(members, [])
        fabric_members = self.rear_shelf_build_with_cam(33, "scbus13 on mpr1 bus 0:\n<Synthetic disk 0001>  at scbus13 target 153 lun 0 (da5,pass5)")
        self.assertEqual(fabric_members, [])

    def rear_shelf_build_with_cam(self, bay, camcontrol):
        rows = [self.mpr_row(location, target=150 + location) for location in range(30)]
        slot = self.slot(
            bay, device_name="da5",
            multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                MultipathMember(device_name="da5", controller_label="mpr0", state="ACTIVE")]),
            raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": bay - 29},
        )
        fabric = self.build([slot], "core", {
            "mprutil show adapters": MPR_ADAPTERS,
            "mprutil -u 0 show enclosures": f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
            "mprutil -u 0 show devices": "\n".join(rows),
            "camcontrol devlist -v": camcontrol,
        })
        return next(t for t in fabric.traces if t.id == f"bay:{bay}").metrics["mpr_devices"]

    def test_front_shelf_bay_still_matches_with_and_without_cam_identity(self):
        rows = "\n".join(self.mpr_row(location, target=150 + location) for location in range(30))
        for camcontrol in (None, "scbus12 on mpr0 bus 0:\n<Synthetic disk 0001>  at scbus12 target 155 lun 0 (da5,pass5)"):
            with self.subTest(camcontrol=bool(camcontrol)):
                outputs = {
                    "mprutil show adapters": MPR_ADAPTERS,
                    "mprutil -u 0 show enclosures": f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
                    "mprutil -u 0 show devices": rows,
                }
                if camcontrol:
                    outputs["camcontrol devlist -v"] = camcontrol
                slot = self.slot(5, device_name="da5",
                    multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                        MultipathMember(device_name="da5", controller_label="mpr0", state="ACTIVE")]),
                    raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": 6})
                fabric = self.build([slot], "core", outputs)
                members = next(t for t in fabric.traces if t.id == "bay:5").metrics["mpr_devices"]
                self.assertEqual([item["mpr_slot"] for item in members], ["5"])

    def test_missing_exact_mpr_location_does_not_attach_neighbor_diagnostics(self):
        fabric = self.core_build(5, 5, self.mpr_row(4))
        bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
        self.assertEqual(bay.metrics["mpr_devices"], [])

    def test_mpr_exact_and_zero_based_locations_keep_diagnostics(self):
        for number in (0, 5):
            with self.subTest(number=number):
                fabric = self.core_build(number, number, self.mpr_row(number))
                bay = next(trace for trace in fabric.traces if trace.id == f"bay:{number}")
                self.assertEqual([item["mpr_slot"] for item in bay.metrics["mpr_devices"]], [str(number)])
                self.assertEqual(bay.metrics["mpr_devices"][0]["diagnostics"]["ioc_terminated_count"], 1)

    def test_mpr_named_member_proves_one_based_conversion(self):
        # The provider names this member at location 4; SES reports 5.
        # This is independent device evidence, not permission to try every N-1.
        fabric = self.core_build(5, 5, self.mpr_row(4, "SAS Target da5"))
        bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
        self.assertEqual([item["mpr_slot"] for item in bay.metrics["mpr_devices"]], ["4"])

    def test_mpr_rejects_named_member_at_contradictory_location_or_enclosure(self):
        for row in (self.mpr_row(2, "SAS Target da5"), self.mpr_row(5, "SAS Target da5", enclosure="0012")):
            with self.subTest(row=row):
                fabric = self.core_build(5, 5, row)
                bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
                self.assertEqual(bay.metrics["mpr_devices"], [])

    def test_mpr_rejects_other_named_occupant_without_using_sas_address_as_identity(self):
        fabric = self.core_build(5, 5, self.mpr_row(5, "SAS Target da6"), sas_address=self.CORE_SAS_PORT)
        bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
        self.assertEqual(bay.metrics["mpr_devices"], [])
        # A multipath port may have a different SAS address. Location-only
        # provider rows must not be rejected by generic SAS-address equality.
        valid = self.core_build(5, 5, self.mpr_row(5), sas_address=f"{3:016x}")
        bay = next(trace for trace in valid.traces if trace.id == "bay:5")
        self.assertEqual(len(bay.metrics["mpr_devices"]), 1)

    def test_mpr_conflicting_provider_rows_remain_unknown_in_both_orders(self):
        cases = (
            (self.mpr_row(5), self.mpr_row(5, target=181)),
            (self.mpr_row(5, "SAS Target da5"), self.mpr_row(2, "SAS Target da5", target=181)),
        )
        for rows in cases:
            for order in (rows, rows[::-1]):
                with self.subTest(rows=order):
                    fabric = self.core_build(5, 5, "\n".join(order))
                    bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
                    self.assertEqual(bay.metrics["mpr_devices"], [])
        duplicate = self.mpr_row(5)
        fabric = self.core_build(5, 5, f"{duplicate}\n{duplicate}")
        bay = next(trace for trace in fabric.traces if trace.id == "bay:5")
        self.assertEqual(len(bay.metrics["mpr_devices"]), 1)

    def test_named_mpr_location_conflicts_reject_exact_and_one_based_matches(self):
        for location in (5, 4):
            first = self.mpr_row(location, "SAS Target da5")
            for distinct_port in (False, True):
                second = self.mpr_row(location, "SAS Target da6", target=181)
                if distinct_port:
                    second = second.replace("000a", "000b").replace(self.CORE_SAS_PORT, f"{4:016x}")
                for order in ((first, second), (second, first), (first, second, first)):
                    with self.subTest(location=location, distinct_port=distinct_port, rows=order):
                        fabric = self.core_build(5, 5, "\n".join(order))
                        bay = next(t for t in fabric.traces if t.id == "bay:5")
                        self.assertEqual(bay.metrics["mpr_devices"], [])
                        self.assertFalse(any(link.kind == "mpr-enclosure-bay" for link in fabric.links))

    def test_named_mpr_valid_duplicates_and_multipath_remain_attached(self):
        for number, location in ((0, 0), (5, 5), (5, 4)):
            row = self.mpr_row(location, "SAS Target da5")
            fabric = self.core_build(number, number, f"{row}\n{row}", sas_address=f"{3:016x}")
            bay = next(t for t in fabric.traces if t.id == f"bay:{number}")
            self.assertEqual(len(bay.metrics["mpr_devices"]), 1)
            self.assertEqual(bay.metrics["mpr_devices"][0]["diagnostics"]["ioc_terminated_count"], 1)
        # Two controller-local paths to the same observed bay are not a
        # contradiction. Their member names, handles and SAS ports can differ.
        slot = self.slot(5, device_name="multipath/synthetic", sas_address=f"{3:016x}",
            multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                MultipathMember(device_name="da5", controller_label="mpr0", state="ACTIVE"),
                MultipathMember(device_name="da6", controller_label="mpr1", state="PASSIVE")]),
            raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": 5})
        outputs = {
            "mprutil show adapters": MPR_ADAPTERS,
            "mprutil -u 0 show enclosures": f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
            "mprutil -u 1 show enclosures": f"36 {self.CORE_ENCLOSURE} 0008 0002 SES",
            "mprutil -u 0 show devices": self.mpr_row(5, "SAS Target da5"),
            "mprutil -u 1 show devices": self.mpr_row(5, "SAS Target da6", target=181).replace(
                "000a", "000b").replace(self.CORE_SAS_PORT, f"{4:016x}"),
        }
        for order in (outputs, dict(reversed(list(outputs.items())))):
            fabric = self.build([slot], "core", order)
            bay = next(t for t in fabric.traces if t.id == "bay:5")
            self.assertEqual({item["controller"] for item in bay.metrics["mpr_devices"]}, {"mpr0", "mpr1"})
            self.assertEqual({item["member_device_name"] for item in bay.metrics["mpr_devices"]}, {"da5", "da6"})

    def test_platform_canonical_path_alias_wins_with_exact_topology(self):
        path_id = "path:storage-v2:3a81d3f19a789954b11eb21e39a4556020f65aff43d5e6659280805e546df9ce"
        controller_id = "controller:storage-v2:86bacf459e77ffba8cf6d92c42a486bd8c5028ffb675e05ee108ad4b27d5a102"
        enclosure_id = "storage-enclosure:storage-v2:86bacf459e77ffba8cf6d92c42a486bd8c5028ffb675e05ee108ad4b27d5a102:synthetic-shelf"
        aliases = [
            SasFabricAlias(object_id="path:linux-block:pool-mirror", object_kind="path", label="Legacy name"),
            SasFabricAlias(object_id=path_id, object_kind="path", label="Exact name"),
        ]
        fabric = self.build([self.slot(device_name="sda", pool_name="Pool", vdev_name="mirror")], aliases=aliases)
        self.assertEqual(fabric.paths[0]["id"], path_id)
        self.assertEqual(fabric.paths[0]["alias"], "Exact name")
        traces = {t.id: t for t in fabric.traces}
        self.assertEqual(set(traces["bay:0"].node_ids), {
            "host", controller_id, path_id, enclosure_id, "backplane:0", "bay:0", "pool:Pool", "vdev:Pool/mirror",
        })
        self.assertEqual(set(traces[path_id].node_ids), {"host", controller_id, path_id})
        expected_edges = {("host", controller_id), (controller_id, path_id), (path_id, enclosure_id)}
        self.assertEqual({(link.source, link.target) for link in fabric.links if link.kind in {
            "host-controller", "controller-path", "path-storage-enclosure",
        }}, expected_edges)
        self.assertEqual(traces[path_id].alias, "Exact name")

    def test_platform_route_identity_preserves_pool_vdev_components_and_raw_devices(self):
        pairs = (("Pool", "pool", "mirror-0", "mirror-0"),
                 ("a/b", "a", "c", "b/c"), ("a b", "a-b", "x", "x"),
                 ("a%2Fb", "a/b", "x", "x"), ("pool", "pool", "Vdev", "vdev"))
        for pool_a, pool_b, vdev_a, vdev_b in pairs:
            slots = [self.slot(0, device_name="sda", pool_name=pool_a, vdev_name=vdev_a),
                     self.slot(1, device_name="sdb", pool_name=pool_b, vdev_name=vdev_b)]
            identities = []
            for order in (slots, slots[::-1]):
                with self.subTest(pools=(pool_a, pool_b), vdevs=(vdev_a, vdev_b), first=order[0].slot):
                    fabric = self.build(order)
                    self.assertEqual(len(fabric.paths), 2)
                    nodes = {node.id: node for node in fabric.nodes}
                    actual = {}
                    for path in fabric.paths:
                        self.assertEqual(path["count"], 1)
                        number = path["slots"][0]
                        self.assertEqual(nodes[path["id"]].raw["devices"], ["sda" if number == 0 else "sdb"])
                        actual[number] = path["id"]
                    self.assertNotEqual(actual[0], actual[1])
                    identities.append(actual)
            if len(identities) == 2:
                self.assertEqual(identities[0], identities[1])
                for slot in slots:
                    self.assertEqual(self.build([slot]).paths[0]["id"], identities[0][slot.slot])

    def test_platform_controller_identity_preserves_case_and_delimiters(self):
        for platform in ("esxi", "quantastor"):
            for left, right in (("C1", "c1"), ("C/1", "C-1"), ("C%2F1", "C/1")):
                slots = [self.slot(i, device_name=f"sd{chr(97 + i)}", pool_name="pool", vdev_name="mirror",
                                   raw_status={"controller_id": name}, operator_context={"selected_view_label": name})
                         for i, name in enumerate((left, right))]
                ids = []
                for order in (slots, slots[::-1]):
                    with self.subTest(platform=platform, names=(left, right), first=order[0].slot):
                        fabric = self.build(order, platform)
                        self.assertEqual(len(fabric.controllers), 2)
                        self.assertEqual(len(fabric.paths), 2)
                        self.assertEqual(sorted(c["related_slots"] for c in fabric.controllers), [[0], [1]])
                        nodes = {node.id: node for node in fabric.nodes}
                        links = {link.id: link for link in fabric.links}
                        for trace in fabric.traces:
                            self.assertTrue(set(trace.node_ids) <= nodes.keys())
                            self.assertTrue(set(trace.link_ids) <= links.keys())
                        ids.append({p["slots"][0]: p["id"] for p in fabric.paths})
                if len(ids) == 2:
                    self.assertEqual(ids[0], ids[1])

    def test_platform_source_identity_keeps_same_pool_routes_separate(self):
        slots = [self.slot(0, device_name="sda", pool_name="pool", vdev_name="mirror"),
                 self.slot(1, device_name="nvme0n1", pool_name="pool", vdev_name="mirror")]
        ids = []
        for order in (slots, slots[::-1]):
            fabric = self.build(order)
            self.assertEqual({p["source"] for p in fabric.paths}, {"linux_block", "linux_nvme"})
            self.assertEqual(sorted(p["slots"] for p in fabric.paths), [[0], [1]])
            ids.append({p["slots"][0]: p["id"] for p in fabric.paths})
        self.assertEqual(ids[0], ids[1])

    def test_platform_legacy_path_alias_follows_unique_route_without_store_migration(self):
        alias = SasFabricAlias(object_id="path:linux-block:pool-mirror", object_kind="path", label="Operator route")
        slot = self.slot(device_name="sda", pool_name="Pool", vdev_name="mirror")
        fabric = self.build([slot], aliases=[alias])
        path = fabric.paths[0]
        self.assertEqual(path.get("alias"), "Operator route")
        self.assertEqual(next(n for n in fabric.nodes if n.id == path["id"]).alias, "Operator route")
        self.assertEqual(next(t for t in fabric.traces if t.id == path["id"]).alias, "Operator route")
        self.assertEqual(alias.object_id, "path:linux-block:pool-mirror")

    def test_ambiguous_legacy_route_alias_is_retained_with_warning_not_guessed(self):
        alias = SasFabricAlias(object_id="path:linux-block:pool-mirror", object_kind="path", label="Operator route")
        slots = [self.slot(0, device_name="sda", pool_name="Pool", vdev_name="mirror"),
                 self.slot(1, device_name="sdb", pool_name="pool", vdev_name="mirror")]
        for order in (slots, slots[::-1]):
            fabric = self.build(order, aliases=[alias])
            self.assertEqual(len(fabric.paths), 2)
            self.assertTrue(all(not p.get("alias") for p in fabric.paths))
            self.assertIn(alias, fabric.aliases)
            self.assertTrue(any("ambiguous" in warning.lower() and "alias" in warning.lower() for warning in fabric.warnings))

    def test_platform_route_ids_fit_alias_contract_and_controller_name_lookup(self):
        slot = self.slot(device_name="sda", pool_name="Long pool/" * 30, vdev_name="Long vdev/" * 30)
        fabric = self.build([slot])
        path = fabric.paths[0]
        alias = SasFabricAlias(object_id=path["id"], object_kind="path", label="Long route")
        self.assertEqual(alias.object_id, path["id"])
        controller = fabric.controllers[0]
        self.assertEqual(controller["id"], f"controller:{controller['name']}")
        named = self.build([slot], aliases=[alias])
        self.assertEqual(named.paths[0].get("alias"), "Long route")

    def test_platform_legacy_controller_and_enclosure_aliases_remain_visible(self):
        aliases = [
            SasFabricAlias(object_id="controller:linux-block", object_kind="controller", label="Source name"),
            SasFabricAlias(object_id="storage-enclosure:linux-block:synthetic-shelf",
                           object_kind="storage-enclosure", label="Shelf name"),
        ]
        fabric = self.build([self.slot(device_name="sda", pool_name="pool")], aliases=aliases)
        self.assertEqual(next(n for n in fabric.nodes if n.kind == "controller").alias, "Source name")
        self.assertEqual(next(n for n in fabric.nodes if n.kind == "storage-enclosure").alias, "Shelf name")
        # Read compatibility does not remove or mutate the supplied records.
        for alias in aliases:
            self.assertIn(alias, fabric.aliases)

    def test_platform_controller_labels_are_separate_from_canonical_joins(self):
        for platform in ("linux", "scale", "esxi", "quantastor", "ipmi"):
            with self.subTest(platform=platform):
                fabric = self.build([self.slot(device_name="sda", health="healthy")], platform)
                controller = fabric.controllers[0]
                node = next(n for n in fabric.nodes if n.id == controller["id"])
                member = next(t for t in fabric.traces if t.id == "bay:0").metrics["path_states"][0]
                self.assertEqual(controller.get("label"), node.label)
                self.assertEqual(member.get("controller_label"), node.label)
                self.assertEqual(fabric.paths[0].get("controller_label"), node.label)
                self.assertNotIn("storage-v2:", node.label)
                self.assertEqual(controller["id"], f"controller:{controller['name']}")
                self.assertEqual(member["controller"], controller["name"])
                self.assertEqual(fabric.paths[0]["controller"], controller["name"])

    def partial_mpr_build(self, rows, *, enclosures=None, controller="mpr0"):
        slot = self.slot(5, device_name="da5",
            multipath=MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                MultipathMember(device_name="da5", controller_label=controller, state="ACTIVE")]),
            raw_status={"enclosure_id": self.CORE_ENCLOSURE, "ses_slot_number": 5})
        outputs = {
            "mprutil show adapters": MPR_ADAPTERS,
            "mprutil -u 0 show devices": rows,
            CORE_MPR_DMESG_EVENTS_COMMAND: "mpr0: Controller reported scsi ioc terminated tgt 180 SMID 1 loginfo 31120302",
        }
        if enclosures is not None:
            outputs["mprutil -u 0 show enclosures"] = enclosures
        return self.build([slot], "core", outputs)

    def test_named_mpr_partial_enclosure_evidence_keeps_proven_diagnostics(self):
        for location in (5, 4):
            row = self.mpr_row(location, "SAS Target da5")
            for enclosures in (None, "", f"36 {3:016x} 0008 0012 SES", f"36 {self.CORE_ENCLOSURE} 0008 0002 SES"):
                with self.subTest(location=location, enclosures=enclosures):
                    fabric = self.partial_mpr_build(f"{row}\n{row}", enclosures=enclosures)
                    members = next(t for t in fabric.traces if t.id == "bay:5").metrics["mpr_devices"]
                    self.assertEqual(len(members), 1)
                    self.assertEqual(members[0]["diagnostics"]["ioc_terminated_count"], 1)
                    self.assertEqual(members[0]["mpr_slot"], str(location))

    def test_partial_mpr_evidence_still_rejects_conflicts_and_unproven_offsets(self):
        cases = [
            (self.mpr_row(5, "SAS Target da5"), f"36 {3:016x} 0008 0002 SES", "mpr0"),
            (self.mpr_row(5, "SAS Target da5"), None, "mpr1"),
            (self.mpr_row(3, "SAS Target da5"), None, "mpr0"),
            (self.mpr_row(4), None, "mpr0"),
            (self.mpr_row(5, "SAS Target da6"), None, "mpr0"),
            (self.mpr_row(4, "SAS Target da5") + "\n" + self.mpr_row(5, "SAS Target da5"), None, "mpr0"),
            (self.mpr_row(5, "SAS Target da5") + "\n" + self.mpr_row(4, "SAS Target da5"), None, "mpr0"),
        ]
        for location in (5, 4):
            first = self.mpr_row(location, "SAS Target da5")
            other = self.mpr_row(location, "SAS Target da6", target=181)
            for rows in ((first, other), (other, first), (first, other, first)):
                cases.append(("\n".join(rows), None, "mpr0"))
        for rows, enclosures, controller in cases:
            with self.subTest(rows=rows, enclosures=enclosures, controller=controller):
                fabric = self.partial_mpr_build(rows, enclosures=enclosures, controller=controller)
                self.assertEqual(next(t for t in fabric.traces if t.id == "bay:5").metrics["mpr_devices"], [])

    def test_platform_shared_path_health_is_order_independent_member_evidence(self):
        from itertools import permutations

        cases = ((["healthy", "fault"], "mixed"), (["healthy", "unknown"], "unknown"),
                 (["healthy", "healthy"], "healthy"), (["fault", "fault"], "fault"),
                 (["healthy", "fault", "unknown"], "mixed"), (["unknown", "unknown"], "unknown"))
        for states, expected in cases:
            slots = [self.slot(i, device_name=f"sd{chr(97 + i)}", pool_name="pool", vdev_name="mirror", health=state)
                     for i, state in enumerate(states)]
            for order in permutations(slots):
                with self.subTest(states=states, order=[s.slot for s in order]):
                    fabric = self.build(list(order))
                    self.assertEqual(len(fabric.paths), 1)
                    path = fabric.paths[0]
                    self.assertEqual(path["state"], expected)
                    self.assertEqual(path.get("state_basis"), "member-health-not-transport")
                    self.assertEqual(path["member_state_counts"], {s: states.count(s) for s in set(states)})
                    nodes = {node.id: node for node in fabric.nodes}
                    self.assertEqual(nodes[path["id"]].status, expected)
                    self.assertEqual(nodes[path["id"]].metrics["member_state_counts"], path["member_state_counts"])
                    path_links = [link for link in fabric.links if link.kind in {"controller-path", "path-storage-enclosure"}]
                    self.assertTrue(path_links)
                    self.assertEqual({link.status for link in path_links}, {expected})
                    path_trace = next(t for t in fabric.traces if t.id == path["id"])
                    self.assertEqual(path_trace.metrics["state"], expected)
                    for slot in slots:
                        bay = next(t for t in fabric.traces if t.id == f"bay:{slot.slot}")
                        self.assertEqual(bay.metrics["path_states"][0]["state"], slot.health)
                        self.assertEqual(bay.metrics["path_states"][0]["member_state"], slot.health)
                        self.assertEqual(nodes[f"bay:{slot.slot}"].status, slot.health)
                    # Member faults do not establish a controller/transport fault.
                    self.assertEqual({nodes[c["id"]].status for c in fabric.controllers}, {"online"})


class SystemSasFabricDraftTests(unittest.TestCase):
    """Inactive composition contract, not a replacement for cascade admission."""

    @staticmethod
    def payload(enclosure="synthetic-head", system="synthetic-system", unit=0):
        address = f"{1:016x}"
        fabric = build_sas_fabric_snapshot(
            system=SystemConfig(id=system, truenas=TrueNASConfig(platform="core")),
            snapshot=InventorySnapshot(
                slots=[FabricPathRegressionTests.slot(0)], refresh_interval_seconds=30,
                selected_enclosure_id=address,
            ),
            ssh_outputs={
                "mprutil show adapters": f"/dev/mpr{unit} SAS3516 Synthetic 1.0",
                f"mprutil -u {unit} show expanders": f"36 {address} 0009 0001 0002 1",
            },
        )
        payload = fabric.model_dump(mode="json")
        payload.update(selected_enclosure_id=enclosure, generated_at="2026-01-01T00:00:00Z")
        return payload

    @staticmethod
    def compose(payloads, **overrides):
        from app.services.system_sas_fabric import compose_system_sas_fabrics

        return compose_system_sas_fabrics(
            payloads, **{"max_input_bytes": 4 * 1024 * 1024, "max_depth": 40, **overrides},
        )

    @classmethod
    def cascade_payload(cls, enclosure="synthetic-head"):
        from app.models.domain import SasFabricLink, SasFabricNode

        payload = cls.payload(enclosure)
        # Supplied graph edge only. This fixture does not implement/adopt #970.
        rows = parse_mpr_expanders(
            f"36 {1:016x} 0009 0001 0002 1\n"
            "  4 8 0019 12.0 1.5 12.0 Expander\n"
            "  5 9 0019 12.0 1.5 12.0 Expander\n"
            f"36 {2:016x} 0019 0009 0012 2\n"
            "  8 4 0009 12.0 1.5 12.0 Expander\n"
            "  9 5 0009 12.0 1.5 12.0 Expander"
        )
        payload["nodes"] = [n for n in payload["nodes"] if n["kind"] != "expander"]
        for row in rows:
            payload["nodes"].append(SasFabricNode(
                id=f"expander:controller:mpr0:{row['sas_address']}", kind="expander",
                label="Synthetic expander", raw_id=row["sas_address"], controller_id="controller:mpr0",
                related_slots=[0], evidence=["mprutil -u 0 show expanders"], raw=row,
            ).model_dump(mode="json"))
        source, target = [n["id"] for n in payload["nodes"] if n["kind"] == "expander"]
        payload["links"] = [link for link in payload["links"] if link["kind"] != "controller-expander"]
        payload["links"].append(SasFabricLink(
            id="synthetic-cascade", source=source, target=target, kind="expander-expander",
            evidence=["synthetic supplied edge"], related_slots=[0],
        ).model_dump(mode="json"))
        payload["traces"] = []
        return payload

    def test_same_system_deduplicates_shared_hba_and_expander_without_losing_bays(self):
        first, second = self.payload(), self.payload("synthetic-jbod")
        before = json.dumps([first, second], sort_keys=True)
        graph = self.compose([first, second])["systems"][0]
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "controller"]), 1)
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "expander"]), 1)
        bays = [n for n in graph["nodes"] if n["kind"] == "bay"]
        self.assertEqual(len(bays), 2)
        self.assertEqual({n["observations"][0]["enclosure_id"] for n in bays}, {"synthetic-head", "synthetic-jbod"})
        self.assertTrue(all(n["observations"][0]["payload"]["related_slots"] == [0] for n in bays))
        self.assertEqual(json.dumps([first, second], sort_keys=True), before)

    def test_system_and_controller_identity_never_collide(self):
        result = self.compose([self.payload(), self.payload(system="synthetic-other"), self.payload("synthetic-jbod", unit=1)])
        self.assertEqual(len(result["systems"]), 2)
        ids = [n["id"] for graph in result["systems"] for n in graph["nodes"]]
        self.assertEqual(len(ids), len(set(ids)))
        own = next(g for g in result["systems"] if g["system_id"] == "synthetic-system")
        self.assertEqual(len([n for n in own["nodes"] if n["kind"] == "expander"]), 2)

    def test_missing_authoritative_scope_and_duplicate_enclosure_are_refused(self):
        for field in ("system_id", "selected_enclosure_id"):
            payload = self.payload()
            payload[field] = None
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "scope"):
                self.compose([payload])
        with self.assertRaisesRegex(ValueError, "duplicate enclosure"):
            self.compose([self.payload(), self.payload()])

    def test_conflicting_controller_identity_is_not_shared(self):
        first, second = self.payload(), self.payload("synthetic-jbod")
        for payload, address in ((first, "0000:01:00.0"), (second, "0000:02:00.0")):
            next(n for n in payload["nodes"] if n["kind"] == "controller")["metrics"]["pci_address"] = address
        graph = self.compose([first, second])["systems"][0]
        controllers = [n for n in graph["nodes"] if n["kind"] == "controller"]
        self.assertEqual(len(controllers), 2)
        self.assertTrue(all(n["identity_state"] == "conflicting" for n in controllers))
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "expander"]), 2)

    def test_handle_only_expander_identity_stays_enclosure_local(self):
        payloads = [self.payload(), self.payload("synthetic-jbod")]
        for payload in payloads:
            next(n for n in payload["nodes"] if n["kind"] == "expander")["raw"].pop("sas_address")
        graph = self.compose(payloads)["systems"][0]
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "expander"]), 2)

    def test_supplied_cascade_and_reported_reciprocal_lanes_are_preserved(self):
        graph = self.compose([self.cascade_payload()])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"], {
            "state": "observed", "lane_count": 2, "source_phys": [4, 5], "target_phys": [8, 9],
            "phy_pairs": [[4, 8], [5, 9]], "basis": "reciprocal_reported_phy_rows",
        })
        self.assertEqual(link["observations"][0]["payload"]["evidence"], ["synthetic supplied edge"])
        self.assertFalse(any(link["kind"] == "controller-expander" for link in graph["links"]))

    def test_case_alias_endpoint_handles_never_admit_reciprocal_lanes(self):
        payload = self.cascade_payload()
        left, right = [node for node in payload["nodes"] if node["kind"] == "expander"]
        left["raw"]["dev_handle"], right["raw"]["dev_handle"] = "a00b", "A00B"
        for own, peer in ((left, right), (right, left)):
            for phy in own["raw"]["phys"]:
                phy["dev_handle"] = peer["raw"]["dev_handle"]
        graph = self.compose([payload])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"]["state"], "unknown")
        self.assertIsNone(link["wide_port"]["lane_count"])

    def test_distinct_hex_handles_match_phy_rows_without_changing_raw_evidence(self):
        payload = self.cascade_payload()
        left, right = [node for node in payload["nodes"] if node["kind"] == "expander"]
        left["raw"]["dev_handle"], right["raw"]["dev_handle"] = "a00b", "c00d"
        for own, peer in ((left, right), (right, left)):
            for phy in own["raw"]["phys"]:
                phy["dev_handle"] = peer["raw"]["dev_handle"].upper()
        before = json.dumps(payload, sort_keys=True)
        graph = self.compose([payload])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"]["state"], "observed")
        self.assertEqual(link["wide_port"]["lane_count"], 2)
        self.assertEqual(json.dumps(payload, sort_keys=True), before)
        observations = [observation["payload"] for node in graph["nodes"]
                        if node["kind"] == "expander" for observation in node["observations"]]
        self.assertEqual({node["raw"]["dev_handle"] for node in observations}, {"a00b", "c00d"})

    def test_case_alias_duplicate_handle_is_ambiguous_within_controller(self):
        payload = self.cascade_payload()
        left, right = [node for node in payload["nodes"] if node["kind"] == "expander"]
        left["raw"]["dev_handle"], right["raw"]["dev_handle"] = "a00b", "c00d"
        for own, peer in ((left, right), (right, left)):
            for phy in own["raw"]["phys"]:
                phy["dev_handle"] = peer["raw"]["dev_handle"]
        duplicate = json.loads(json.dumps(left))
        duplicate.update(id="synthetic-duplicate-expander", raw_id=f"{3:016x}")
        duplicate["raw"].update(sas_address=f"{3:016x}", dev_handle="A00B", phys=[])
        payload["nodes"].append(duplicate)
        graph = self.compose([payload])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"]["state"], "unknown")
        self.assertEqual(link["wide_port"]["reason"], "ambiguous_endpoint_handle")

    def test_shared_lanes_are_not_summed_and_permutations_are_deterministic(self):
        first, second = self.cascade_payload(), self.cascade_payload("synthetic-jbod")
        expected = self.compose([first, second])
        for payload in (first, second):
            payload["nodes"].reverse()
            payload["links"].reverse()
            for node in payload["nodes"]:
                if "phys" in node["raw"]:
                    node["raw"]["phys"].reverse()
        self.assertEqual(self.compose([second, first]), expected)
        link = next(link for link in expected["systems"][0]["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"]["lane_count"], 2)
        self.assertEqual(len(link["observations"]), 2)

    def test_missing_phy_evidence_never_becomes_zero_or_linked_phy_count(self):
        payload = self.payload()
        graph = self.compose([payload])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "controller-expander")
        self.assertEqual(link["wide_port"]["state"], "unknown")
        self.assertIsNone(link["wide_port"]["lane_count"])
        self.assertIsNone(link["wide_port"]["source_phys"])
        self.assertIsNone(link["wide_port"]["target_phys"])

    def test_contradictory_or_partial_or_duplicate_phy_evidence_stays_unknown(self):
        for change in ("remote", "duplicate", "missing", "handle", "type"):
            payload = self.cascade_payload()
            peer = [n for n in payload["nodes"] if n["kind"] == "expander"][1]
            if change == "remote":
                peer["raw"]["phys"][0]["remote_phy"] = "7"
            elif change == "duplicate":
                peer["raw"]["phys"].append(dict(peer["raw"]["phys"][0]))
            elif change == "missing":
                peer["raw"]["phys"].pop()
            elif change == "handle":
                peer["raw"]["dev_handle"] = None
            else:
                peer["raw"]["phys"][0]["device"] = "No Device"
            with self.subTest(change=change):
                graph = self.compose([payload])["systems"][0]
                link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
                self.assertEqual(link["wide_port"]["state"], "unknown")
                self.assertIsNone(link["wide_port"]["lane_count"])

    def test_conflicting_reports_do_not_promote_one_shelfs_lanes(self):
        first, second = self.cascade_payload(), self.cascade_payload("synthetic-jbod")
        next(n for n in second["nodes"] if n["kind"] == "expander")["raw"]["phys"] = []
        graph = self.compose([first, second])["systems"][0]
        link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
        self.assertEqual(link["wide_port"]["state"], "unknown")
        self.assertEqual(len(link["observations"]), 2)

    def test_sources_warnings_unknowns_and_local_trace_references_survive(self):
        payloads = [self.payload(), self.payload("synthetic-jbod")]
        payloads[0]["warnings"] = ["Synthetic missing evidence"]
        payloads[0]["raw"]["future_evidence"] = {"unknown": None}
        result = self.compose(payloads)["systems"][0]
        enclosure = next(e for e in result["enclosures"] if e["enclosure_id"] == "synthetic-head")
        self.assertEqual(enclosure["snapshot"]["warnings"], payloads[0]["warnings"])
        self.assertEqual(enclosure["snapshot"]["raw"], payloads[0]["raw"])
        all_nodes, all_links = {n["id"] for n in result["nodes"]}, {link["id"] for link in result["links"]}
        for enclosure in result["enclosures"]:
            for trace in enclosure["traces"]:
                self.assertLessEqual(set(trace["node_ids"]), all_nodes)
                self.assertLessEqual(set(trace["link_ids"]), all_links)
                self.assertEqual(trace["observation"]["slots"], [0])

    def test_malformed_graph_structure_is_explicitly_refused(self):
        for change in ("duplicate_node", "dangling_link", "dangling_controller", "dangling_trace", "bad_node"):
            payload = self.payload()
            if change == "duplicate_node":
                payload["nodes"].append(dict(payload["nodes"][0]))
            elif change == "dangling_link":
                payload["links"][0]["target"] = "absent"
            elif change == "dangling_controller":
                next(n for n in payload["nodes"] if n["kind"] == "expander")["controller_id"] = "absent"
            elif change == "dangling_trace":
                payload["traces"][0]["node_ids"].append("absent")
            else:
                payload["nodes"][0]["kind"] = 7
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.compose([payload])

    def test_explicit_caller_budgets_refuse_size_depth_cycles_and_non_json(self):
        with self.assertRaisesRegex(ValueError, "byte budget"):
            self.compose([self.payload()], max_input_bytes=100)
        with self.assertRaisesRegex(ValueError, "depth budget"):
            self.compose([self.payload()], max_depth=2)
        payload = self.payload()
        payload["raw"]["cycle"] = payload
        with self.assertRaises(ValueError):
            self.compose([payload])
        for value in (float("nan"), object()):
            payload = self.payload()
            payload["raw"]["invalid"] = value
            with self.assertRaises(ValueError):
                self.compose([payload])
        for limits in ({"max_input_bytes": 0}, {"max_depth": True}):
            with self.assertRaises(ValueError):
                self.compose([], **limits)

    def test_ambiguous_handles_or_reused_local_phys_are_not_lane_evidence(self):
        for change in ("handle_collision", "reused_phy", "raw_controller_conflict"):
            payload = self.cascade_payload()
            source, target = [n for n in payload["nodes"] if n["kind"] == "expander"]
            if change == "handle_collision":
                duplicate = json.loads(json.dumps(target))
                duplicate.update(id="synthetic-ambiguous-peer", raw_id=f"{3:016x}")
                duplicate["raw"]["sas_address"] = f"{3:016x}"
                payload["nodes"].append(duplicate)
            elif change == "reused_phy":
                source["raw"]["phys"].append({**source["raw"]["phys"][0], "dev_handle": "0029"})
            else:
                source["raw"]["controller_id"] = "controller:mpr1"
            with self.subTest(change=change):
                graph = self.compose([payload])["systems"][0]
                link = next(link for link in graph["links"] if link["kind"] == "expander-expander")
                self.assertEqual(link["wide_port"]["state"], "unknown")
                self.assertIsNone(link["wide_port"]["lane_count"])

    def test_duplicate_sas_identity_in_one_enclosure_is_refused(self):
        payload = self.cascade_payload()
        duplicate = json.loads(json.dumps(next(n for n in payload["nodes"] if n["kind"] == "expander")))
        duplicate["id"] = "synthetic-duplicate-identity"
        payload["nodes"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "ambiguous shared node identity"):
            self.compose([payload])

    def test_scope_tuple_encoding_preserves_case_and_delimiters(self):
        payloads = [self.payload("c", "synthetic:a:b"), self.payload("b:c", "synthetic:a"),
                    self.payload("c", "Synthetic:a:b")]
        result = self.compose(payloads)
        ids = [n["id"] for graph in result["systems"] for n in graph["nodes"]]
        self.assertEqual(len(result["systems"]), 3)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.compose(list(reversed(payloads))), result)

    def test_distinct_supplied_parallel_edges_and_trace_membership_are_preserved(self):
        from app.models.domain import SasFabricLink, SasFabricNode, SasFabricSnapshot, SasFabricTrace

        payload = SasFabricSnapshot(
            system_id="synthetic-system", selected_enclosure_id="synthetic-head",
            nodes=[SasFabricNode(id="h", kind="host", label="Synthetic"),
                   SasFabricNode(id="c", kind="controller", label="Synthetic controller")],
            links=[SasFabricLink(id=f"l{slot}", source="h", target="c", kind="host-controller", related_slots=[slot])
                   for slot in (0, 1)],
            traces=[SasFabricTrace(id=f"t{slot}", kind="path", label="Synthetic path", link_ids=[f"l{slot}"], slots=[slot])
                    for slot in (0, 1)],
        ).model_dump(mode="json")
        graph = self.compose([payload])["systems"][0]
        self.assertEqual(len(graph["links"]), 2)
        traces = graph["enclosures"][0]["traces"]
        self.assertNotEqual(traces[0]["link_ids"], traces[1]["link_ids"])
        by_id = {link["id"]: link for link in graph["links"]}
        for trace in traces:
            link = by_id[trace["link_ids"][0]]
            self.assertEqual(link["observations"][0]["payload"]["related_slots"], trace["observation"]["slots"])

    def test_malformed_interpreted_raw_fields_refuse_with_value_error(self):
        import os
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "scripts" / "preview_system_sas_fabric.py"),
                   "--max-input-bytes", str(4 * 1024 * 1024), "--max-depth", "40"]
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        for field, value in (("dev_handle", []), ("dev_handle", {}), ("controller_id", []),
                             ("sas_address", {}), ("phys", {}), ("device", []), ("device", {})):
            payload = self.cascade_payload()
            peer = [n for n in payload["nodes"] if n["kind"] == "expander"][1]
            if field == "device":
                peer["raw"]["phys"][0][field] = value
            else:
                peer["raw"][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    self.compose([payload])
                child = subprocess.run(command, input=json.dumps([payload]), text=True, capture_output=True,
                                       cwd=root, env=environment, timeout=10)
                self.assertEqual(child.returncode, 2)
                self.assertEqual(child.stdout, "")
                self.assertNotIn("Traceback", child.stderr)

    def test_preview_cli_reads_stdin_and_file_and_refuses_bad_input(self):
        import os
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "scripts" / "preview_system_sas_fabric.py"),
                   "--max-input-bytes", str(4 * 1024 * 1024), "--max-depth", "40"]
        payload = json.dumps([self.payload()])
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.json"
            source.write_text(payload)
            stdin = subprocess.run(command, input=payload, text=True, capture_output=True, cwd=directory, env=environment, timeout=10)
            file = subprocess.run([*command, str(source)], text=True, capture_output=True, cwd=directory, env=environment, timeout=10)
            refusals = [subprocess.run(command, input=data, text=True, capture_output=True, cwd=directory, env=environment, timeout=10)
                        for data in ("[{}]", "{\"duplicate\":1,\"duplicate\":2}", "[NaN]", "[" * 10000, "[" + " " * (4 * 1024 * 1024))]
        self.assertEqual(stdin.returncode, 0, stdin.stderr)
        self.assertEqual(file.returncode, 0, file.stderr)
        self.assertEqual(stdin.stdout, file.stdout)
        self.assertEqual(json.loads(stdin.stdout), self.compose(json.loads(payload)))
        self.assertTrue(all(bad.returncode == 2 and bad.stdout == "" for bad in refusals))


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


# --- #941 CORE expander PHY error counters ----------------------------------

SMP_EXPANDER_A = "5000c50000000001"
SMP_EXPANDER_B = "5000c50000000002"
SMP_EXPANDERS = f"""
Num Phys SAS Address      DevHandle Parent EncHandle SAS Level
4        {SMP_EXPANDER_A} 0009      0001   0002      1
  0 1 000a 12.0 1.5 12.0 End Device
  1 2 000b 12.0 1.5 12.0 End Device
  2       1.5 12.0 No Device
  3       1.5 12.0 No Device
2        {SMP_EXPANDER_B} 0019      0001   0012      1
  0 1 001a 12.0 1.5 12.0 End Device
""".strip()
SMP_DEVICES = """
B____T    SAS Address      Handle  Parent    Device        Speed Enc  Slot  Wdt
00   12   5000c50000000011 000b    0009      SAS Target    12    0002 1     1
00   10   5000c50000000010 000a    0009      SAS Target    12    0002 0     1
00   30   5000c50000000020 001a    0019      SAS Target    12    0012 0     1
""".strip()
SMP_DEVLIST = """
scbus4 on mpr0 bus 0:
<SYNTHETIC DISK 0001>  at scbus4 target 10 lun 0 (pass10,da10)
<SYNTHETIC DISK 0002>  at scbus4 target 12 lun 0 (da12,pass12)
<SYNTHETIC DISK 0003>  at scbus4 target 30 lun 0 (pass30,da30)
scbus5 on mpr1 bus 0:
<SYNTHETIC DISK 0004>  at scbus5 target 10 lun 0 (pass99,da99)
""".strip()


def _smp_frame(phy: int, counters=(1, 2, 3, 4), *, result: int = 0) -> bytes:
    header = bytes((0x41, 0x11, result, 6))
    body = (7).to_bytes(2, "big") + bytes(3) + bytes((phy,)) + bytes(2)
    return header + body + b"".join(value.to_bytes(4, "big") for value in counters)


def _od(data: bytes) -> str:
    """Bytes as `od -An -tx1 -v` prints them: 16 per line, leading spaces."""
    return "\n".join(
        "".join(f" {byte:02x}" for byte in data[index:index + 16]) for index in range(0, len(data), 16)
    )


def _smp_output(unit: int, address: str, device: str, per_phy: dict, *, end: bool = True) -> str:
    phys = sorted(per_phy)
    lines = [f"tnj-smp-phy-error-log:{unit}:{address}:{device}:{phys[0]:02x}-{phys[-1]:02x}"]
    for phy in phys:
        lines.append(f"@phy={phy:02x}")
        value = per_phy[phy]
        if value is None:
            lines.append("@no-response")
            break
        data = value.encode() if isinstance(value, str) else value
        if data:
            lines.append(_od(data))
    if end:
        lines.append("@end")
    return "\n".join(lines) + "\n"


_SUDO_REFUSAL = "Sorry, user jbodmap is not allowed to execute '/sbin/camcontrol smpcmd pass10' as root on host.\n"


class CoreExpanderCounterTests(unittest.TestCase):
    def outputs(self, **extra):
        outputs = {
            "mprutil -u 0 show expanders": SMP_EXPANDERS,
            "mprutil -u 0 show devices": SMP_DEVICES,
            "camcontrol devlist -v": SMP_DEVLIST,
        }
        outputs.update(extra)
        return outputs

    def test_discovery_uses_only_existing_reads_and_the_lowest_routed_target(self):
        from app.services.core_expander_counters import SmpTarget, discover_smp_targets

        targets = discover_smp_targets(self.outputs())
        self.assertEqual(targets, [
            SmpTarget(0, SMP_EXPANDER_A, "pass10", 4),
            SmpTarget(0, SMP_EXPANDER_B, "pass30", 2),
        ])
        # Without CAM's target-to-pass map there is no route, so nothing runs.
        self.assertEqual(discover_smp_targets(self.outputs(**{"camcontrol devlist -v": ""})), [])
        # The same target number on another controller is another device.
        other = SMP_DEVLIST.replace("on mpr0", "on mpr7")
        self.assertEqual(discover_smp_targets(self.outputs(**{"camcontrol devlist -v": other})), [])

    def test_discovery_refuses_ambiguous_or_malformed_rows(self):
        from app.services.core_expander_counters import discover_smp_targets, parse_camcontrol_pass_devices

        twice = SMP_DEVLIST + "\nscbus4 on mpr0 bus 0:\n<X> at scbus4 target 10 lun 1 (pass11,da11)"
        routes = parse_camcontrol_pass_devices(twice)
        self.assertNotIn(("mpr0", 10), routes)
        self.assertEqual(routes[("mpr0", 12)], "pass12")
        self.assertEqual(
            [target.device for target in discover_smp_targets(self.outputs(**{"camcontrol devlist -v": twice}))],
            ["pass12", "pass30"],
        )
        bad = SMP_EXPANDERS.replace("4        5000", "300      5000")
        self.assertEqual([t.expander_sas_address for t in discover_smp_targets(self.outputs(**{
            "mprutil -u 0 show expanders": bad,
        }))], [SMP_EXPANDER_B])

    def test_command_is_one_sh_line_with_the_granted_shape_and_a_stable_key(self):
        from app.services.core_expander_counters import SmpTarget, build_phy_error_log_command

        command = build_phy_error_log_command(SmpTarget(0, SMP_EXPANDER_A, "pass10", 4))
        self.assertNotIn("\n", command)
        self.assertIn("for p in 00 01 02 03;", command)
        self.assertIn(
            '/usr/bin/timeout -k 1 2 sudo -n /sbin/camcontrol smpcmd pass10 -r 12 '
            '"40{}11{}06{}02{}00{}00{}00{}00{}00{}$p{}00{}00" -R 28 - 2>&1 | /usr/bin/od -An -tx1 -v',
            command,
        )
        self.assertEqual(canonicalize_ssh_command(command), f"smp phy error log 0:{SMP_EXPANDER_A}:pass10:00-03")
        probe = build_phy_error_log_command(SmpTarget(0, SMP_EXPANDER_A, "pass10", 4), [0])
        self.assertNotEqual(canonicalize_ssh_command(probe), canonicalize_ssh_command(command))
        for target in (SmpTarget(0, "not-an-address", "pass10", 4), SmpTarget(0, SMP_EXPANDER_A, "pass10;x", 4)):
            with self.subTest(target=target), self.assertRaises(ValueError):
                build_phy_error_log_command(target)

    def test_output_parser_decodes_frames_and_never_turns_failures_into_zeros(self):
        from app.services.core_expander_counters import CAPABILITY_GRANTED, parse_phy_error_log_output

        stdout = _smp_output(0, SMP_EXPANDER_A, "pass10", {
            0: _smp_frame(0, (5, 0, 1, 0xFFFFFFFF)),
            1: _smp_frame(1, (0, 0, 0, 0)),
            2: _smp_frame(2, (0, 0, 0, 0), result=0x10)[:4],
            3: "camcontrol: error sending command\n",
        })
        result = parse_phy_error_log_output(stdout)
        self.assertEqual((result.unit, result.expander_sas_address), (0, SMP_EXPANDER_A))
        self.assertEqual(result.capability, CAPABILITY_GRANTED)
        rows = {row["phy"]: row for row in result.rows}
        self.assertEqual(rows[0]["counter_state"], "measured")
        self.assertEqual(rows[0]["phy_reset_problem_count"], 0xFFFFFFFF)
        self.assertEqual(rows[1]["invalid_dword_count"], 0)
        self.assertEqual((rows[2]["counter_state"], rows[2]["counter_reason"]), ("unknown", "function_result"))
        self.assertEqual((rows[3]["counter_state"], rows[3]["counter_reason"]), ("unknown", "command_failed"))
        for phy in (2, 3):
            self.assertTrue(all(rows[phy][field] is None for field in (
                "invalid_dword_count", "running_disparity_error_count",
                "loss_of_dword_synchronization_count", "phy_reset_problem_count",
            )))

    def test_output_parser_classifies_refusal_timeouts_and_truncation(self):
        from app.services.core_expander_counters import CAPABILITY_DENIED, parse_phy_error_log_output

        denied = parse_phy_error_log_output(_smp_output(0, SMP_EXPANDER_A, "pass10", {0: _SUDO_REFUSAL}))
        self.assertEqual(denied.capability, CAPABILITY_DENIED)
        self.assertEqual(denied.rows[0]["counter_state"], "permission_denied")
        password = parse_phy_error_log_output(_smp_output(0, SMP_EXPANDER_A, "pass10", {
            0: "sudo: a password is required\n",
        }))
        self.assertEqual(password.capability, CAPABILITY_DENIED)
        hung = parse_phy_error_log_output(_smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0), 1: None}))
        self.assertEqual(hung.capability, "granted")
        self.assertEqual([row["counter_reason"] for row in hung.rows], [None, "no_response"])
        cut = parse_phy_error_log_output(_smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0)}, end=False))
        self.assertEqual(cut.rows[0]["counter_state"], "unknown")
        self.assertEqual(cut.rows[0]["counter_reason"], "incomplete_output")
        only_hung = parse_phy_error_log_output(_smp_output(0, SMP_EXPANDER_A, "pass10", {0: None}))
        self.assertIsNone(only_hung.capability)
        forged = parse_phy_error_log_output("tnj-smp-phy-error-log:0:zz:pass10:00-00\n@phy=00\n@end\n")
        self.assertIsNone(forged.unit)

    def snapshot(self, selected):
        # Storage Fabric keeps the selected enclosure's expanders (and their
        # verified ancestors) only, so each check selects the expander it reads.
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, present=False, state=SlotState.empty)
        return InventorySnapshot(
            slots=[slot], refresh_interval_seconds=30, selected_system_platform="core",
            selected_enclosure_id=selected,
        )

    def fabric(self, outputs, capability=None, selected=SMP_EXPANDER_A):
        return build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
            snapshot=self.snapshot(selected),
            ssh_outputs=outputs,
            expander_counter_capability=capability,
        )

    def expander(self, fabric, address):
        return next(node for node in fabric.nodes if node.kind == "expander" and node.raw.get("sas_address") == address)

    def test_fabric_attaches_counters_to_expander_phy_rows(self):
        key_a = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-03 ; ..."
        fabric_outputs = self.outputs(**{key_a: _smp_output(0, SMP_EXPANDER_A, "pass10", {
            0: _smp_frame(0, (0, 0, 0, 0)),
            1: _smp_frame(1, (9, 1, 2, 0)),
            2: _smp_frame(2, result=0x10)[:4],
            3: _smp_frame(3, (0, 0, 0, 0)),
        })})
        fabric = self.fabric(fabric_outputs)
        node = self.expander(fabric, SMP_EXPANDER_A)
        counters = node.raw["phy_error_counters"]
        self.assertEqual(counters["state"], "available")
        self.assertIsNone(counters["message"])
        self.assertEqual(counters["device"], "pass10")
        self.assertEqual((counters["measured_phys"], counters["unknown_phys"], counters["phys_with_errors"]), (3, 1, 1))
        self.assertEqual(node.metrics["phy_error_counters"], "3 PHYs read, 1 with errors, 1 unknown")
        self.assertIn("camcontrol smpcmd REPORT PHY ERROR LOG", node.evidence)
        phys = {row["phy"]: row for row in node.raw["phys"]}
        self.assertEqual(phys["1"]["invalid_dword_count"], 9)
        self.assertEqual(phys["1"]["counter_state"], "measured")
        self.assertIsNone(phys["2"]["invalid_dword_count"])
        self.assertEqual(phys["2"]["counter_reason"], "function_result")
        payload = next(item for item in fabric.expanders if item.get("sas_address") == SMP_EXPANDER_A)
        self.assertEqual(payload["phy_error_counters"]["state"], "available")
        # The second expander has a route but was not read in this refresh.
        other = self.expander(self.fabric(fabric_outputs, selected=SMP_EXPANDER_B), SMP_EXPANDER_B)
        self.assertEqual(other.raw["phy_error_counters"]["state"], "not_read")

    def test_fabric_marks_phys_after_a_stopped_batch_unknown(self):
        key_a = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-03 ; ..."
        fabric = self.fabric(self.outputs(**{key_a: _smp_output(0, SMP_EXPANDER_A, "pass10", {
            0: _smp_frame(0, (0, 0, 0, 0)),
            1: None,
        })}))
        node = self.expander(fabric, SMP_EXPANDER_A)
        counters = node.raw["phy_error_counters"]
        self.assertEqual(counters["state"], "available")
        self.assertEqual((counters["measured_phys"], counters["unknown_phys"]), (1, 3))
        phys = {row["phy"]: row for row in node.raw["phys"]}
        self.assertEqual(phys["1"]["counter_reason"], "no_response")
        for phy in ("2", "3"):
            self.assertEqual((phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "not_read"))
            self.assertIsNone(phys[phy]["invalid_dword_count"])
        # The read stopped at the silent PHY, and says so.
        self.assertEqual((counters["reason"], counters["stopped_at_phy"]), ("no_response", 1))
        self.assertEqual(
            node.metrics["phy_error_counters"], "1 PHYs read, 0 with errors, 3 unknown, read stopped at PHY 1 (no answer)",
        )

    def test_fabric_reports_refused_permission_without_zeros(self):
        message = "Expander counters not available (permission not granted)."
        refused_output = {
            f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-00 ; ...":
                _smp_output(0, SMP_EXPANDER_A, "pass10", {0: _SUDO_REFUSAL}),
        }
        for outputs, capability in ((self.outputs(**refused_output), None), (self.outputs(), "permission_denied")):
            for address in ((SMP_EXPANDER_A,) if capability is None else (SMP_EXPANDER_A, SMP_EXPANDER_B)):
                with self.subTest(capability=capability, address=address):
                    node = self.expander(self.fabric(outputs, capability, selected=address), address)
                    self.assertEqual(node.raw["phy_error_counters"]["state"], "permission_denied")
                    self.assertEqual(node.raw["phy_error_counters"]["message"], message)
                    self.assertEqual(node.metrics["phy_error_counters"], message)
                    for row in node.raw["phys"]:
                        self.assertIsNone(row["invalid_dword_count"])
                        self.assertEqual(row["counter_state"], "permission_denied")

    def test_fabric_names_a_missing_route(self):
        fabric = self.fabric(self.outputs(**{"camcontrol devlist -v": ""}))
        node = self.expander(fabric, SMP_EXPANDER_A)
        self.assertEqual(node.raw["phy_error_counters"]["state"], "no_route")
        self.assertEqual(
            node.metrics["phy_error_counters"],
            "Expander counters not available (no CAM pass device reaches this expander).",
        )

    def service(self, temp_dir):
        system = SystemConfig(id="core-smp", label="CORE", truenas=TrueNASConfig(platform="core"))
        return InventoryService(
            Settings(), system, AsyncMock(), AsyncMock(), None,
            MappingStore(str(Path(temp_dir) / "slot_mappings.json")),
            ProfileRegistry(Settings()),
            SlotDetailStore(str(Path(temp_dir) / "slot_detail_cache.json")),
        )

    @staticmethod
    def results(*extra):
        base = [
            SSHCommandResult(command="sudo -n /usr/sbin/mprutil -u 0 show expanders", ok=True, stdout=SMP_EXPANDERS),
            SSHCommandResult(command="sudo -n /usr/sbin/mprutil -u 0 show devices", ok=True, stdout=SMP_DEVICES),
            SSHCommandResult(command="sudo -n /sbin/camcontrol devlist -v", ok=True, stdout=SMP_DEVLIST),
        ]
        return [*base, *extra]

    def test_collector_probes_once_then_reads_every_phy_when_granted(self):
        from app.services.core_expander_counters import SMP_COUNTER_READ_INTERVAL_SECONDS

        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            probe = service._core_expander_counter_probe_commands(self.results())
            self.assertEqual(len(probe), 1)
            self.assertIn("for p in 00;", probe[0])
            self.assertIn("smpcmd pass10 ", probe[0])
            answered = SSHCommandResult(command=probe[0], ok=True, stdout=_smp_output(
                0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0)}))
            full = service._core_expander_counter_probe_commands(self.results(answered))
            self.assertEqual(len(full), 2)
            self.assertIn("for p in 00 01 02 03;", full[0])
            self.assertIn("for p in 00 01;", full[1])
            self.assertEqual(service._smp_counter_capability, "granted")
            # Inside the read interval the next refresh reads nothing; after
            # it, the refresh goes straight to the full read.
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])
            now[0] += SMP_COUNTER_READ_INTERVAL_SECONDS
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), full)

    def test_collector_remembers_a_refusal_and_asks_again_only_after_the_interval(self):
        from app.services.core_expander_counters import SMP_CAPABILITY_RETRY_SECONDS

        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            probe = service._core_expander_counter_probe_commands(self.results())
            refused = SSHCommandResult(command=probe[0], ok=True, stdout=_smp_output(
                0, SMP_EXPANDER_A, "pass10", {0: _SUDO_REFUSAL}))
            self.assertEqual(service._core_expander_counter_probe_commands(self.results(refused)), [])
            self.assertEqual(service._smp_counter_capability, "permission_denied")
            now[0] += SMP_CAPABILITY_RETRY_SECONDS - 1
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])
            now[0] += 2
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), probe)
            # An answer-less probe proves nothing and is not retried in the same refresh.
            silent = SSHCommandResult(command=probe[0], ok=True, stdout=_smp_output(
                0, SMP_EXPANDER_A, "pass10", {0: None}))
            self.assertEqual(service._core_expander_counter_probe_commands(self.results(silent)), [])

    def test_collector_does_nothing_off_core_or_without_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            self.assertEqual(service._core_expander_counter_probe_commands([]), [])
            service.system = service.system.model_copy(
                update={"truenas": TrueNASConfig(platform="scale")})
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])

    @unittest.skipUnless(
        os.name == "posix" and Path("/usr/bin/timeout").exists() and Path("/usr/bin/od").exists(),
        "needs a POSIX sh with /usr/bin/timeout and /usr/bin/od",
    )
    def test_built_command_runs_in_sh_with_the_exact_granted_argv(self):
        """Run the real command line under sh with a fake sudo that records argv."""
        from app.services.core_expander_counters import (
            SMP_PHY_ERROR_LOG_SUDO_GRANT, SmpTarget, build_phy_error_log_command,
            parse_phy_error_log_output, phy_error_log_argv,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            log = Path(temp_dir) / "argv.log"
            fake = bin_dir / "sudo"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                "args = sys.argv[1:]\n"
                f"open({str(log)!r}, 'a').write(repr(args) + '\\n')\n"
                "mode = os.environ['FAKE_SMP_MODE']\n"
                "if mode == 'deny':\n"
                "    sys.stderr.write('Sorry, user x is not allowed to execute this as root on host.\\n'); sys.exit(1)\n"
                "phy = int(args[6].split('{}')[9], 16)\n"
                "if mode == 'hang' and phy == 1:\n"
                "    time.sleep(10)\n"
                "frame = bytes((0x41, 0x11, 0, 6, 0, 1, 0, 0, 0, phy, 0, 0)) + (phy + 1).to_bytes(4, 'big') * 4\n"
                "sys.stdout.buffer.write(frame)\n"
            )
            fake.chmod(0o755)
            target = SmpTarget(0, SMP_EXPANDER_A, "pass10", 3)
            command = build_phy_error_log_command(target)

            def run(mode):
                log.write_text("")
                env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_SMP_MODE": mode}
                completed = subprocess.run(
                    ["/bin/sh", "-c", command], capture_output=True, text=True, env=env, timeout=30, check=False,
                )
                calls = [eval(line) for line in log.read_text().splitlines()]  # noqa: S307 - our own repr lines
                return completed, calls

            completed, calls = run("ok")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, "")
            self.assertEqual(calls, [["-n", *phy_error_log_argv("pass10", phy)] for phy in range(3)])
            for argv in calls:
                self.assertIsNotNone(re.fullmatch(
                    SMP_PHY_ERROR_LOG_SUDO_GRANT.split(" ", 1)[1], " ".join(argv[2:])))
            result = parse_phy_error_log_output(completed.stdout)
            self.assertEqual([row["invalid_dword_count"] for row in result.rows], [1, 2, 3])
            self.assertEqual(result.capability, "granted")

            completed, calls = run("deny")
            result = parse_phy_error_log_output(completed.stdout)
            self.assertEqual(result.capability, "permission_denied")
            self.assertEqual(len(result.rows), 3)

            with patch("app.services.core_expander_counters.SMP_CALL_TIMEOUT_SECONDS", 1):
                command = build_phy_error_log_command(target)
            completed, calls = run("hang")
            result = parse_phy_error_log_output(completed.stdout)
            self.assertEqual([row["counter_state"] for row in result.rows], ["measured", "unknown"])
            self.assertEqual(result.rows[1]["counter_reason"], "no_response")
            self.assertEqual(len(calls), 2, "a call with no answer ends the batch")


def _nine_expander_outputs(count: int = 9, num_phys: int = 1) -> dict:
    """``count`` expanders of ``num_phys`` PHYs on mpr0, each with its own routed end device."""
    expander_lines = ["Num Phys SAS Address      DevHandle Parent EncHandle SAS Level"]
    device_lines = ["B____T    SAS Address      Handle  Parent    Device        Speed Enc  Slot  Wdt"]
    devlist_lines = ["scbus4 on mpr0 bus 0:"]
    for index in range(count):
        handle = 0x20 + index * 2
        address = f"5000c5000000{0xa0 + index:04x}"
        expander_lines.append(f"{num_phys:<8} {address} {handle:04x}      0001   {0x40 + index:04x}      1")
        expander_lines.append(f"  0 1 {handle + 1:04x} 12.0 1.5 12.0 End Device")
        device_lines.append(
            f"00   {index:<4} 5000c5000000{0xb0 + index:04x} {handle + 1:04x}    {handle:04x}      SAS Target    12    "
            f"{0x40 + index:04x} 0     1"
        )
        devlist_lines.append(f"<SYNTHETIC DISK {index:04d}>  at scbus4 target {index} lun 0 (pass{index},da{index})")
    return {
        "mprutil -u 0 show expanders": "\n".join(expander_lines),
        "mprutil -u 0 show devices": "\n".join(device_lines),
        "camcontrol devlist -v": "\n".join(devlist_lines),
    }


class CoreExpanderCounterRemediationTests(unittest.TestCase):
    """Sol's #1088 review: parent contradictions, the read budget, probe order, refusal retry."""

    outputs = CoreExpanderCounterTests.outputs
    snapshot = CoreExpanderCounterTests.snapshot
    fabric = CoreExpanderCounterTests.fabric
    expander = CoreExpanderCounterTests.expander
    service = CoreExpanderCounterTests.service
    results = staticmethod(CoreExpanderCounterTests.results)

    # Target 10 now names expander B (0019) as its parent while A's PHY table
    # still lists its handle 000a; target 12 (A's only clean route) is gone.
    CONTRADICTORY_DEVICES = SMP_DEVICES.replace(
        "000a    0009", "000a    0019",
    ).replace(
        "00   12   5000c50000000011 000b    0009      SAS Target    12    0002 1     1\n", "",
    )

    def contradictory_results(self, *extra):
        return [
            SSHCommandResult(command="sudo -n /usr/sbin/mprutil -u 0 show expanders", ok=True, stdout=SMP_EXPANDERS),
            SSHCommandResult(
                command="sudo -n /usr/sbin/mprutil -u 0 show devices", ok=True, stdout=self.CONTRADICTORY_DEVICES,
            ),
            SSHCommandResult(command="sudo -n /sbin/camcontrol devlist -v", ok=True, stdout=SMP_DEVLIST),
            *extra,
        ]

    def test_contradictory_parent_never_routes_one_expanders_counters_to_another(self):
        from app.services.core_expander_counters import SmpTarget, discover_smp_targets

        self.assertNotIn("000b", self.CONTRADICTORY_DEVICES)
        outputs = self.outputs(**{"mprutil -u 0 show devices": self.CONTRADICTORY_DEVICES})
        # A has no consistent route left; B keeps its own clean target 30.
        self.assertEqual(discover_smp_targets(outputs), [SmpTarget(0, SMP_EXPANDER_B, "pass30", 2)])

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            commands = service._core_expander_counter_probe_commands(self.contradictory_results())
        self.assertEqual(len(commands), 1)
        self.assertIn(f"tnj-smp-phy-error-log:0:{SMP_EXPANDER_B}:pass30:", commands[0])
        self.assertFalse(any(SMP_EXPANDER_A in command or "pass10" in command for command in commands))

        # Even an A-marked read through pass10 (an older plan, or a forged
        # line) must not publish B-reachable counters on A.
        forged = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-00 ; ..."
        fabric_outputs = {
            **outputs,
            forged: _smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0, (71, 0, 0, 0))}),
        }
        node = self.expander(self.fabric(fabric_outputs), SMP_EXPANDER_A)
        counters = node.raw["phy_error_counters"]
        self.assertEqual(counters["state"], "no_route")
        self.assertIsNone(counters["device"])
        self.assertEqual(counters["rows"], [])
        self.assertNotIn(71, [row.get("invalid_dword_count") for row in node.raw["phys"]])
        self.assertFalse(any(row.get("counter_state") == "measured" for row in node.raw["phys"]))

        # The reverse disagreement: the parent names A but B's PHY table lists
        # the same handle. That device is no route to either expander.
        reverse = SMP_EXPANDERS.replace("  0 1 001a", "  0 1 000a")
        self.assertEqual(discover_smp_targets(self.outputs(**{"mprutil -u 0 show expanders": reverse})), [
            SmpTarget(0, SMP_EXPANDER_A, "pass12", 4),
            SmpTarget(0, SMP_EXPANDER_B, "pass30", 2),
        ])

    def test_read_budget_reports_not_read_and_rotates_instead_of_no_route(self):
        from app.services.core_expander_counters import (
            NO_ROUTE_MESSAGE, SMP_COUNTER_READ_INTERVAL_SECONDS, SMP_MAX_TARGETS_PER_REFRESH,
            discover_smp_targets,
        )

        outputs = _nine_expander_outputs()
        targets = discover_smp_targets(outputs)
        self.assertEqual(len(targets), 9, "route discovery is complete; the budget applies to reads")
        self.assertEqual(targets[8].device, "pass8")

        results = [
            SSHCommandResult(
                command=f"sudo -n /usr/sbin/{key}" if key.startswith("mprutil") else f"sudo -n /sbin/{key}",
                ok=True, stdout=stdout,
            )
            for key, stdout in outputs.items()
        ]
        read_addresses: list[list[str]] = []
        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            service._smp_counter_clock = lambda: now[0]
            for refresh in range(3):
                commands = service._core_expander_counter_probe_commands(list(results))
                # The second refresh, inside the read interval, reads only the
                # expander still waiting; the third, after it, starts a new turn.
                self.assertEqual(len(commands), 1 if refresh == 1 else SMP_MAX_TARGETS_PER_REFRESH)
                window = [canonicalize_ssh_command(command).split(":")[1] for command in commands]
                read_addresses.append(window)
                # Later planning rounds of the same refresh keep the window.
                answered = [SSHCommandResult(command=command, ok=True, stdout="") for command in commands]
                self.assertEqual(
                    service._core_expander_counter_probe_commands([*results, *answered]), [],
                )
                now[0] += 1 if refresh == 0 else SMP_COUNTER_READ_INTERVAL_SECONDS
        all_addresses = [target.expander_sas_address for target in targets]
        self.assertEqual(read_addresses[0], all_addresses[:8])
        self.assertEqual(read_addresses[1], [all_addresses[8]], "the ninth expander is read on the next refresh")
        self.assertEqual(set(read_addresses[0]) | set(read_addresses[1]), set(all_addresses))
        # The least recently read go first: the ninth waits for the next turn.
        self.assertEqual(read_addresses[2], all_addresses[:8])

        # The fabric: eight read this refresh, the ninth has a route but waits its turn.
        fabric_outputs = dict(outputs)
        for target in targets[:8]:
            key = f"echo tnj-smp-phy-error-log:{target.identity}:00-00 ; ..."
            fabric_outputs[key] = _smp_output(0, target.expander_sas_address, target.device, {0: _smp_frame(0)})
        ninth = targets[8].expander_sas_address
        node = self.expander(self.fabric(fabric_outputs, "granted", selected=ninth), ninth)
        counters = node.raw["phy_error_counters"]
        self.assertEqual(counters["state"], "not_read")
        self.assertEqual(counters["reason"], "read_budget")
        self.assertEqual(counters["device"], "pass8")
        self.assertNotEqual(counters["message"], NO_ROUTE_MESSAGE)
        self.assertIn("read budget", counters["message"])
        first = self.expander(self.fabric(fabric_outputs, "granted", selected=targets[0].expander_sas_address),
                              targets[0].expander_sas_address)
        self.assertEqual(first.raw["phy_error_counters"]["state"], "available")

    def test_full_read_measurement_wins_over_the_probe_for_the_same_phy(self):
        probe_key = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-00 ; ..."
        full_key = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-03 ; ..."
        fabric = self.fabric(self.outputs(**{
            probe_key: _smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0, (1, 0, 0, 0))}),
            full_key: _smp_output(0, SMP_EXPANDER_A, "pass10", {
                phy: _smp_frame(phy, (2 if phy == 0 else 0, 0, 0, 0)) for phy in range(4)
            }),
        }))
        node = self.expander(fabric, SMP_EXPANDER_A)
        phys = {row["phy"]: row for row in node.raw["phys"]}
        self.assertEqual(phys["0"]["invalid_dword_count"], 2)
        # A measured probe still beats an unknown full-read row.
        fabric = self.fabric(self.outputs(**{
            probe_key: _smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0, (1, 0, 0, 0))}),
            full_key: _smp_output(0, SMP_EXPANDER_A, "pass10", {0: "camcontrol: error sending command\n"}),
        }))
        phys = {row["phy"]: row for row in self.expander(fabric, SMP_EXPANDER_A).raw["phys"]}
        self.assertEqual((phys["0"]["counter_state"], phys["0"]["invalid_dword_count"]), ("measured", 1))

    def test_a_forced_refresh_asks_sudo_again_after_a_refusal(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            probe = service._core_expander_counter_probe_commands(self.results())
            refused = SSHCommandResult(command=probe[0], ok=True, stdout=_smp_output(
                0, SMP_EXPANDER_A, "pass10", {0: _SUDO_REFUSAL}))
            self.assertEqual(service._core_expander_counter_probe_commands(self.results(refused)), [])
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])

            async def snapshot(force):
                with patch.object(service, "_resolve_snapshot_cache_key", side_effect=RuntimeError("stop")):
                    with self.assertRaises(RuntimeError):
                        await service._get_snapshot_result(force_refresh=force)

            # An ordinary or background refresh keeps the remembered refusal.
            asyncio.run(snapshot(False))
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])
            # A user's forced refresh (the Refresh button, ?force=true) retries once.
            asyncio.run(snapshot(True))
            self.assertEqual(service._smp_counter_capability, "permission_denied")
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), probe)
            self.assertEqual(service._core_expander_counter_probe_commands(self.results(refused)), [])
            self.assertEqual(service._core_expander_counter_probe_commands(self.results()), [])

    def test_logged_command_masks_the_expander_address_and_timeout_has_a_kill_after(self):
        from app.services.core_expander_counters import SmpTarget, build_phy_error_log_command
        from app.services.ssh_probe import redact_ssh_command

        command = build_phy_error_log_command(SmpTarget(0, SMP_EXPANDER_A, "pass10", 4))
        self.assertIn("/usr/bin/timeout -k 1 2 sudo -n /sbin/camcontrol smpcmd pass10 ", command)
        redacted = redact_ssh_command(command)
        self.assertNotIn(SMP_EXPANDER_A, redacted)
        self.assertIn("tnj-smp-phy-error-log:0:5000...0001:pass10:00-03", redacted)
        self.assertNotIn(SMP_EXPANDER_A, redact_ssh_command(f"sudo -n true ; {command}"))

    def test_route_discovery_work_is_linear_in_rows_and_ambiguous_rows_fail_closed(self):
        """Codex security review: index rows instead of scanning devices per expander."""
        import app.services.core_expander_counters as counters

        size = 300
        outputs = _nine_expander_outputs(size)
        calls = [0]
        real_hex_int = counters._hex_int

        def counting(value):
            calls[0] += 1
            return real_hex_int(value)

        with patch.object(counters, "_hex_int", counting):
            targets = counters.discover_smp_targets(outputs)
        self.assertEqual(len(targets), size)
        # One pass over expanders, their PHY rows and devices; not size * size.
        self.assertLess(calls[0], 12 * size, calls[0])

        # Two expander rows with one handle, or two device rows with one
        # handle, cannot name one route.
        dup_expander = SMP_EXPANDERS.replace("0019      0001", "0009      0001")
        self.assertEqual(counters.discover_smp_targets(self.outputs(**{
            "mprutil -u 0 show expanders": dup_expander,
        })), [])
        dup_device = SMP_DEVICES.replace("5000c50000000020 001a", "5000c50000000020 000a")
        self.assertEqual(
            [(t.expander_sas_address, t.device) for t in counters.discover_smp_targets(self.outputs(**{
                "mprutil -u 0 show devices": dup_device,
            }))],
            [(SMP_EXPANDER_A, "pass12")],
        )


class CoreExpanderCounterRateLimitTests(unittest.TestCase):
    """Codex security review on #1088: forced refreshes must not hammer the appliance.

    A forced refresh may ask sudo again with one probe call, but full counter
    reads are rate limited per expander whatever asked for the refresh, and
    every refresh stays under a hard ceiling of privileged calls.
    """

    outputs = CoreExpanderCounterTests.outputs
    snapshot = CoreExpanderCounterTests.snapshot
    expander = CoreExpanderCounterTests.expander
    service = CoreExpanderCounterTests.service
    results = staticmethod(CoreExpanderCounterTests.results)
    READ_AT = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    def fabric(self, outputs, capability=None, selected=SMP_EXPANDER_A, reads=None):
        return build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
            snapshot=self.snapshot(selected),
            ssh_outputs=outputs,
            expander_counter_capability=capability,
            expander_counter_reads=reads,
        )

    @staticmethod
    def base_results(outputs):
        return [
            SSHCommandResult(
                command=f"sudo -n /usr/sbin/{key}" if key.startswith("mprutil") else f"sudo -n /sbin/{key}",
                ok=True, stdout=stdout,
            )
            for key, stdout in outputs.items()
        ]

    @staticmethod
    def phys_in(command):
        return [int(word, 16) for word in re.search(r"for p in ([0-9a-f ]+);", command).group(1).split()]

    def answer(self, command, refuse):
        unit, address, device, _span = canonicalize_ssh_command(command)[len("smp phy error log "):].split(":")
        per_phy = {phy: _SUDO_REFUSAL if refuse else _smp_frame(phy) for phy in self.phys_in(command)}
        return SSHCommandResult(command=command, ok=True, stdout=_smp_output(int(unit), address, device, per_phy))

    def refresh(self, service, base, *, forced=True, refuse=False):
        """One collection, planned round by round as run_planned_commands does."""
        if forced:
            # What the Refresh button and ?force=true call (see the forced refresh test above).
            service.retry_expander_counter_capability()
        results = list(base)
        commands: list[str] = []
        # Chunks follow one planning round at a time: up to 64 rounds per 255-PHY expander.
        for _round in range(100):
            planned = [command for command in service._core_expander_counter_probe_commands(list(results))
                       if command not in commands]
            if not planned:
                break
            commands.extend(planned)
            results.extend(self.answer(command, refuse) for command in planned)
        else:
            self.fail("the planner never settled")
        service._record_smp_counter_reads(results)
        return commands, results

    def calls(self, commands):
        return sum(len(self.phys_in(command)) for command in commands)

    def test_forced_refreshes_in_a_loop_stay_under_the_call_ceiling_and_the_read_interval(self):
        import app.services.core_expander_counters as counters

        outputs = _nine_expander_outputs(9, num_phys=255)
        targets = counters.discover_smp_targets(outputs)
        self.assertEqual([target.num_phys for target in targets], [255] * 9)
        base = self.base_results(outputs)
        now = [1000.0]
        reads: dict[str, list[int]] = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            for refresh in range(8):
                commands, _results = self.refresh(service, base)
                self.assertLessEqual(self.calls(commands), counters.SMP_MAX_CALLS_PER_REFRESH, refresh)
                # Full reads come in chunks of several PHYs; count expanders, not chunks.
                read_now = {
                    canonicalize_ssh_command(command).split(":")[1]
                    for command in commands if len(self.phys_in(command)) > 1
                }
                self.assertLessEqual(len(read_now), counters.SMP_MAX_TARGETS_PER_REFRESH)
                for address in read_now:
                    reads.setdefault(address, []).append(refresh)
                now[0] += 1
            # Inside the interval each expander is read in full exactly once,
            # in turn, and then forced refreshes start no read at all.
            self.assertEqual(sorted(reads), sorted(target.expander_sas_address for target in targets))
            self.assertTrue(all(len(refreshes) == 1 for refreshes in reads.values()), reads)
            self.assertEqual(self.refresh(service, base)[0], [])
            first = sorted(address for address, refreshes in reads.items() if refreshes == [0])
            self.assertTrue(first)
            # After the interval the least recently read expanders go first.
            now[0] = 1000.0 + counters.SMP_COUNTER_READ_INTERVAL_SECONDS
            commands, _results = self.refresh(service, base)
            self.assertLessEqual(self.calls(commands), counters.SMP_MAX_CALLS_PER_REFRESH)
            self.assertEqual(sorted({canonicalize_ssh_command(command).split(":")[1] for command in commands}), first)

    def test_a_forced_refresh_after_a_refusal_makes_exactly_one_probe_call(self):
        import app.services.core_expander_counters as counters

        base = self.results()
        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            probe, _results = self.refresh(service, base, forced=False, refuse=True)
            self.assertEqual(self.calls(probe), 1)
            self.assertEqual(service._smp_counter_capability, "permission_denied")
            for _refresh in range(3):
                now[0] += 1
                self.assertEqual(self.refresh(service, base, forced=False, refuse=True)[0], [])
                self.assertEqual(self.refresh(service, base, refuse=True)[0], probe, "one probe call, no reads")
            # The operator adds the grant and presses Refresh: one probe, then
            # the full reads, all under the ceiling.
            commands, _results = self.refresh(service, base)
            self.assertEqual(commands[0], probe[0])
            self.assertEqual([len(self.phys_in(command)) for command in commands], [1, 4, 2])
            self.assertLessEqual(self.calls(commands), counters.SMP_MAX_CALLS_PER_REFRESH)
            self.assertEqual(service._smp_counter_capability, "granted")
            # Pressing Refresh again inside the interval asks for nothing.
            for _refresh in range(5):
                now[0] += 60
                self.assertEqual(self.refresh(service, base)[0], [])

    def test_counters_reused_inside_the_interval_show_their_read_time(self):
        base = self.results()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_wall_clock = lambda: self.READ_AT
            commands, _results = self.refresh(service, base)
            self.assertEqual(len(commands), 3)
            # A forced refresh inside the interval reads nothing new.
            commands, results = self.refresh(service, base)
            self.assertEqual(commands, [])
            reads = service.expander_counter_reads()
        outputs = {item.command: item.stdout for item in results}
        for address in (SMP_EXPANDER_A, SMP_EXPANDER_B):
            with self.subTest(address=address):
                node = self.expander(self.fabric(outputs, "granted", selected=address, reads=reads), address)
                counters = node.raw["phy_error_counters"]
                self.assertEqual(counters["state"], "available")
                self.assertEqual(counters["read_at"], "2026-11-07T12:00:00Z")
                self.assertTrue(node.metrics["phy_error_counters"].endswith(", read at 2026-11-07T12:00:00Z"))
                self.assertTrue(all(row["counter_state"] == "measured" for row in node.raw["phys"]))
        # Kept readings are not shown once sudo refuses the read.
        node = self.expander(self.fabric(outputs, "permission_denied", reads=reads), SMP_EXPANDER_A)
        self.assertEqual(node.raw["phy_error_counters"]["state"], "permission_denied")
        self.assertIsNone(node.raw["phy_error_counters"]["read_at"])
        # A reading in this refresh's own outputs with no kept read time has none.
        key_a = f"echo tnj-smp-phy-error-log:0:{SMP_EXPANDER_A}:pass10:00-03 ; ..."
        fresh = self.outputs(**{key_a: _smp_output(0, SMP_EXPANDER_A, "pass10", {0: _smp_frame(0)})})
        node = self.expander(self.fabric(fresh, "granted"), SMP_EXPANDER_A)
        self.assertIsNone(node.raw["phy_error_counters"]["read_at"])
        self.assertNotIn("read at", node.metrics["phy_error_counters"])

    def test_rotation_stays_fair_when_refreshes_are_further_apart_than_the_interval(self):
        """Codex review on be4ecccc: expired read times must still order the rotation."""
        import app.services.core_expander_counters as counters

        outputs = _nine_expander_outputs(9, num_phys=4)
        addresses = [target.expander_sas_address for target in counters.discover_smp_targets(outputs)]
        base = self.base_results(outputs)
        now = [1000.0]
        rounds: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_clock = lambda: now[0]
            service._smp_counter_capability = "granted"
            for _refresh in range(3):
                commands, _results = self.refresh(service, base, forced=False)
                rounds.append([canonicalize_ssh_command(command).split(":")[1] for command in commands])
                # For example source_bundle_cache_ttl_seconds=1800.
                now[0] += 2 * counters.SMP_COUNTER_READ_INTERVAL_SECONDS
            # A departed expander's read time is dropped; the rest are kept.
            service._core_expander_counter_probe_commands(self.base_results(_nine_expander_outputs(8, num_phys=4)))
            self.assertEqual(set(service._smp_counter_last_read), {(0, address) for address in addresses[:8]})
        self.assertEqual(rounds[0], addresses[:8])
        self.assertIn(addresses[8], rounds[1], "the never-read expander goes first on the next refresh")
        # Commands keep discovery order; the least recently read are chosen.
        self.assertEqual(rounds[1], [*addresses[:7], addresses[8]])
        self.assertEqual(rounds[2], addresses[:8], "the expander skipped last time is read next")


def _wide_expander_outputs(num_phys: int = 10) -> dict:
    """Expander A with ``num_phys`` PHY rows, routed through pass10."""
    expander_lines = [
        "Num Phys SAS Address      DevHandle Parent EncHandle SAS Level",
        f"{num_phys:<8} {SMP_EXPANDER_A} 0009      0001   0002      1",
        "  0 1 000a 12.0 1.5 12.0 End Device",
        *[f"  {phy}       1.5 12.0 No Device" for phy in range(1, num_phys)],
    ]
    return {
        "mprutil -u 0 show expanders": "\n".join(expander_lines),
        # Header and the target 10 row of SMP_DEVICES: one end device on PHY 0.
        "mprutil -u 0 show devices": "\n".join(
            line for line in SMP_DEVICES.splitlines() if line.startswith(("B____T", "00   10 "))
        ),
        "camcontrol devlist -v": SMP_DEVLIST,
    }


class CoreExpanderCounterChunkTests(unittest.TestCase):
    """Codex P2 on #1088: one expander's PHY reads must fit the SSH command deadline.

    Each expander is read in fixed-size chunks, one SSH command per chunk, so
    a chunk that runs past the SSH command timeout loses only its own PHYs.
    """

    snapshot = CoreExpanderCounterTests.snapshot
    expander = CoreExpanderCounterTests.expander
    service = CoreExpanderCounterTests.service
    fabric = CoreExpanderCounterRateLimitTests.fabric
    base_results = staticmethod(CoreExpanderCounterRateLimitTests.base_results)
    phys_in = staticmethod(CoreExpanderCounterRateLimitTests.phys_in)
    calls = CoreExpanderCounterRateLimitTests.calls
    READ_AT = CoreExpanderCounterRateLimitTests.READ_AT

    def refresh(self, service, base, slow_phy=None, offset=0):
        """One collection; the chunk holding ``slow_phy`` runs past the SSH timeout."""
        results = list(base)
        commands: list[str] = []
        # Chunks follow one planning round at a time: up to 64 rounds per 255-PHY expander.
        for _round in range(100):
            planned = [command for command in service._core_expander_counter_probe_commands(list(results))
                       if command not in commands]
            if not planned:
                break
            commands.extend(planned)
            for command in planned:
                phys = self.phys_in(command)
                if slow_phy in phys:
                    # What SSHProbe returns when the command deadline expires.
                    results.append(SSHCommandResult(command=command, ok=False, stderr="Command timed out"))
                    continue
                unit, address, device, _span = canonicalize_ssh_command(command)[len("smp phy error log "):].split(":")
                per_phy = {phy: _smp_frame(phy, (offset + phy + 1, 0, 0, 0)) for phy in phys}
                results.append(SSHCommandResult(
                    command=command, ok=True, stdout=_smp_output(int(unit), address, device, per_phy),
                ))
        else:
            self.fail("the planner never settled")
        service._record_smp_counter_reads(results)
        return commands, results

    def phy_rows(self, service, results):
        outputs = {item.command: item.stdout for item in results if item.ok}
        node = self.expander(
            self.fabric(outputs, "granted", reads=service.expander_counter_reads()), SMP_EXPANDER_A,
        )
        return node, {int(row["phy"]): row for row in node.raw["phys"]}

    def test_chunks_fit_the_ssh_command_deadline(self):
        import app.services.core_expander_counters as counters
        from app.config import SSHConfig

        per_call = counters.SMP_CALL_TIMEOUT_SECONDS + counters.SMP_CALL_KILL_AFTER_SECONDS
        # Worst case of one chunk, every call killed: clearly under the default deadline.
        self.assertLessEqual(counters.SMP_PHYS_PER_COMMAND * per_call + 3, SSHConfig().timeout_seconds)
        target = counters.SmpTarget(0, SMP_EXPANDER_A, "pass10", 10)
        commands = counters.build_phy_error_log_commands(target)
        self.assertEqual([self.phys_in(command) for command in commands], [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]])
        self.assertEqual(
            [canonicalize_ssh_command(command) for command in commands],
            [f"smp phy error log 0:{SMP_EXPANDER_A}:pass10:{span}" for span in ("00-03", "04-07", "08-09")],
        )
        self.assertEqual(sum(counters.smp_output_calls(canonicalize_ssh_command(c)) for c in commands), 10)
        wide = counters.SmpTarget(0, SMP_EXPANDER_A, "pass10", 255)
        self.assertEqual(len(counters.build_phy_error_log_commands(wide)), 64)
        # The hang stop still works inside a chunk: one silent call ends that chunk.
        self.assertIn("then echo @no-response; break; fi", commands[0])

    def test_a_slow_chunk_ends_the_read_and_keeps_the_phys_before_it(self):
        # A chunk past the SSH command deadline means the expander is not
        # answering (each call is killed after 3 s, a chunk takes at most 12 s),
        # so the read stops there for this refresh (tnj-941f); the chunks
        # already read keep their counters.
        base = self.base_results(_wide_expander_outputs(10))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            service._smp_counter_wall_clock = lambda: self.READ_AT
            commands, results = self.refresh(service, base, slow_phy=5)
            self.assertEqual(len(commands), 2, "one SSH command per chunk, none after the slow one")
            self.assertEqual(self.calls(commands), 8, "calls, not chunks, count against the ceiling")
            node, phys = self.phy_rows(service, results)
        counters = node.raw["phy_error_counters"]
        self.assertEqual(counters["state"], "available")
        self.assertEqual((counters["reason"], counters["stopped_at_phy"]), ("no_response", 4))
        self.assertEqual((counters["measured_phys"], counters["unknown_phys"]), (4, 6))
        for phy in (0, 1, 2, 3):
            with self.subTest(phy=phy):
                self.assertEqual(phys[phy]["counter_state"], "measured")
                self.assertEqual(phys[phy]["invalid_dword_count"], phy + 1)
        for phy in range(4, 10):
            with self.subTest(phy=phy):
                self.assertEqual((phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "not_read"))
                self.assertIsNone(phys[phy]["invalid_dword_count"])
        self.assertEqual(counters["read_at"], "2026-11-07T12:00:00Z")

    def test_a_reread_replaces_kept_chunks_and_never_mixes_in_an_older_failed_chunk(self):
        from app.services.core_expander_counters import SMP_COUNTER_READ_INTERVAL_SECONDS

        base = self.base_results(_wide_expander_outputs(10))
        now = [1000.0]
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            service._smp_counter_clock = lambda: now[0]
            wall = [self.READ_AT]
            service._smp_counter_wall_clock = lambda: wall[0]
            self.refresh(service, base)
            # Inside the interval nothing is read; the kept chunks are shown.
            commands, results = self.refresh(service, base, offset=100)
            self.assertEqual(commands, [])
            _node, phys = self.phy_rows(service, results)
            self.assertEqual([phys[phy]["invalid_dword_count"] for phy in range(10)], list(range(1, 11)))
            # The next read's last chunk runs past the deadline: its PHYs are
            # unknown, not the older reading, and the other chunks are new.
            now[0] += SMP_COUNTER_READ_INTERVAL_SECONDS
            commands, results = self.refresh(service, base, slow_phy=9, offset=100)
            self.assertEqual(len(commands), 3)
            node, phys = self.phy_rows(service, results)
            self.assertEqual([phys[phy]["invalid_dword_count"] for phy in range(8)], list(range(101, 109)))
            for phy in (8, 9):
                self.assertEqual((phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "not_read"))
            self.assertEqual(node.raw["phy_error_counters"]["measured_phys"], 8)
            # A read whose first chunk fails ends there and keeps the previous
            # reading, which says that a later read stopped.
            now[0] += SMP_COUNTER_READ_INTERVAL_SECONDS
            wall[0] = datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc)
            commands, results = self.refresh(service, base, slow_phy=0, offset=200)
            self.assertEqual(len(commands), 1)
            node, phys = self.phy_rows(service, results)
            self.assertEqual([phys[phy]["invalid_dword_count"] for phy in range(8)], list(range(101, 109)))
            self.assertEqual(node.raw["phy_error_counters"]["reason"], "no_response")
            self.assertEqual(node.raw["phy_error_counters"]["read_at"], "2026-11-07T12:00:00Z")
            self.assertTrue(node.metrics["phy_error_counters"].endswith(
                "read at 2026-11-07T12:00:00Z, a later read stopped at PHY 0 (no answer) at 2026-11-07T12:30:00Z",
            ), node.metrics["phy_error_counters"])

    def test_ssh_round_trips_per_refresh_stay_bounded(self):
        import app.services.core_expander_counters as counters

        outputs = _nine_expander_outputs(9, num_phys=255)
        targets = counters.discover_smp_targets(outputs)
        base = self.base_results(outputs)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            commands, _results = self.refresh(service, base)
        self.assertLessEqual(self.calls(commands), counters.SMP_MAX_CALLS_PER_REFRESH)
        self.assertLessEqual(len(commands), counters.SMP_MAX_COMMANDS_PER_REFRESH)
        self.assertEqual(len(commands), 1 + 2 * 64, "the probe, then two 255-PHY expanders in 4-PHY chunks")
        # The command bound holds on its own, whatever the call budget allows.
        chosen = counters.select_counter_reads(targets, {}, 0.0, counters.SMP_MAX_CALLS_PER_REFRESH, command_budget=64)
        self.assertEqual(len(chosen), 1)
        self.assertLessEqual(
            1 + counters.SMP_MAX_TARGETS_PER_REFRESH
            + (counters.SMP_MAX_CALLS_PER_REFRESH - 1) // counters.SMP_PHYS_PER_COMMAND,
            counters.SMP_MAX_COMMANDS_PER_REFRESH,
        )


def _retained_leaves(value):
    """Every leaf value of a retained structure (mappings, sequences, dataclasses)."""
    import dataclasses

    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, field.name) for field in dataclasses.fields(item))
        else:
            yield item


def _chunk_identity(command: str) -> tuple[int, str, str, list[int]]:
    unit, address, device, span = canonicalize_ssh_command(command)[len("smp phy error log "):].split(":")
    first, last = (int(part, 16) for part in span.split("-"))
    return int(unit), address, device, list(range(first, last + 1))


class CoreExpanderCounterRetentionTests(unittest.TestCase):
    """Codex security P2 on #1088: bound retained counter output across refreshes.

    A configured CORE peer under hostile control may advertise many expanders
    and answer every chunk with as much output as SSHProbe lets through. The
    collector keeps only validated decoded rows, never raw output, with hard
    bounds per PHY, per expander and in total; oversized or malformed output
    is rejected and shown as an error, never accumulated.
    """

    snapshot = CoreExpanderCounterTests.snapshot
    expander = CoreExpanderCounterTests.expander
    service = CoreExpanderCounterTests.service
    fabric = CoreExpanderCounterRateLimitTests.fabric
    base_results = staticmethod(CoreExpanderCounterRateLimitTests.base_results)

    def refresh(self, service, base, answer):
        """One collection planned round by round, as SSHProbe.run_planned_commands does."""
        results = list(base)
        commands: list[str] = []
        for _round in range(300):
            planned = [command for command in service._core_expander_counter_probe_commands(list(results))
                       if command not in commands]
            if not planned:
                break
            commands.extend(planned)
            results.extend(answer(command) for command in planned)
        else:
            self.fail("the planner never settled")
        service._record_smp_counter_reads(results)
        return commands, results

    @staticmethod
    def valid(command):
        unit, address, device, phys = _chunk_identity(command)
        return SSHCommandResult(command=command, ok=True, stdout=_smp_output(
            unit, address, device, {phy: _smp_frame(phy, (phy + 1, 0, 0, 0)) for phy in phys},
        ))

    def test_a_hostile_peer_cannot_grow_retained_counter_data(self):
        import pickle

        import app.services.core_expander_counters as counters

        outputs = _nine_expander_outputs(100, num_phys=16)
        base = self.base_results(outputs)
        # Hex that parses as bytes but is far more than four PHYs can print.
        junk = "\n".join([" 41 11 00 06" * 16] * 320)

        def hostile(command):
            unit, address, device, phys = _chunk_identity(command)
            stdout = (
                f"tnj-smp-phy-error-log:{unit}:{address}:{device}:{phys[0]:02x}-{phys[-1]:02x}\n"
                f"@phy={phys[0]:02x}\n{junk}\n@end\n"
            )
            # The most SSHProbe lets through for one counter command, exit 0.
            self.assertLess(len(stdout), counters.SMP_MAX_OUTPUT_BYTES_PER_COMMAND)
            self.assertGreater(len(stdout), 60_000)
            return SSHCommandResult(command=command, ok=True, stdout=stdout)

        now = [1000.0]
        sizes: list[int] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            service._smp_counter_clock = lambda: now[0]
            for refresh in range(13):
                commands, results = self.refresh(service, base, hostile)
                self.assertTrue(commands, refresh)
                reads = service.expander_counter_reads()
                # Only decoded rows are kept: no raw output, no long strings.
                longest = max(
                    (len(leaf) for leaf in _retained_leaves(reads) if isinstance(leaf, (str, bytes))), default=0,
                )
                self.assertLessEqual(longest, 64, f"raw output kept after refresh {refresh}")
                sizes.append(len(pickle.dumps(reads)))
                # A rejected chunk ends that expander's read for this refresh.
                per_expander: dict[str, int] = {}
                for command in commands:
                    address = _chunk_identity(command)[1]
                    per_expander[address] = per_expander.get(address, 0) + 1
                self.assertEqual(set(per_expander.values()), {1}, refresh)
                now[0] += 1
            self.assertLessEqual(max(sizes), 256 * 1024, sizes)
            self.assertLessEqual(len(reads), counters.SMP_MAX_RETAINED_EXPANDERS)
            address = _chunk_identity(commands[0])[1]
            fresh = {item.command: item.stdout for item in results if item.ok}
            node = self.expander(self.fabric(fresh, "granted", selected=address, reads=reads), address)
        summary = node.raw["phy_error_counters"]
        self.assertEqual((summary["state"], summary["reason"]), ("unavailable", "rejected_output"))
        self.assertEqual(summary["measured_phys"], 0)
        self.assertEqual(summary["message"], counters.REJECTED_OUTPUT_MESSAGE)
        phy0 = next(row for row in node.raw["phys"] if str(row["phy"]) == "0")
        self.assertEqual((phy0["counter_state"], phy0["counter_reason"]), ("unknown", "rejected_output"))
        self.assertIsNone(phy0["invalid_dword_count"])

    def test_malformed_chunk_output_is_rejected_not_attached(self):
        import app.services.core_expander_counters as counters

        base = self.base_results(_wide_expander_outputs(12))
        cases = {
            # Rows for PHYs outside the chunk that was asked for.
            "other_phys": lambda unit, address, device, phys: _smp_output(
                unit, address, device, {phy + 8: _smp_frame(phy + 8, (77, 0, 0, 0)) for phy in phys},
            ).replace(f":{phys[0] + 8:02x}-{phys[-1] + 8:02x}", f":{phys[0]:02x}-{phys[-1]:02x}"),
            # A marker for another pass device.
            "other_device": lambda unit, address, device, phys: _smp_output(
                unit, address, "pass12", {phy: _smp_frame(phy, (77, 0, 0, 0)) for phy in phys},
            ),
            # Exit 0 without the end marker.
            "no_end": lambda unit, address, device, phys: _smp_output(
                unit, address, device, {phy: _smp_frame(phy, (77, 0, 0, 0)) for phy in phys}, end=False,
            ),
        }
        for name, build in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temp_dir:
                service = self.service(temp_dir)
                service._smp_counter_capability = "granted"

                def answer(command, build=build):
                    unit, address, device, phys = _chunk_identity(command)
                    if phys[0] == 4:
                        return SSHCommandResult(command=command, ok=True, stdout=build(unit, address, device, phys))
                    return self.valid(command)

                commands, results = self.refresh(service, base, answer)
                # The rejected chunk (PHYs 4-7) ends the read: 8-11 is never asked for.
                self.assertEqual([_chunk_identity(command)[3][0] for command in commands], [0, 4])
                fresh = {item.command: item.stdout for item in results if item.ok}
                node = self.expander(
                    self.fabric(fresh, "granted", reads=service.expander_counter_reads()), SMP_EXPANDER_A,
                )
                phys = {int(row["phy"]): row for row in node.raw["phys"]}
                self.assertEqual([phys[phy]["invalid_dword_count"] for phy in range(4)], [1, 2, 3, 4])
                for phy in range(4, 8):
                    self.assertEqual(
                        (phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "rejected_output"),
                    )
                for phy in range(8, 12):
                    self.assertEqual((phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "not_read"))
                    self.assertNotEqual(phys[phy]["invalid_dword_count"], 77)
                summary = node.raw["phy_error_counters"]
                self.assertEqual((summary["state"], summary["reason"]), ("available", "rejected_output"))
                self.assertEqual(summary["stopped_at_phy"], 4)
                self.assertIn("read stopped at PHY 4 (answer rejected)", node.metrics["phy_error_counters"])
                # The same output in this refresh's raw outputs alone is rejected too.
                raw_only = self.expander(self.fabric(fresh, "granted"), SMP_EXPANDER_A)
                raw_phys = {int(row["phy"]): row for row in raw_only.raw["phys"]}
                self.assertTrue(all(raw_phys[phy]["invalid_dword_count"] != 77 for phy in range(12)))
                self.assertEqual(raw_phys[4]["counter_reason"], "rejected_output")
                self.assertLessEqual(counters.SMP_MAX_RETAINED_EXPANDERS, 64)

    def test_retained_readings_have_hard_per_expander_and_total_bounds(self):
        import app.services.core_expander_counters as counters

        for count, num_phys, refreshes in ((100, 16, 13), (20, 255, 10)):
            outputs = _nine_expander_outputs(count, num_phys=num_phys)
            addresses = [target.expander_sas_address for target in counters.discover_smp_targets(outputs)]
            base = self.base_results(outputs)
            now = [1000.0]
            order: list[str] = []
            with self.subTest(count=count, num_phys=num_phys), tempfile.TemporaryDirectory() as temp_dir:
                service = self.service(temp_dir)
                service._smp_counter_capability = "granted"
                service._smp_counter_clock = lambda: now[0]
                for _refresh in range(refreshes):
                    commands, results = self.refresh(service, base, self.valid)
                    for command in commands:
                        address = _chunk_identity(command)[1]
                        if address not in order:
                            order.append(address)
                    reads = service.expander_counter_reads()
                    rows = [row for read in reads.values() for row in read.rows]
                    self.assertLessEqual(len(reads), counters.SMP_MAX_RETAINED_EXPANDERS)
                    self.assertLessEqual(len(rows), counters.SMP_MAX_RETAINED_PHYS)
                    for read in reads.values():
                        self.assertLessEqual(len(read.rows), 255)
                        self.assertEqual(len({row[0] for row in read.rows}), len(read.rows))
                    for leaf in _retained_leaves(reads):
                        self.assertTrue(
                            leaf is None or type(leaf) in (int, bool) or (isinstance(leaf, str) and len(leaf) <= 64),
                            repr(leaf)[:80],
                        )
                    now[0] += 1
                self.assertEqual(sorted(order), sorted(addresses), "every expander was read once")
                kept = {address for (_unit, address) in reads}
                # The least recently read are evicted first; the newest are kept.
                self.assertLess(len(kept), len(order))
                self.assertEqual(kept, set(order[-len(kept):]))
                evicted = order[0]
                fresh = {item.command: item.stdout for item in results if item.ok}
                node = self.expander(self.fabric(fresh, "granted", selected=evicted, reads=reads), evicted)
                self.assertEqual(node.raw["phy_error_counters"]["state"], "not_read")
                self.assertEqual(node.raw["phy_error_counters"]["measured_phys"], 0)
                newest = order[-1]
                node = self.expander(self.fabric(fresh, "granted", selected=newest, reads=reads), newest)
                self.assertEqual(node.raw["phy_error_counters"]["state"], "available")


class CoreExpanderCounterHungTests(unittest.TestCase):
    """A hung expander costs about one chunk timeout per read, not one per chunk.

    The first chunk that does not answer ends that expander's read for this
    refresh; the expander is marked as not answering and keeps the 15-minute
    read interval. Other expanders are read as usual.
    """

    snapshot = CoreExpanderCounterTests.snapshot
    expander = CoreExpanderCounterTests.expander
    service = CoreExpanderCounterTests.service
    fabric = CoreExpanderCounterRateLimitTests.fabric
    base_results = staticmethod(CoreExpanderCounterRateLimitTests.base_results)
    refresh = CoreExpanderCounterRetentionTests.refresh
    READ_AT = CoreExpanderCounterRateLimitTests.READ_AT

    def hung_from(self, hang_phy, spent, *, ssh_timeout=False):
        """Answers every PHY before ``hang_phy``; the call for it never answers.

        ``spent`` collects the simulated seconds of each chunk: a quick answer
        is 0.05 s, a call killed by timeout(1) is its timeout plus the kill
        grace, a command past the SSH deadline is the whole deadline.
        """
        import app.services.core_expander_counters as counters
        from app.config import SSHConfig

        def answer(command):
            unit, address, device, phys = _chunk_identity(command)
            if hang_phy in phys and ssh_timeout:
                spent.append(float(SSHConfig().timeout_seconds))
                return SSHCommandResult(command=command, ok=False, stderr="SSH command timed out.")
            per_phy: dict[int, object] = {}
            seconds = 0.0
            for phy in phys:
                if hang_phy is not None and phy >= hang_phy:
                    per_phy[phy] = None
                    seconds += counters.SMP_CALL_TIMEOUT_SECONDS + counters.SMP_CALL_KILL_AFTER_SECONDS
                    break
                per_phy[phy] = _smp_frame(phy, (phy + 1, 0, 0, 0))
                seconds += 0.05
            spent.append(seconds)
            return SSHCommandResult(command=command, ok=True, stdout=_smp_output(unit, address, device, per_phy))

        return answer

    def test_a_silent_expander_costs_one_chunk_timeout(self):
        import app.services.core_expander_counters as counters

        base = self.base_results(_wide_expander_outputs(40))
        for ssh_timeout in (False, True):
            with self.subTest(ssh_timeout=ssh_timeout), tempfile.TemporaryDirectory() as temp_dir:
                service = self.service(temp_dir)
                service._smp_counter_capability = "granted"
                service._smp_counter_wall_clock = lambda: self.READ_AT
                now = [1000.0]
                service._smp_counter_clock = lambda: now[0]
                spent: list[float] = []
                commands, results = self.refresh(service, base, self.hung_from(0, spent, ssh_timeout=ssh_timeout))
                self.assertEqual(len(commands), 1, "the first silent chunk ends the read")
                per_chunk = 30.0 if ssh_timeout else counters.SMP_CALL_TIMEOUT_SECONDS + counters.SMP_CALL_KILL_AFTER_SECONDS
                self.assertLessEqual(sum(spent), per_chunk)
                fresh = {item.command: item.stdout for item in results if item.ok}
                node = self.expander(self.fabric(fresh, "granted", reads=service.expander_counter_reads()),
                                     SMP_EXPANDER_A)
                summary = node.raw["phy_error_counters"]
                self.assertEqual((summary["state"], summary["reason"]), ("unavailable", "no_response"))
                self.assertEqual(summary["stopped_at_phy"], 0)
                self.assertEqual(summary["message"], counters.HUNG_MESSAGE)
                self.assertTrue(all(row["invalid_dword_count"] is None for row in node.raw["phys"]))
                # The 15-minute interval holds: no retry until it passes, and the mark stays.
                now[0] += counters.SMP_COUNTER_READ_INTERVAL_SECONDS - 1
                self.assertEqual(self.refresh(service, base, self.hung_from(0, spent))[0], [])
                base_outputs = {item.command: item.stdout for item in base}
                node = self.expander(
                    self.fabric(base_outputs, "granted", reads=service.expander_counter_reads()), SMP_EXPANDER_A,
                )
                self.assertEqual(node.raw["phy_error_counters"]["reason"], "no_response")
                now[0] += 1
                self.assertEqual(len(self.refresh(service, base, self.hung_from(0, spent))[0]), 1)

    def test_a_hang_part_way_keeps_the_phys_read_before_it(self):
        import app.services.core_expander_counters as counters

        base = self.base_results(_wide_expander_outputs(40))
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            service._smp_counter_wall_clock = lambda: self.READ_AT
            spent: list[float] = []
            commands, results = self.refresh(service, base, self.hung_from(9, spent))
            self.assertEqual([_chunk_identity(command)[3][0] for command in commands], [0, 4, 8])
            self.assertLessEqual(
                sum(spent), counters.SMP_CALL_TIMEOUT_SECONDS + counters.SMP_CALL_KILL_AFTER_SECONDS + 1,
            )
            fresh = {item.command: item.stdout for item in results if item.ok}
            node = self.expander(self.fabric(fresh, "granted", reads=service.expander_counter_reads()), SMP_EXPANDER_A)
        phys = {int(row["phy"]): row for row in node.raw["phys"]}
        self.assertEqual([phys[phy]["invalid_dword_count"] for phy in range(9)], list(range(1, 10)))
        self.assertEqual((phys[9]["counter_state"], phys[9]["counter_reason"]), ("unknown", "no_response"))
        for phy in range(10, 40):
            self.assertEqual((phys[phy]["counter_state"], phys[phy]["counter_reason"]), ("unknown", "not_read"))
        summary = node.raw["phy_error_counters"]
        self.assertEqual((summary["state"], summary["reason"], summary["stopped_at_phy"]), ("available", "no_response", 9))
        self.assertEqual((summary["measured_phys"], summary["unknown_phys"]), (9, 31))
        self.assertEqual(
            node.metrics["phy_error_counters"],
            "9 PHYs read, 9 with errors, 31 unknown, read stopped at PHY 9 (no answer), read at 2026-11-07T12:00:00Z",
        )

    def test_a_hung_expander_does_not_stop_the_others(self):
        outputs = _nine_expander_outputs(3, num_phys=12)
        from app.services.core_expander_counters import discover_smp_targets

        targets = discover_smp_targets(outputs)
        hung = targets[1].expander_sas_address
        base = self.base_results(outputs)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self.service(temp_dir)
            service._smp_counter_capability = "granted"
            spent: list[float] = []
            silent = self.hung_from(0, spent)

            def answer(command):
                if _chunk_identity(command)[1] == hung:
                    return silent(command)
                return CoreExpanderCounterRetentionTests.valid(command)

            commands, _results = self.refresh(service, base, answer)
            per_expander: dict[str, int] = {}
            for command in commands:
                address = _chunk_identity(command)[1]
                per_expander[address] = per_expander.get(address, 0) + 1
        self.assertEqual(per_expander, {targets[0].expander_sas_address: 3, hung: 1, targets[2].expander_sas_address: 3})
