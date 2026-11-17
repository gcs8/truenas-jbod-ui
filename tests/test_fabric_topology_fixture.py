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


CASES = {"core_cascade": _core_case, "linux_virtual": _virtual_case, "scale_ses": _ses_case, "linux_health": _health_case}


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


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--write-fixture"]:
        FIXTURE.write_text(json.dumps(build_fixture(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        unittest.main()
