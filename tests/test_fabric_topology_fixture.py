"""Storage Fabric Topology mode: the payloads its Node tests draw (#937).

``tests/js/fabric_topology_model.test.js`` draws the Topology view from what
the real Python producer emits, not from hand-built payloads: decimal strings
for SAS levels and PHY counts, the canonical ``fail`` and ``missing`` path
states, disk SAS addresses inside the bay trace, and the physical-location
flags of a logical disk inventory. This module builds those payloads from
synthetic inputs through ``build_sas_fabric_snapshot`` and checks that the
committed fixture is exactly that output.

Regenerate with
``python -m tests.test_fabric_topology_fixture --write-fixture``.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, MultipathMember, MultipathView, SlotState, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot

# Fixed, so the committed fixture does not change with the clock.
SNAPSHOT_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sas_fabric" / "topology_fabric.json"


def _address(tag: int) -> str:
    # Invented SAS addresses in the sanitized 5000c500... form, built at run time.
    return f"5000c500{tag:08x}"


FRONT = _address(0xF01)
REAR = _address(0xF02)
UNITS = {0: "a", 1: "b"}
# Bay -> (state of the mpr0 member, state of the mpr1 member), as the
# multipath layer prints them; the producer normalizes them.
MEMBER_STATES = {
    0: ("ACTIVE", "PASSIVE"),
    1: ("ACTIVE", "PASSIVE"),
    2: ("ACTIVE", "FAIL"),
    3: ("ACTIVE", "PASSIVE"),
    4: ("ACTIVE", "PASSIVE"),
    5: ("ACTIVE", "MISSING"),
    6: ("ACTIVE", "PASSIVE"),
    7: ("ACTIVE", "PASSIVE"),
}


def _expander_address(unit: int, level: int) -> str:
    return _address(0xE00 + unit * 0x10 + level)


def _disk_address(unit: int, bay: int) -> str:
    return _address(0xD000 + bay * 0x10 + unit)


def _hba_row(phy: int, device_handle: str = "0009") -> str:
    return f"{phy} 0001 {device_handle} N 12.0 3.0 12.0 SAS Initiator"


def _expander_row(phy: int, remote: int, handle: str, device: str) -> str:
    return f"  {phy} {remote} {handle} 12.0 1.5 12.0 {device}"


def _unit_outputs(unit: int) -> dict[str, str]:
    """One HBA: four lanes to a level-1 expander, four lanes on to a level-2 one."""

    adapter = "\n".join([
        f"mpr{unit} Adapter:",
        "  Board Name: Synthetic 9300-8e",
        *(_hba_row(phy) for phy in range(4)),
        *(f"{phy} 0000 0000 N 0 3.0 12.0 No Device" for phy in range(4, 8)),
    ])
    # Whole PHY tables (one row per PHY), so the linked count is a reading.
    expanders = "\n".join([
        "Num Phys SAS Address      DevHandle Parent EncHandle SAS Level",
        f"36 {_expander_address(unit, 1)} 0009 0001 0002 1",
        *(f"  {phy} 1.5 12.0 No Device" for phy in range(28)),
        *(_expander_row(28 + phy, phy, "000a", "Edge Expander") for phy in range(4)),
        *(_expander_row(32 + phy, phy, "0001", "SAS Initiator") for phy in range(4)),
        f"36 {_expander_address(unit, 2)} 000a 0009 0003 2",
        *(_expander_row(phy, 28 + phy, "0009", "Edge Expander") for phy in range(4)),
        *(f"  {phy} 1.5 12.0 No Device" for phy in range(4, 36)),
    ])
    enclosures = "\n".join([
        f"24 {FRONT} 0008 0002 SES",
        f"24 {REAR} 0018 0003 SES",
    ])
    devices = "\n".join(
        f"00 {bay + unit * 8} {_disk_address(unit, bay)} {0x20 + bay:04x} {'0009' if bay < 4 else '000a'} "
        f"SAS Target 12.0 {'0002' if bay < 4 else '0003'} {bay % 4}"
        for bay in range(8)
    )
    return {
        f"mprutil -u {unit} show adapter": adapter,
        f"mprutil -u {unit} show expanders": expanders,
        f"mprutil -u {unit} show enclosures": enclosures,
        f"mprutil -u {unit} show devices": devices,
    }


def _core_slot(bay: int) -> SlotView:
    first, second = MEMBER_STATES[bay]
    return SlotView(
        slot=bay,
        slot_label=f"{bay:02d}",
        row_index=0,
        column_index=bay,
        present=True,
        state=SlotState.healthy,
        device_name=f"multipath/disk{bay}",
        serial=f"SANITIZED-TOPO-{bay}",
        raw_status={"enclosure_id": FRONT if bay < 4 else REAR, "slot_number": str(bay % 4)},
        multipath=MultipathView(
            name=f"disk{bay}",
            device_name=f"multipath/disk{bay}",
            members=[
                MultipathMember(device_name=f"da{bay}", state=first, controller_label="mpr0"),
                MultipathMember(device_name=f"da{bay + 8}", state=second, controller_label="mpr1"),
            ],
        ),
    )


def _core_case() -> tuple[SystemConfig, InventorySnapshot, dict[str, str]]:
    outputs = {"mprutil show adapters": "/dev/mpr0 SAS3008 Synthetic 16.00.12.00\n/dev/mpr1 SAS3008 Synthetic 16.00.12.00"}
    for unit in UNITS:
        outputs.update(_unit_outputs(unit))
    snapshot = InventorySnapshot(
        slots=[_core_slot(bay) for bay in range(8)],
        refresh_interval_seconds=30,
        last_updated=SNAPSHOT_TIME,
        selected_system_id="synthetic-core",
        selected_system_platform="core",
        selected_enclosure_id=FRONT,
    )
    return SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")), snapshot, outputs


def _virtual_case() -> tuple[SystemConfig, InventorySnapshot, dict[str, str]]:
    slots = [
        SlotView(
            slot=index,
            slot_label=f"{index:02d}",
            row_index=0,
            column_index=index,
            present=True,
            state=SlotState.healthy,
            device_name=f"sd{chr(ord('a') + index)}",
            serial=f"SANITIZED-VIRTUAL-{index}",
            physical_location_known=False,
            raw_status={"virtual_enclosure": True},
        )
        for index in range(2)
    ]
    snapshot = InventorySnapshot(
        slots=slots, refresh_interval_seconds=30, last_updated=SNAPSHOT_TIME, selected_system_id="synthetic-linux",
    )
    return SystemConfig(id="synthetic-linux", label="Synthetic Linux", truenas=TrueNASConfig(platform="linux")), snapshot, {}


def _ses_case() -> tuple[SystemConfig, InventorySnapshot, dict[str, str]]:
    slots = [
        SlotView(
            slot=index,
            slot_label=f"{index:02d}",
            row_index=0,
            column_index=index,
            present=True,
            state=SlotState.healthy,
            device_name=f"sd{chr(ord('a') + index)}",
            serial=f"SANITIZED-SES-{index}",
            sas_address=_address(0xC000 + index),
            ssh_ses_device="/dev/sg9",
        )
        for index in range(3)
    ]
    snapshot = InventorySnapshot(
        slots=slots, refresh_interval_seconds=30, last_updated=SNAPSHOT_TIME, selected_system_id="synthetic-scale",
        selected_system_platform="scale",
    )
    return SystemConfig(id="synthetic-scale", label="Synthetic SCALE", truenas=TrueNASConfig(platform="scale")), snapshot, {}


def _health_case() -> tuple[SystemConfig, InventorySnapshot, dict[str, str]]:
    """Linux block disks whose path state is member health: a faulted disk,
    an unmapped logical disk and an optimal one, in a profile that labels
    slot 0 "01"."""

    slots = [
        SlotView(
            slot=0, slot_label="01", row_index=0, column_index=0, present=True, state=SlotState.fault,
            device_name="sda", serial="SANITIZED-HEALTH-0",
        ),
        SlotView(
            slot=1, slot_label="02", row_index=0, column_index=1, present=True, state=SlotState.unmapped,
            device_name="sdb", serial="SANITIZED-HEALTH-1", physical_location_known=False,
            raw_status={"virtual_enclosure": True},
        ),
        SlotView(
            slot=2, slot_label="03", row_index=0, column_index=2, present=True, state=SlotState.healthy,
            health="OPTIMAL", device_name="sdc", serial="SANITIZED-HEALTH-2",
        ),
    ]
    snapshot = InventorySnapshot(
        slots=slots, refresh_interval_seconds=30, last_updated=SNAPSHOT_TIME, selected_system_id="synthetic-health",
    )
    return SystemConfig(id="synthetic-health", label="Synthetic health", truenas=TrueNASConfig(platform="linux")), snapshot, {}


def _non_ascii_label_case() -> tuple[SystemConfig, InventorySnapshot, dict[str, str]]:
    """SCALE SES bays whose profile labels them with Arabic-Indic digits
    (U+0661..U+0663), which the producer keeps as the bay label, beside one
    ASCII label."""

    labels = ["\u0661", "\u0662", "\u0663", "04"]
    slots = [
        SlotView(
            slot=index,
            slot_label=label,
            row_index=0,
            column_index=index,
            present=True,
            state=SlotState.healthy,
            device_name=f"sd{chr(ord('a') + index)}",
            serial=f"SANITIZED-LABEL-{index}",
            ssh_ses_device="/dev/sg9",
        )
        for index, label in enumerate(labels)
    ]
    snapshot = InventorySnapshot(
        slots=slots, refresh_interval_seconds=30, last_updated=SNAPSHOT_TIME, selected_system_id="synthetic-labels",
        selected_system_platform="scale",
    )
    return SystemConfig(id="synthetic-labels", label="Synthetic labels", truenas=TrueNASConfig(platform="scale")), snapshot, {}


CASES = {
    "core_cascade": _core_case,
    "linux_virtual": _virtual_case,
    "scale_ses": _ses_case,
    "linux_health": _health_case,
    "ses_non_ascii_labels": _non_ascii_label_case,
}


def _case_payload(build) -> dict:
    system, snapshot, outputs = build()
    fabric = build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs=outputs).model_dump(mode="json")
    fabric.pop("generated_at")
    inventory = snapshot.model_dump(mode="json")
    inventory.pop("generated_at", None)
    return {"fabric": fabric, "snapshot": inventory}


def build_fixture() -> dict:
    return {name: _case_payload(build) for name, build in CASES.items()}


class FabricTopologyFixtureTests(unittest.TestCase):
    def test_projection_sorts_one_boards_members_only_once(self) -> None:
        from unittest.mock import patch

        from app.services.sas_fabric_enclosures import project_enclosures

        _, snapshot, _ = _core_case()
        slot = snapshot.slots[0]
        snapshot.slots = [slot.model_copy(update={"slot": index}) for index in reversed(range(4096))]
        nodes = {}
        with patch("app.services.sas_fabric_enclosures.sorted", wraps=sorted, create=True) as order:
            project_enclosures(nodes, {}, {}, snapshot)
        self.assertEqual(nodes[f"enclosure:{FRONT}"].related_slots, list(range(4096)))
        self.assertLessEqual(order.call_count, 2)

    def test_thirteen_generality_fabrics_are_current_real_builder_output(self) -> None:
        from tests.topology_generality_fixtures import fixtures

        actual = fixtures()
        expected = json.loads(FIXTURE.with_name("topology_generality.json").read_text(encoding="utf-8"))
        self.assertEqual(len(actual), 13)
        self.assertEqual(len({item["id"] for item in actual}), 13)
        self.assertEqual(expected, {"fixtures": actual})
        for item in actual:
            if item["transport"]:
                self.assertTrue(item["fabric"]["raw"]["sas_transport_ports"])
                self.assertNotIn("sandbox_transport_ports", item["fabric"]["raw"])

    def test_fixture_is_current_builder_output(self) -> None:
        self.assertEqual(json.loads(FIXTURE.read_text(encoding="utf-8")), build_fixture())

    def test_fixture_carries_the_field_shapes_the_view_must_read(self) -> None:
        # The shapes the Topology view was wrong about: if the producer
        # changes them, this fails before the Node test silently stops
        # covering them.
        core = build_fixture()["core_cascade"]["fabric"]
        expanders = [node for node in core["nodes"] if node["kind"] == "expander"]
        self.assertEqual(sorted(node["metrics"]["sas_level"] for node in expanders), ["1", "1", "2", "2"])
        self.assertTrue(all(node["metrics"]["num_phys"] == "36" for node in expanders))
        self.assertEqual(sorted(node["metrics"]["linked_phys"] for node in expanders), [4, 4, 8, 8])
        kinds = {link["kind"] for link in core["links"]}
        self.assertTrue({"controller-expander", "expander-expander", "mpr-enclosure-bay"} <= kinds, kinds)
        states = {state["state"] for trace in core["traces"] if trace["kind"] == "bay" for state in trace["metrics"]["path_states"]}
        self.assertEqual(states, {"active", "passive", "fail", "missing"})
        addresses = {
            device["sas_address"]
            for trace in core["traces"] if trace["kind"] == "bay"
            for device in trace["metrics"]["mpr_devices"]
        }
        self.assertIn(_disk_address(1, 2), addresses)
        self.assertFalse(any("serial" in node["metrics"] for node in core["nodes"] if node["kind"] == "bay"))
        lanes = [link["reported_lanes"] for link in core["links"] if link["kind"] in {"controller-expander", "expander-expander"}]
        self.assertTrue(lanes and all(item is not None for item in lanes), lanes)

        virtual = build_fixture()["linux_virtual"]["fabric"]
        bays = [node for node in virtual["nodes"] if node["kind"] == "bay"]
        self.assertTrue(bays)
        for node in bays:
            self.assertIs(node["metrics"]["physical_location_known"], False)
            self.assertIs(node["metrics"]["virtual_enclosure"], True)
            self.assertTrue(node["label"].startswith("Disk "))
        self.assertEqual(virtual["raw"]["fabric_kind"], "storage_linux")

        ses = build_fixture()["scale_ses"]["fabric"]
        self.assertEqual(ses["raw"]["fabric_kind"], "linux_ses")

        # The producer keeps a label of non-ASCII digits as the bay label.
        labelled = build_fixture()["ses_non_ascii_labels"]["fabric"]
        bay_labels = sorted(node["label"] for node in labelled["nodes"] if node["kind"] == "bay")
        self.assertEqual(bay_labels, ["Bay 04", "Bay \u0661", "Bay \u0662", "Bay \u0663"])

    def test_browser_crawl_draws_topology_bays_and_lines_from_this_fixture(self) -> None:
        # The CI browser crawl renders Topology from this fixture (in the demo
        # scope), so its bay cells and lines are drawn there, not excused.
        from app.services.public_demo_fixture import build_public_demo_snapshot_bundle
        from scripts.build_ui_crawl_fixture import topology_fabric

        bundle = build_public_demo_snapshot_bundle()
        fabric = topology_fabric(bundle)
        demo = bundle.sas_fabric.model_dump(mode="json")
        self.assertEqual(fabric["system_id"], demo["system_id"])
        self.assertEqual(fabric["selected_enclosure_id"], demo["selected_enclosure_id"])
        self.assertTrue([node for node in fabric["nodes"] if node["kind"] == "bay"])
        self.assertTrue({"controller-expander", "expander-expander", "mpr-enclosure-bay"} <= {link["kind"] for link in fabric["links"]})
        inventory = json.loads((Path(__file__).resolve().parents[1] / "qa" / "ui-states.json").read_text(encoding="utf-8"))
        gaps = {gap["key"] for gap in inventory["render_gaps"]}
        drawn = {"[data-topo-bay]", "[data-topo-link]", "[data-fabric-topology-link]"}
        self.assertFalse(drawn & gaps)
        states = [state for state in inventory["states"] if state.get("variant") == "fabric-topology" and state.get("ci")]
        self.assertTrue(states)
        self.assertTrue(drawn <= {key for state in states for key in state["covers"]})


def _core_variant(mode: str):
    """The CORE case with one adapter or expander header line changed (#1504)."""

    def build():
        system, snapshot, outputs = _core_case()
        adapter, devices, expanders = "mprutil -u 0 show adapter", "mprutil -u 0 show devices", "mprutil -u 0 show expanders"
        enclosures = "mprutil -u 0 show enclosures"
        # PHY 4 of mpr0 given a handle: a disk's, the enclosure processor's,
        # or one nothing carries; and the row's own device type.
        handle = {"present": "0020", "behind": "0020", "type-clash": "0020", "sep": "0008", "sep-unlisted": "0008"}.get(mode, "009b")
        device = {"sep": "SEP Target", "sep-unlisted": "SEP Target", "type-clash": "Edge Expander"}.get(mode, "SAS Target")
        if mode in {"present", "absent", "behind", "type-clash", "sep", "sep-unlisted"}:
            outputs[adapter] = outputs[adapter].replace("4 0000 0000 N 0 3.0 12.0 No Device", f"4 0001 {handle} N 12.0 3.0 12.0 {device}")
        if mode in {"present", "absent", "type-clash"}:
            outputs[devices] = outputs[devices].replace("5000c5000000d000 0020 0009 SAS Target", "5000c5000000d000 0020 0001 SAS Target")
        if mode == "sep":
            outputs[devices] += f"\n00 8 {FRONT} 0008 0001 SEP Target 12.0 0002 8"
        if mode == "duplicate":
            outputs[expanders] = outputs[expanders].replace("5000c50000000e02 000a 0009", "5000c50000000e02 0009 0009")
        # The expander's handle on a second device: the same expander seen
        # again, another SMP target, or the enclosure processor.
        if mode in {"smp-same", "smp-other", "smp-other-parent"}:
            address = _address(0xEEE) if mode == "smp-other" else _expander_address(0, 1)
            parent = "0002" if mode == "smp-other-parent" else "0001"
            outputs[devices] += f"\n00 8 {address} 0009 {parent} SMP Target 12.0 0002 8"
        if mode == "sep-collision":
            outputs[enclosures] = outputs[enclosures].replace("0008 0002 SES", "0009 0002 SES")
        # PHY 4 names disk handle 0020, which one disk (or two) of the
        # device list carries, with or without the expander reply read.
        if mode.startswith("partial"):
            outputs[adapter] = outputs[adapter].replace("4 0000 0000 N 0 3.0 12.0 No Device", "4 0001 0020 N 12.0 3.0 12.0 SAS Target")
            outputs[devices] = outputs[devices].replace("5000c5000000d000 0020 0009", "5000c5000000d000 0020 0001")
            if not mode.endswith("-unique"):
                outputs[devices] += f"\n00 8 {_address(0xEEE)} 0020 0001 SAS Target 12.0 0003 8"
            if "-unread" in mode:
                del outputs[expanders]
        if mode.startswith("cascade-without-"):
            del outputs[f"mprutil -u 0 show {mode.removeprefix('cascade-without-')}"]
        # A second disk of the unit with Bay 00's disk handle, behind an
        # expander PHY that names that handle; no adapter row names it.
        if mode in {"disk-clean", "disk-collision"}:
            outputs[expanders] = outputs[expanders].replace("  5 1.5 12.0 No Device", "  5 0 0020 3.0 1.5 12.0 SAS Target", 1)
            if mode == "disk-collision":
                outputs[devices] += f"\n00 8 {_address(0xEEE)} 0020 0009 SAS Target 12.0 0003 8"
        # PHY 4 names a handle only the device list carries, as an expander.
        if mode in {"listed-smp", "unlisted-exp"}:
            outputs[adapter] = outputs[adapter].replace("4 0000 0000 N 0 3.0 12.0 No Device", "4 0001 009b N 12.0 3.0 12.0 Edge Expander")
            if mode == "listed-smp":
                outputs[devices] += f"\n00 8 {_address(0xEEE)} 009b 0001 SMP Target 12.0 0002 8"
        return system, snapshot, outputs

    return _case_payload(build)["fabric"]


class AdapterHandleIdentityTests(unittest.TestCase):
    """A CORE adapter row's handle is decided over the unit, before selection (#1504)."""

    def test_a_handle_two_expanders_carry_is_ambiguous_though_one_is_outside_the_selection(self) -> None:
        fabric = _core_variant("duplicate")
        shown = {expander["id"] for expander in fabric["expanders"]}
        self.assertNotIn("expander:controller:mpr0:5000c50000000e02", shown)
        mpr0 = next(item for item in fabric["controllers"] if item["id"] == "controller:mpr0")
        self.assertEqual(mpr0.get("device_handle_states"), {"0": "ambiguous", "1": "ambiguous", "2": "ambiguous", "3": "ambiguous"})
        # The adapter rows stay as reported.
        self.assertTrue(all("device_handle_state" not in row for row in mpr0["phy_rows"]))

    def test_a_handle_one_expander_carries_has_no_state(self) -> None:
        self.assertTrue(all("device_handle_states" not in item for item in _core_variant("base")["controllers"]))

    def test_a_handle_only_a_disk_carries_names_that_disk(self) -> None:
        mpr0 = next(item for item in _core_variant("present")["controllers"] if item["id"] == "controller:mpr0")
        self.assertEqual(mpr0.get("device_handle_states"), {"4": "end_device"})
        # A handle no list carries is not marked: it is outside the payload.
        absent = next(item for item in _core_variant("absent")["controllers"] if item["id"] == "controller:mpr0")
        self.assertNotIn("device_handle_states", absent)

    @staticmethod
    def _mpr0(mode: str) -> dict:
        return next(item for item in _core_variant(mode)["controllers"] if item["id"] == "controller:mpr0")

    def test_cascade_child_handle_must_be_unique_even_if_the_second_table_is_empty(self) -> None:
        from app.services.sas_fabric_core import _MprReportedLaneContext

        expanders = [row for row in _core_variant("base")["expanders"] if row["controller_id"] == "controller:mpr0"]
        child = next(index for index, row in enumerate(expanders) if row["parent"] == expanders[0]["dev_handle"])
        self.assertIsNotNone(_MprReportedLaneContext(expanders, None, 0).parent_link_lanes(child, to_controller=False))
        other = {**expanders[child], "sas_address": _address(0xEEE), "phys": []}
        for records, index in (([*expanders, other], child), ([other, *expanders], child + 1)):
            self.assertIsNone(_MprReportedLaneContext(records, None, 0).parent_link_lanes(index, to_controller=False))

    def test_cascade_lanes_need_every_unit_list(self) -> None:
        for missing in ("devices", "enclosures"):
            fabric = _core_variant(f"cascade-without-{missing}")
            cascades = [link for link in fabric["links"] if link["kind"] == "expander-expander"]
            self.assertFalse(any(link["reported_lanes"] for link in cascades if "controller:mpr0:" in link["source"]))
            self.assertTrue(any(link["reported_lanes"] for link in cascades if "controller:mpr1:" in link["source"]))

    def test_the_enclosure_processor_a_handle_names_is_one_whichever_reply_names_it(self) -> None:
        # Named by the device list and the enclosure list, or by the
        # enclosure list alone: the enclosure processor, never a disk.
        for mode in ("sep", "sep-unlisted"):
            self.assertEqual(self._mpr0(mode).get("device_handle_states"), {"4": "sep"}, mode)

    def test_a_disk_handle_whose_device_is_not_attached_to_this_hba_or_of_another_type_contradicts_the_row(self) -> None:
        # The disk's own parent is the expander, or the row says an expander
        # is attached: the row and the device list disagree.
        for mode in ("behind", "type-clash"):
            self.assertEqual(self._mpr0(mode).get("device_handle_states"), {"4": "mismatch"}, mode)

    def test_an_expander_handle_a_second_device_carries_is_ambiguous_and_its_cable_lanes_are_not_reported(self) -> None:
        # The same expander's address under another parent is one device
        # whose records disagree: ambiguous too.
        for mode in ("smp-other", "sep-collision", "smp-other-parent"):
            fabric = _core_variant(mode)
            mpr0 = next(item for item in fabric["controllers"] if item["id"] == "controller:mpr0")
            self.assertEqual(mpr0.get("device_handle_states"), {str(phy): "ambiguous" for phy in range(4)}, mode)
            cable = next(link for link in fabric["links"] if link["kind"] == "controller-expander" and link["source"] == "controller:mpr0")
            self.assertIsNone(cable["reported_lanes"], mode)

    def test_an_unread_list_never_relaxes_ambiguity_and_leaves_a_single_device_not_read(self) -> None:
        # Two disks with the handle, read: ambiguous whether or not the
        # expander reply was read. One disk read with the expander reply not
        # read: what the handle names is not read, never that disk.
        self.assertEqual(self._mpr0("partial").get("device_handle_states"), {"4": "ambiguous"})
        unread = self._mpr0("partial-unread")
        self.assertEqual(unread.get("device_handle_states", {}).get("4"), "ambiguous")
        self.assertEqual(unread.get("mpr_lists", {}).get("expanders"), "not_read")
        states = self._mpr0("partial-unread-unique").get("device_handle_states", {})
        self.assertEqual({states.get(str(phy)) for phy in range(5)}, {"not_read"})
        self.assertEqual(self._mpr0("partial-unique").get("device_handle_states"), {"4": "end_device"})

    def test_every_handle_two_devices_of_the_unit_carry_is_delivered_with_its_hba(self) -> None:
        self.assertIn("0020", self._mpr0("disk-collision").get("ambiguous_device_handles", []))
        self.assertNotIn("ambiguous_device_handles", self._mpr0("disk-clean"))
        # The lists each reply was read from travel with the HBA, by id.
        self.assertEqual(self._mpr0("base").get("mpr_lists"), {"devices": "read", "enclosures": "read", "expanders": "read"})

    def test_an_expander_only_the_device_list_names_is_not_absent(self) -> None:
        self.assertEqual(self._mpr0("listed-smp").get("device_handle_states"), {"4": "unlisted_expander"})
        self.assertNotIn("device_handle_states", self._mpr0("unlisted-exp"))

    def test_the_same_expander_seen_again_in_the_device_list_is_one_device(self) -> None:
        fabric = _core_variant("smp-same")
        mpr0 = next(item for item in fabric["controllers"] if item["id"] == "controller:mpr0")
        self.assertNotIn("device_handle_states", mpr0)
        cable = next(link for link in fabric["links"] if link["kind"] == "controller-expander" and link["source"] == "controller:mpr0")
        self.assertEqual(cable["reported_lanes"]["lane_count"], 4)

    def test_handle_identity_reconciliation_is_once_per_unit(self) -> None:
        from unittest.mock import patch

        from app.services import sas_fabric_core

        with patch.object(
            sas_fabric_core,
            "_mpr_handle_devices",
            wraps=sas_fabric_core._mpr_handle_devices,
        ) as reconcile:
            _core_variant("base")

        self.assertEqual(reconcile.call_count, len(UNITS))

    def test_named_disk_destination_depends_on_exact_adapter_type(self) -> None:
        from app.services.sas_fabric_core import _mpr_adapter_handle_states
        from app.services.sas_fabric_parsers import parse_mpr_device_list

        devices, rejected = parse_mpr_device_list(f"00 3 {_address(0xD01)} 0020 0001 SAS Target da3 12.0 0002 3 1")
        self.assertEqual(rejected, 0)
        adapter = {"phy": "4", "controller_handle": "0001", "device_handle": "0020", "device": "SAS Target"}
        payload = {"devices": devices, "lists": {"devices": "read", "expanders": "empty", "enclosures": "empty"}}
        self.assertEqual(_mpr_adapter_handle_states([adapter], payload), {"4": "end_device"})
        unknown = {**adapter, "device": "SAS Target unrecognized-description"}
        self.assertEqual(_mpr_adapter_handle_states([unknown], payload), {"4": "mismatch"})
        # Older device-list rows had no separate type; discrepant evidence must not grant a disk destination.
        legacy = {k: v for k, v in devices[0].items() if k != "device_type"}
        self.assertEqual(_mpr_adapter_handle_states([adapter], {**payload, "devices": [legacy]}), {"4": "end_device"})
        disputed = {**devices[0], "device_type": "SEP Target"}
        self.assertEqual(_mpr_adapter_handle_states([adapter], {**payload, "devices": [disputed]}), {"4": "mismatch"})

    def test_missing_identity_fields_do_not_bridge_parent_disagreements_in_any_order(self) -> None:
        from app.services.sas_fabric_core import _mpr_adapter_handle_states

        adapter_rows = [{
            "phy": "4",
            "controller_handle": "0001",
            "device_handle": "0020",
            "device": "SAS Target",
        }]
        observations = [
            {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target", "parent": None},
            {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target", "parent": "0001"},
            {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target", "parent": "0002"},
        ]
        for rows in (observations, observations[::-1]):
            with self.subTest(parents=[row["parent"] for row in rows]):
                payload = {
                    "lists": {"devices": "read", "enclosures": "empty", "expanders": "empty"},
                    "devices": rows,
                    "enclosures": [],
                    "expanders": [],
                }
                self.assertEqual(_mpr_adapter_handle_states(adapter_rows, payload), {"4": "ambiguous"})

    def test_addressed_identity_disagreements_are_ambiguous_in_any_order(self) -> None:
        from app.services.sas_fabric_core import _mpr_adapter_handle_states

        adapter_rows = [{
            "phy": "4",
            "controller_handle": "0001",
            "device_handle": "0020",
            "device": "SAS Target",
        }]
        cases = {
            "parent": [
                {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target",
                 "sas_address": _address(0xD01), "parent": "0001"},
                {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target",
                 "sas_address": _address(0xD01), "parent": "0002"},
            ],
            "address": [
                {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target",
                 "sas_address": _address(0xD01), "parent": "0001"},
                {"handle": "0020", "device": "SAS Target", "device_type": "SAS Target",
                 "sas_address": _address(0xD02), "parent": "0001"},
            ],
        }
        for disagreement, observations in cases.items():
            for rows in (observations, observations[::-1]):
                with self.subTest(disagreement=disagreement, order=[row["sas_address"] for row in rows]):
                    payload = {
                        "lists": {"devices": "read", "enclosures": "empty", "expanders": "empty"},
                        "devices": rows,
                        "enclosures": [],
                        "expanders": [],
                    }
                    self.assertEqual(_mpr_adapter_handle_states(adapter_rows, payload), {"4": "ambiguous"})


class TransportAddressIdentityTests(unittest.TestCase):
    """A disk port address at two slots identifies neither, host-wide (#1504)."""

    def _records(self, duplicate: bool, inventory_addresses: bool = True, missing_ordinal: bool = False) -> list[dict]:
        from tests import topology_generality_fixtures as generality

        survey = generality.Survey()
        host = survey.host(1, address="5aaa000000000101", phys=4)
        expander = survey.expander(host, [0, 1], index=0, address="5bbb00000000a1b2", phys=4, uplink=[2, 3])
        shelf = {"id": "5fff0000000000f0", "label": "Test shelf", "layout": [[0, 1]], "slots": []}
        for bay in (0, 1):
            address = "5eee000000000010" if duplicate or bay == 0 else "5eee000000000011"
            survey.end(expander, [bay], bay=bay, enclosure=shelf["id"], address=address, block=f"wdcase{bay}")
            if missing_ordinal and bay == 1:
                attrs = survey.rows[-2]["attrs"]
                del attrs["bay_identifier"]
                attrs["enclosure_identifier"] = "0x" + REAR
            shelf["slots"].append({"slot": bay, "address": address if inventory_addresses else None, "block": f"wdcase{bay}"})
        fabric, _inventory, ok = generality.enriched({"id": "example", "label": "Test box", "enclosures": [shelf]}, shelf, survey.text())
        self.assertTrue(ok)
        return [record for record in fabric["raw"]["sas_transport_ports"] if record["remote_kind"] == "end_device"]

    def test_different_read_enclosures_conflict_even_without_a_bay_number(self) -> None:
        records = self._records(duplicate=True, inventory_addresses=False, missing_ordinal=True)
        self.assertCountEqual([row["bay_identifier"] for row in records], [0, None])
        self.assertTrue(all(row.get("remote_address_state") == "ambiguous" for row in records))

    def test_one_address_at_two_transport_slots_marks_both_records_whatever_the_inventory_reports(self) -> None:
        # The transport alone places one address at two slots.
        records = self._records(duplicate=True, inventory_addresses=False)
        self.assertEqual(sorted(record["bay_identifier"] for record in records), [0, 1])
        self.assertTrue(all(record.get("remote_address_state") == "ambiguous" for record in records))

    def test_one_address_at_two_slots_marks_both_records(self) -> None:
        records = self._records(duplicate=True)
        self.assertEqual(sorted(record["bay_identifier"] for record in records), [0, 1])
        self.assertTrue(all(record["remote_address_state"] == "ambiguous" for record in records))

    def test_distinct_addresses_are_not_marked(self) -> None:
        self.assertTrue(all("remote_address_state" not in record for record in self._records(duplicate=False)))

    def _survey(self, mode: str) -> list[dict]:
        """Two bays whose inventory reports one address (``partial``: only
        Bay 01's transport end is read), or whose second disk the transport
        places in another enclosure than its bay (``contradict``)."""
        from tests import topology_generality_fixtures as generality

        survey = generality.Survey()
        host = survey.host(1, address="5aaa000000000101", phys=4)
        expander = survey.expander(host, [0, 1], index=0, address="5bbb00000000a1b2", phys=4, uplink=[2, 3])
        shelf = {"id": "5fff0000000000f0", "label": "Test shelf", "layout": [[0, 1]], "slots": []}
        for bay in (0, 1):
            address = "5eee000000000010" if mode == "partial" or bay == 0 else "5eee000000000011"
            if mode != "partial" or bay == 1:
                placed = mode == "contradict" and bay == 1
                survey.end(expander, [bay], bay=0 if placed else bay, enclosure="5fff0000000000aa" if placed else shelf["id"], address=address, block=f"wdcase{bay}")
            shelf["slots"].append({"slot": bay, "address": address, "block": f"wdcase{bay}"})
        fabric, _inventory, ok = generality.enriched({"id": "example", "label": "Test box", "enclosures": [shelf]}, shelf, survey.text())
        self.assertTrue(ok)
        return [record for record in fabric["raw"]["sas_transport_ports"] if record["remote_kind"] == "end_device"]

    def test_an_address_the_inventory_reports_for_two_bays_is_ambiguous_though_one_end_is_not_read(self) -> None:
        records = self._survey("partial")
        self.assertEqual([record["bay_identifier"] for record in records], [1])
        self.assertEqual(records[0].get("remote_address_state"), "ambiguous")

    def test_a_disk_the_transport_places_in_another_enclosure_than_its_bay_is_ambiguous(self) -> None:
        records = {record["remote_sas_address"]: record for record in self._survey("contradict")}
        self.assertEqual(records["5eee000000000011"].get("remote_address_state"), "ambiguous")
        # The disk whose enclosure agrees is not marked.
        self.assertNotIn("remote_address_state", records["5eee000000000010"])

    def test_agreeing_inventory_and_transport_mark_nothing(self) -> None:
        self.assertTrue(all("remote_address_state" not in record for record in self._survey("agreement")))


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--write-generality-fixture"]:
        from tests.topology_generality_fixtures import fixtures
        FIXTURE.with_name("topology_generality.json").write_text(json.dumps({"fixtures": fixtures()}, indent=1) + "\n", encoding="utf-8")
    elif sys.argv[1:] == ["--write-fixture"]:
        FIXTURE.write_text(json.dumps(build_fixture(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        unittest.main()
