"""CORE Storage Fabric controller identity without a numbered MPR unit (#1219).

When no numbered ``mprutil -u N`` output exists, the CORE builder falls back
to the unnumbered ``mprutil show ...`` commands. That fallback must not invent
a controller when nothing was observed, must keep the adapter details it was
given, and every link, trace and controller reference must name a node that
exists in the same graph.
"""

import json
import unittest
from pathlib import Path

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, MultipathMember, MultipathView, SlotState, SlotView
from app.services.inventory import InventoryService
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_parsers import (
    build_core_mprutil_unit_commands,
    parse_mpr_adapter_detail,
    parse_mpr_enclosures,
    parse_mpr_expanders,
)
from app.services.ssh_probe import SSHCommandResult

NO_CORE_DATA_WARNING = "No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions."
UNREADABLE_MPRUTIL_WARNING = (
    "CORE mprutil output was returned but could not be read as controller data, so controller details are unknown."
)
RENDER_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sas_fabric" / "core_no_controller_fabric.json"
SELECTED_ENCLOSURE = "5000000000000001"
EXPANDER_ADDRESS = "5000000000000002"

UNREADABLE_UNIT_WARNING = (
    "CORE controller data could not be read for mpr1, so controller, expander and enclosure counts may be incomplete."
)
NO_MPRUTIL_DATA_WARNING = (
    "CORE mprutil returned no controller data, so expander and enclosure counts and controller details were not read."
)
LISTS_MISSING_WARNING = (
    "CORE controller mpr0 did not return a readable expander or enclosure list, so those counts may be incomplete."
)
ALL_COUNTS = ("controllers", "paths", "expanders", "enclosures")

# mprutil prints an expander's enclosure handle in decimal ("%04d") and the
# enclosure list's in hexadecimal ("%04x"): 0016 and 0010 are one handle.
LEGACY_OUTPUTS = {
    "mprutil show adapter": (
        "Synthetic Adapter:\n"
        "Board Name: Synthetic HBA\n"
        "Firmware Revision: 1.2.3\n"
    ),
    "mprutil show expanders": f"1 {EXPANDER_ADDRESS} 000a 0001 0016 1\n",
    "mprutil show enclosures": f"1 {SELECTED_ENCLOSURE} 000b 0010 SES\n",
}

# A whole read of unit 0: adapter detail plus its (empty) expander and enclosure lists.
NUMBERED_UNIT_OUTPUTS = {
    "mprutil -u 0 show adapter": LEGACY_OUTPUTS["mprutil show adapter"],
    "mprutil -u 0 show expanders": "",
    "mprutil -u 0 show enclosures": "",
}


def _slot(**overrides) -> SlotView:
    values = {"slot": 0, "slot_label": "00", "row_index": 0, "column_index": 0}
    values.update(overrides)
    return SlotView(**values)


def _inventory_slot() -> SlotView:
    return _slot(
        present=True,
        state=SlotState.healthy,
        device_name="da0",
        pool_name="tank",
        vdev_name="mirror-0",
        raw_status={"device_names": ["da0"]},
    )


def _failure_records(*commands: str) -> list[dict]:
    """The records the inventory hands the builder for failed commands (it names no command)."""
    results = [SSHCommandResult(command=command, ok=False, exit_code=1) for command in commands]
    records = InventoryService._ssh_failure_details(results)
    assert not any("command" in key for record in records for key in record), records
    return records


def _core_fabric(
    outputs: dict[str, str],
    *,
    slots: list[SlotView] | None = None,
    selected: str | None = None,
    command_failures: list[dict] | None = None,
):
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
        snapshot=InventorySnapshot(
            slots=slots if slots is not None else [_slot()],
            refresh_interval_seconds=30,
            selected_enclosure_id=selected,
        ),
        ssh_outputs=outputs,
        command_failures=command_failures,
    )


def _virtual_slot(slot: int = 0) -> SlotView:
    return _inventory_slot().model_copy(
        update={"slot": slot, "slot_label": f"{slot:02d}", "column_index": slot, "physical_location_known": False}
    )


def _multipath_slot() -> SlotView:
    # The inventory saw this disk on mpr0, so the disk view's trace reaches that controller.
    return _inventory_slot().model_copy(update={"multipath": MultipathView(
        name="disk0",
        device_name="multipath/disk0",
        members=[MultipathMember(device_name="da0", state="ACTIVE", controller_label="mpr0")],
    )})


def _render_case_inputs() -> dict[str, tuple]:
    """Builder inputs for the payloads the Node page test draws with the real page code."""
    second_physical = _inventory_slot().model_copy(update={"slot": 1, "slot_label": "01", "column_index": 1})
    return {
        "no_controller_bays": ({}, [_inventory_slot()]),
        "no_controller_virtual": ({}, [_virtual_slot()]),
        "no_controller_mixed": ({}, [second_physical.model_copy(update={"slot": 0, "slot_label": "00", "column_index": 0}), _virtual_slot(1)]),
        "unreadable_second_unit": ({**NUMBERED_UNIT_OUTPUTS, "mprutil -u 1 show adapter": "unrecognized synthetic text\n"}, [_inventory_slot()]),
        "complete": (dict(NUMBERED_UNIT_OUTPUTS), [_inventory_slot()]),
        "unparsed_lists": (
            {
                **NUMBERED_UNIT_OUTPUTS,
                "mprutil -u 0 show expanders": "unrecognized synthetic text\n",
                "mprutil -u 0 show enclosures": "unrecognized synthetic text\n",
            },
            [_inventory_slot()],
        ),
        "phy_table_rejected": (
            {**NUMBERED_UNIT_OUTPUTS, "mprutil -u 0 show adapter": LEGACY_OUTPUTS["mprutil show adapter"] + "0 malformed\n"},
            [_inventory_slot()],
        ),
        "phy_table_rejected_on_path": (
            {**NUMBERED_UNIT_OUTPUTS, "mprutil -u 0 show adapter": LEGACY_OUTPUTS["mprutil show adapter"] + "0 malformed\n"},
            [_multipath_slot()],
        ),
        **{name: (outputs, [_inventory_slot()], SELECTED_ENCLOSURE) for name, outputs in _healthy_reply_cases().items()},
        **{name: (outputs, [_inventory_slot()], SELECTED_ENCLOSURE) for name, outputs in _unread_reply_cases().items()},
    }


def _render_case_payload(outputs: dict[str, str], slots: list[SlotView], selected: str | None = None) -> dict:
    fabric = _core_fabric(outputs, slots=slots, selected=selected).model_dump(mode="json")
    fabric.pop("generated_at")
    return {"fabric": fabric, "slots": [slot.model_dump(mode="json") for slot in slots]}


def _render_cases() -> dict:
    return {name: _render_case_payload(*inputs) for name, inputs in _render_case_inputs().items()}


class CoreFabricLegacyControllerTests(unittest.TestCase):
    def assert_references_resolve(self, fabric) -> None:
        node_ids = {node.id for node in fabric.nodes}
        link_ids = {link.id for link in fabric.links}
        dangling_links = [
            (link.source, link.target, link.kind)
            for link in fabric.links
            if link.source not in node_ids or link.target not in node_ids
        ]
        dangling_controller_refs = [
            (node.id, node.controller_id)
            for node in fabric.nodes
            if node.controller_id and node.controller_id not in node_ids
        ]
        dangling_raw_controller_refs = [
            (node.id, node.raw.get("controller_id"))
            for node in fabric.nodes
            if node.raw.get("controller_id") and node.raw["controller_id"] not in node_ids
        ]
        dangling_trace_refs = [
            (trace.id, ref)
            for trace in fabric.traces
            for ref in [*trace.node_ids, *trace.link_ids]
            if ref not in node_ids and ref not in link_ids
        ]
        self.assertEqual(dangling_links, [])
        self.assertEqual(dangling_controller_refs, [])
        self.assertEqual(dangling_raw_controller_refs, [])
        self.assertEqual(dangling_trace_refs, [])
        self.assertEqual(
            sorted(controller["id"] for controller in fabric.controllers),
            sorted(node.id for node in fabric.nodes if node.kind == "controller"),
        )

    def test_no_mprutil_output_does_not_invent_a_controller(self) -> None:
        slot = _slot()
        self.assertTrue(slot.physical_location_known)

        fabric = _core_fabric({}, slots=[slot])

        self.assertEqual(fabric.controllers, [])
        self.assertEqual([node.id for node in fabric.nodes if node.kind == "controller"], [])
        self.assertIn(NO_CORE_DATA_WARNING, fabric.warnings)
        self.assertNotIn("mpr-1", fabric.model_dump_json())
        self.assert_references_resolve(fabric)

    def test_no_mprutil_output_and_no_inventory_is_unavailable(self) -> None:
        fabric = _core_fabric({}, slots=[])

        self.assertFalse(fabric.available)
        self.assertEqual(fabric.controllers, [])
        self.assertIn(NO_CORE_DATA_WARNING, fabric.warnings)

    def test_blank_unnumbered_output_does_not_invent_a_controller(self) -> None:
        fabric = _core_fabric({"mprutil show adapter": "  \n", "mprutil show devices": ""})

        self.assertEqual(fabric.controllers, [])
        self.assertIn(NO_CORE_DATA_WARNING, fabric.warnings)
        self.assertNotIn(UNREADABLE_MPRUTIL_WARNING, fabric.warnings)

    def test_no_controller_output_still_draws_known_bays_pools_and_vdevs(self) -> None:
        fabric = _core_fabric({}, slots=[_inventory_slot()])

        # The inventory still knows the bay, pool and vdev, so the map stays
        # drawable; the notice says the controller data could not be read.
        self.assertTrue(fabric.available)
        kinds = {node.kind for node in fabric.nodes}
        self.assertTrue({"bay", "pool", "vdev"} <= kinds, kinds)
        self.assertNotIn("controller", kinds)
        self.assertEqual(fabric.warnings, [NO_CORE_DATA_WARNING])
        self.assert_references_resolve(fabric)

    def test_render_fixture_is_current_builder_output(self) -> None:
        # The Node page test (tests/js/fabric_core_no_controller.test.js) draws
        # these payloads with the real page code. Regenerate with
        # `python -m tests.test_core_fabric_legacy_controller --write-render-fixture`.
        self.assertEqual(json.loads(RENDER_FIXTURE.read_text(encoding="utf-8")), _render_cases())

    def test_unread_or_malformed_phy_table_stays_unknown(self) -> None:
        adapter_without_table = LEGACY_OUTPUTS["mprutil show adapter"]
        malformed_table = adapter_without_table + "0 0001 000a N 6.0\n1 0001 000b N 6.0 1.5 6.0 SAS Target\n"
        cases = {
            "legacy adapter missing": {"mprutil show expanders": LEGACY_OUTPUTS["mprutil show expanders"]},
            "legacy table missing": {"mprutil show adapter": adapter_without_table},
            "legacy table malformed": {"mprutil show adapter": malformed_table},
            "legacy table repeats a PHY": {
                "mprutil show adapter": adapter_without_table + "0 0001 000a N 6.0 1.5 6.0 SAS Target\n" * 2
            },
            "numbered adapter missing": {"mprutil -u 0 show expanders": LEGACY_OUTPUTS["mprutil show expanders"]},
            "numbered table missing": {"mprutil -u 0 show adapter": adapter_without_table},
        }
        for name, outputs in cases.items():
            with self.subTest(name):
                fabric = _core_fabric(outputs)
                [node] = [node for node in fabric.nodes if node.kind == "controller"]
                self.assertEqual((node.metrics["phy_count"], node.metrics["linked_phy_count"]), (None, None))

    def test_expander_without_phy_rows_has_unknown_linked_count(self) -> None:
        listed = LEGACY_OUTPUTS["mprutil show expanders"] + "  0 1 000a 6.0 1.5 6.0 SAS Target\n"
        for name, text, expected in (
            ("rows missing", LEGACY_OUTPUTS["mprutil show expanders"], None),
            ("rows listed", listed, 1),
        ):
            with self.subTest(name):
                fabric = _core_fabric({**LEGACY_OUTPUTS, "mprutil show expanders": text}, selected=SELECTED_ENCLOSURE)
                [node] = [node for node in fabric.nodes if node.kind == "expander"]
                self.assertEqual(node.metrics["linked_phys"], expected)

    def test_reported_phy_table_counts_are_kept(self) -> None:
        table = LEGACY_OUTPUTS["mprutil show adapter"] + "0 0001 000a N 6.0 1.5 6.0 SAS Target\n1 0001 0000 N 6.0 1.5 6.0 No Device\n"
        for command in ("mprutil show adapter", "mprutil -u 0 show adapter"):
            with self.subTest(command):
                fabric = _core_fabric({command: table})
                [node] = [node for node in fabric.nodes if node.kind == "controller"]
                self.assertEqual((node.metrics["phy_count"], node.metrics["linked_phy_count"]), (2, 1))
                self.assertEqual(len(node.raw["phy_rows"]), 2)

    def test_unrecognized_output_is_not_controller_evidence(self) -> None:
        cases = {
            "legacy": {"mprutil show adapter": "unrecognized synthetic text\n"},
            "numbered": {"mprutil -u 0 show adapter": "unrecognized synthetic text\n"},
            "numbered empty": {"mprutil -u 0 show adapter": "", "mprutil -u 0 show devices": ""},
        }
        for name, outputs in cases.items():
            with self.subTest(name):
                fabric = _core_fabric(outputs, slots=[])
                self.assertFalse(fabric.available)
                self.assertEqual(fabric.controllers, [])
                self.assertEqual([node.id for node in fabric.nodes if node.kind == "controller"], [])
                if name == "numbered empty":
                    self.assertEqual(fabric.warnings, [NO_CORE_DATA_WARNING])
                else:
                    self.assertEqual(fabric.warnings, [UNREADABLE_MPRUTIL_WARNING])

    def test_empty_numbered_result_does_not_suppress_unnumbered_details(self) -> None:
        outputs = {"mprutil -u 0 show adapter": "", "mprutil -u 0 show expanders": "", **LEGACY_OUTPUTS}

        fabric = _core_fabric(outputs, selected=SELECTED_ENCLOSURE)

        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr"])
        self.assertEqual(fabric.controllers[0]["board"], "Synthetic HBA")
        self.assertEqual(fabric.controllers[0]["firmware"], "1.2.3")
        # Unit 0 answered with nothing: it was attempted and not read, so the
        # view says so instead of treating the unnumbered read as the whole.
        # (The expander header reports one PHY and lists none: not read whole.)
        self.assertEqual(
            fabric.warnings,
            [
                "CORE controller data could not be read for mpr0, so controller, expander and enclosure counts may be incomplete.",
                "CORE PHY tables could not be read whole for 1 expander, so their linked PHY counts were not read.",
            ],
        )
        self.assertEqual(
            fabric.raw["controller_read"]["units"],
            [{"controller": "mpr", "result": "read"}, {"controller": "mpr0", "result": "no_output"}],
        )
        self.assert_references_resolve(fabric)

    def test_numbered_unit_listed_by_adapter_summary_is_kept_without_detail(self) -> None:
        # Control: the adapter summary is an observation of the unit.
        fabric = _core_fabric({"mprutil show adapters": "/dev/mpr0 SAS3008 Synthetic HBA 1.2.3\n"}, slots=[])

        self.assertTrue(fabric.available)
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertEqual(fabric.controllers[0]["board"], "Synthetic HBA")
        self.assertNotIn(UNREADABLE_MPRUTIL_WARNING, fabric.warnings)

    def test_numbered_adapter_output_still_creates_its_controller(self) -> None:
        # Control: observed numbered output is controller evidence.
        fabric = _core_fabric({"mprutil -u 0 show adapter": LEGACY_OUTPUTS["mprutil show adapter"]})

        self.assertTrue(fabric.available)
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        controller = fabric.controllers[0]
        self.assertEqual(controller["unit"], 0)
        self.assertEqual(controller["board"], "Synthetic HBA")
        self.assertEqual(controller["firmware"], "1.2.3")
        self.assertNotIn(NO_CORE_DATA_WARNING, fabric.warnings)
        self.assert_references_resolve(fabric)

    def test_unnumbered_output_keeps_adapter_details_under_one_controller_id(self) -> None:
        fabric = _core_fabric(dict(LEGACY_OUTPUTS), selected=SELECTED_ENCLOSURE)

        self.assertTrue(fabric.available)
        self.assertEqual(len(fabric.controllers), 1)
        controller = fabric.controllers[0]
        self.assertEqual(controller["board"], "Synthetic HBA")
        self.assertEqual(controller["firmware"], "1.2.3")
        # No unit number was observed, so none is reported or implied.
        self.assertIsNone(controller["unit"])
        self.assertIsNone(controller["device"])
        nodes = {node.id: node for node in fabric.nodes}
        controller_node = nodes[controller["id"]]
        self.assertIn("mprutil show adapter", controller_node.evidence)
        self.assertEqual(controller_node.metrics["firmware"], "1.2.3")

        expanders = [node for node in fabric.nodes if node.kind == "expander"]
        self.assertEqual(len(expanders), 1)
        self.assertEqual(expanders[0].raw["controller_id"], controller["id"])
        self.assertIn(
            (controller["id"], expanders[0].id, "controller-expander"),
            {(link.source, link.target, link.kind) for link in fabric.links},
        )
        self.assertNotIn("mpr-1", fabric.model_dump_json())
        self.assert_references_resolve(fabric)

    def test_no_controller_data_counts_are_not_read(self) -> None:
        for name, outputs in {"no output": {}, "blank output": {"mprutil -u 0 show adapter": " \n"}}.items():
            with self.subTest(name):
                fabric = _core_fabric(outputs, slots=[_inventory_slot()])

                read = fabric.raw["controller_read"]
                self.assertEqual(read["counts"], {count: "not_read" for count in ALL_COUNTS})
                self.assertEqual(fabric.warnings, [NO_CORE_DATA_WARNING])

    def test_readable_and_unreadable_units_make_the_view_partial(self) -> None:
        failures = _failure_records("sudo -n /usr/sbin/mprutil -u 1 show adapter")
        cases = {
            "unreadable text": ({"mprutil -u 1 show adapter": "unrecognized synthetic text\n"}, None, "unreadable"),
            "no output": ({"mprutil -u 1 show adapter": ""}, None, "no_output"),
            "command failed": ({}, failures, "command_failed"),
        }
        for name, (extra, failures, result) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric({**NUMBERED_UNIT_OUTPUTS, **extra}, slots=[_inventory_slot()], command_failures=failures)

                # The readable unit is kept; the unread one is named, not dropped.
                self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
                self.assertEqual(fabric.controllers[0]["board"], "Synthetic HBA")
                self.assertEqual(
                    fabric.raw["controller_read"]["units"],
                    [{"controller": "mpr0", "result": "read"}, {"controller": "mpr1", "result": result}],
                )
                self.assertEqual(fabric.warnings, [UNREADABLE_UNIT_WARNING])
                self.assertEqual(
                    fabric.raw["controller_read"]["counts"],
                    {"controllers": "partial", "paths": "complete", "expanders": "partial", "enclosures": "partial"},
                )

    def test_whole_read_of_every_unit_is_complete(self) -> None:
        # Control: every attempted unit read with its lists; known-empty lists are real zeroes.
        fabric = _core_fabric(dict(NUMBERED_UNIT_OUTPUTS), slots=[_inventory_slot()])

        self.assertEqual(fabric.warnings, [])
        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(fabric.raw["controller_read"]["counts"], {count: "complete" for count in ALL_COUNTS})
        self.assertEqual((fabric.expanders, fabric.enclosures, fabric.paths), ([], [], []))

    def test_read_unit_without_its_lists_is_partial(self) -> None:
        fabric = _core_fabric({"mprutil -u 0 show adapter": LEGACY_OUTPUTS["mprutil show adapter"]})

        self.assertEqual(fabric.warnings, [LISTS_MISSING_WARNING])
        self.assertEqual(
            fabric.raw["controller_read"]["counts"],
            {"controllers": "complete", "paths": "complete", "expanders": "partial", "enclosures": "partial"},
        )

    def test_multipath_paths_without_mprutil_data_are_not_a_whole_read(self) -> None:
        slot = _inventory_slot().model_copy(update={"multipath": MultipathView(
            name="disk0",
            device_name="multipath/disk0",
            members=[MultipathMember(device_name="da0", state="ACTIVE", controller_label="mpr0")],
        )})

        fabric = _core_fabric({}, slots=[slot])

        # The inventory saw one path on mpr0, but no controller command was read.
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertEqual(len(fabric.paths), 1)
        self.assertEqual(fabric.warnings, [NO_MPRUTIL_DATA_WARNING])
        self.assertEqual(
            fabric.raw["controller_read"]["counts"],
            {"controllers": "partial", "paths": "complete", "expanders": "not_read", "enclosures": "not_read"},
        )

    def test_expander_phy_table_counts_need_the_whole_table(self) -> None:
        header = f"2 {EXPANDER_ADDRESS} 000a 0001 0016 1\n"
        phy0 = "  0 1 000b 6.0 1.5 6.0 SAS Target\n"
        phy1 = "  1 1.5 6.0 No Device\n"
        cases = {
            "fewer rows than reported PHYs": (header + phy0, None),
            "a rejected row": (header + phy0 + "  1 malformed\n", None),
            "a repeated PHY": (header + phy0 + phy0, None),
            "a PHY number out of range": (header + phy0 + "  2 1.5 6.0 No Device\n", None),
            "every reported PHY": (header + phy0 + phy1, 1),
            "one reported PHY with no device": (f"1 {EXPANDER_ADDRESS} 000a 0001 0016 1\n" + "  0 1.5 6.0 No Device\n", 0),
            "every reported PHY under a column heading": (
                header + "    Phy  RemotePhy  DevHandle  Speed  Min  Max  Device\n" + phy0 + phy1, 1,
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric({**LEGACY_OUTPUTS, "mprutil show expanders": text}, selected=SELECTED_ENCLOSURE)
                [node] = [node for node in fabric.nodes if node.kind == "expander"]
                self.assertEqual(node.metrics["linked_phys"], expected)
                [parsed] = parse_mpr_expanders(text)
                self.assertEqual(parsed["linked_phys"], expected)
                if expected is None:
                    self.assertIsNone(parsed["device_counts"])
                else:
                    self.assertEqual(sum(parsed["device_counts"].values()), len(parsed["phys"]))

    def test_adapter_phy_table_with_a_gap_stays_unknown(self) -> None:
        adapter = LEGACY_OUTPUTS["mprutil show adapter"]
        rows = {
            "gap": "0 0001 000a N 6.0 1.5 6.0 SAS Target\n2 0001 0000 N 6.0 1.5 6.0 No Device\n",
            "same PHY written twice": "0 0001 000a N 6.0 1.5 6.0 SAS Target\n00 0001 0000 N 6.0 1.5 6.0 No Device\n",
        }
        for name, table in rows.items():
            with self.subTest(name):
                detail = parse_mpr_adapter_detail(adapter + table)
                self.assertEqual((detail["phy_count"], detail["linked_phy_count"]), (None, None))


# Reply shapes as `mprutil` prints them (column headings, blank-padded empty
# columns), built from its printf formats rather than copied from a system.
DEVICE_ADDRESS = f"5{3:015x}"
EXPANDERS_HEADING = "NumPhys   SAS Address     DevHandle   Parent  EncHandle  SAS Level\n"
ENCLOSURES_HEADING = "Slots      Logical ID     SEPHandle  EncHandle    Type\n"
DEVICES_HEADING = "B____T    SAS Address      Handle  Parent    Device        Speed Enc  Slot  Wdt\n"
UNREADABLE_LISTS_WARNING = LISTS_MISSING_WARNING
PHY_TABLE_WARNING_PREFIX = "CORE PHY tables could not be read whole for "
# The selected enclosure on unit 0, so its expanders stay in the drawn scope.
SELECTED_UNIT_ENCLOSURE = {"mprutil -u 0 show enclosures": LEGACY_OUTPUTS["mprutil show enclosures"]}


def _adapter_phy_row(phy: int, *, attached: bool) -> str:
    # mprutil: "%-8d%-12s%-11s%-10s%-8s%-7s%-7s%s"; handles and speed are blank for an unattached PHY.
    handles = ("0001", "0009", "12") if attached else ("", "", "")
    return f"{phy:<8}{handles[0]:<12}{handles[1]:<11}{'N':<10}{handles[2]:<8}{'3.0':<7}{'12':<7}SAS Initiator\n"


def _printed_adapter(phys: list[bool]) -> str:
    rows = "".join(_adapter_phy_row(index, attached=attached) for index, attached in enumerate(phys))
    return (
        "mpr0 Adapter:\n"
        "       Board Name: Synthetic HBA\n"
        "   Board Assembly: \n"
        "Firmware Revision: 16.00.12.00\n"
        "      Temperature: Unknown/Unsupported\n"
        "\n"
        "PhyNum  CtlrHandle  DevHandle  Disabled  Speed   Min    Max    Device\n"
        f"{rows}\n"
    )


def _printed_expanders() -> str:
    return (
        EXPANDERS_HEADING
        + f"  02    {EXPANDER_ADDRESS}    000a       0001     0016       1\n\n"
        + "     Phy  RemotePhy  DevHandle  Speed  Min   Max    Device\n"
        + "     00      01        000b      12    1.5   12    SAS Target    \n"
        + "     01                          1.5   12    No Device     \n\n"
    )


def _printed_device_row(enclosure_handle: str = "0010") -> str:
    return f"00   10    {DEVICE_ADDRESS} 000b    000a      SAS Target    12    {enclosure_handle} 00    1\n"


BLANK_HANDLE = "    "


def _printed_expanders_with_handles(parent: str, enclosure: str) -> str:
    # mprutil: "  %02d    %08x%08x    %04x       %s     %s       %d"; a zero parent
    # or enclosure handle is printed as four blanks.
    return _printed_expanders().replace("000a       0001     0016       1", f"000a       {parent}     {enclosure}       1")


def _printed_enclosure(sep_handle: str = "000b", kind: str = "External SES-2") -> str:
    # mprutil: "  %.2d    %08x%08x    %s       %04x     %s"; the SEP handle is four
    # blanks for an enclosure that is not an SES device.
    return ENCLOSURES_HEADING + f"  01    {SELECTED_ENCLOSURE}    {sep_handle}       0010     {kind}\n\n"


def _adapter_with_discovery(phys: int, discovering: set[int]) -> str:
    # mprutil prints "Discovery still in progress" in place of a PHY's row and goes on.
    rows = "".join(
        "Discovery still in progress\n" if phy in discovering else _adapter_phy_row(phy, attached=True)
        for phy in range(phys)
    )
    return _printed_adapter([]).replace("Device\n\n", f"Device\n{rows}\n")


def _healthy_unit(unit: int = 0) -> dict[str, str]:
    """Every supported list of one unit, as mprutil prints it on a healthy system."""
    return {
        f"mprutil -u {unit} show adapter": _printed_adapter([True, False]).replace("mpr0 Adapter:", f"mpr{unit} Adapter:"),
        f"mprutil -u {unit} show devices": DEVICES_HEADING + _printed_device_row() + "\n",
        f"mprutil -u {unit} show enclosures": _printed_enclosure(),
        f"mprutil -u {unit} show expanders": _printed_expanders(),
    }


def _show_all(unit: int = 0) -> str:
    # mprutil `show all` prints each list under its section label.
    replies = _healthy_unit(unit)
    return "".join(
        f"{label}:\n{replies[f'mprutil -u {unit} show {subcommand}']}"
        for label, subcommand in (("Adapter", "adapter"), ("Devices", "devices"), ("Enclosures", "enclosures"), ("Expanders", "expanders"))
    )


def _two_unit_summary() -> str:
    return "Device Name\t      Chip Name        Board Name        Firmware\n" + "".join(
        f"/dev/mpr{unit}\t{'SAS3008':>16} {'Synthetic HBA':>16}        10000c00\n" for unit in (0, 1)
    )


def _healthy_reply_cases() -> dict[str, dict[str, str]]:
    """Complete, successful replies in shapes mprutil prints: each must read as OK."""
    unit = _healthy_unit()
    cases = {
        "healthy_lists": dict(unit),
        "blank_expander_parent": {**unit, "mprutil -u 0 show expanders": _printed_expanders_with_handles(BLANK_HANDLE, "0016")},
        "blank_expander_enclosure": {**unit, "mprutil -u 0 show expanders": _printed_expanders_with_handles("0001", BLANK_HANDLE)},
        "blank_expander_handles": {**unit, "mprutil -u 0 show expanders": _printed_expanders_with_handles(BLANK_HANDLE, BLANK_HANDLE)},
        "blank_sep_unknown_enclosure": {**unit, "mprutil -u 0 show enclosures": _printed_enclosure(BLANK_HANDLE, "Unknown")},
        "show_all_only": {"mprutil -u 0 show all": _show_all()},
        "show_all_second_unit": {"mprutil show adapters": _two_unit_summary(), **unit, "mprutil -u 1 show all": _show_all(1)},
    }
    for position, discovering in {"first": {0}, "interior": {1}, "last": {2}, "every": {0, 1, 2}}.items():
        cases[f"discovery_{position}"] = {**unit, "mprutil -u 0 show adapter": _adapter_with_discovery(3, discovering)}
    return cases


def _unread_reply_cases() -> dict[str, dict[str, str]]:
    """Controls: a reply that parses to nothing, or no reply text, is still not read."""
    return {
        "show_all_unparseable": {"mprutil -u 0 show all": "unrecognized synthetic text\n"},
        "show_all_empty": {"mprutil -u 0 show all": ""},
    }


class CoreFabricReplyReadStateTests(unittest.TestCase):
    """A count or list is a measurement only when the reply carrying it was read and understood."""

    def test_unparseable_list_replies_are_not_read(self) -> None:
        unreadable = "unrecognized synthetic text\n"
        fabric = _core_fabric(
            {**NUMBERED_UNIT_OUTPUTS, "mprutil -u 0 show expanders": unreadable, "mprutil -u 0 show enclosures": unreadable},
            slots=[_inventory_slot()],
        )

        read = fabric.raw["controller_read"]
        self.assertEqual(read["counts"]["expanders"], "partial")
        self.assertEqual(read["counts"]["enclosures"], "partial")
        self.assertEqual(read["lists"]["mpr0"]["expanders"], "unreadable")
        self.assertEqual(read["lists"]["mpr0"]["enclosures"], "unreadable")
        self.assertEqual(fabric.warnings, [UNREADABLE_LISTS_WARNING])

    def test_list_replies_as_printed_are_read_including_heading_only_zeroes(self) -> None:
        # Control: mprutil prints a column heading even when a list is empty;
        # that is a measured zero, and a printed list is read.
        cases = {
            "heading only": ({"mprutil -u 0 show expanders": EXPANDERS_HEADING + "\n", "mprutil -u 0 show enclosures": ENCLOSURES_HEADING + "\n"}, "empty"),
            "printed rows": ({"mprutil -u 0 show expanders": _printed_expanders(), "mprutil -u 0 show enclosures": ENCLOSURES_HEADING + f"  01    {SELECTED_ENCLOSURE}    000b       0010     External SES-2\n\n"}, "read"),
        }
        for name, (lists, state) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric({**NUMBERED_UNIT_OUTPUTS, **lists}, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
                read = fabric.raw["controller_read"]
                self.assertEqual(fabric.warnings, [])
                self.assertEqual(read["counts"], {count: "complete" for count in ALL_COUNTS})
                self.assertEqual((read["lists"]["mpr0"]["expanders"], read["lists"]["mpr0"]["enclosures"]), (state, state))

    def test_enclosure_device_count_comes_only_from_a_read_device_list(self) -> None:
        unread = "unrecognized synthetic text\n"
        cases = {
            "device list not run": (None, None),
            "device list unreadable": (unread, None),
            "device list printed empty": (DEVICES_HEADING + "\n", 0),
            "device list blank": ("", 0),
            "device list with a row": (DEVICES_HEADING + _printed_device_row() + "\n", 1),
        }
        for name, (devices, expected) in cases.items():
            with self.subTest(name):
                outputs = dict(LEGACY_OUTPUTS)
                if devices is not None:
                    outputs["mprutil show devices"] = devices
                fabric = _core_fabric(outputs, selected=SELECTED_ENCLOSURE)
                [enclosure] = [node for node in fabric.nodes if node.kind == "mpr-enclosure"]
                self.assertEqual(enclosure.metrics["device_count"], expected)
                self.assertEqual(enclosure.raw["device_count"], expected)
                self.assertEqual(enclosure.metrics.get("not_read", []), ["device_count"] if expected is None else [])

    def test_show_all_unit_commands_count_as_attempted(self) -> None:
        failures = _failure_records("sudo -n /usr/sbin/mprutil -u 1 show all")
        cases = {
            "answered": ({"mprutil -u 1 show all": "unrecognized synthetic text\n"}, None, "unreadable"),
            "empty": ({"mprutil -u 1 show all": ""}, None, "no_output"),
            "failed": ({}, failures, "command_failed"),
        }
        for name, (extra, failures, result) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric({**NUMBERED_UNIT_OUTPUTS, **extra}, slots=[_inventory_slot()], command_failures=failures)

                self.assertEqual(
                    fabric.raw["controller_read"]["units"],
                    [{"controller": "mpr0", "result": "read"}, {"controller": "mpr1", "result": result}],
                )
                # Attempted, not invented: no controller node comes from a command name.
                self.assertEqual([node.id for node in fabric.nodes if node.kind == "controller"], ["controller:mpr0"])
                self.assertEqual(fabric.warnings, [UNREADABLE_UNIT_WARNING])
                self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "partial")

    def test_every_failed_unit_read_is_counted_from_the_record_the_inventory_builds(self) -> None:
        # One failed read per unit subcommand: each leaves its controller attempted and unread.
        for subcommand in ("adapter", "all", "devices", "enclosures", "expanders", "iocfacts"):
            with self.subTest(subcommand):
                failures = _failure_records(f"sudo -n /usr/sbin/mprutil -u 1 show {subcommand}")
                fabric = _core_fabric(NUMBERED_UNIT_OUTPUTS, slots=[_inventory_slot()], command_failures=failures)

                self.assertEqual(
                    fabric.raw["controller_read"]["units"],
                    [{"controller": "mpr0", "result": "read"}, {"controller": "mpr1", "result": "command_failed"}],
                )
                self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "partial")
                self.assertEqual(fabric.warnings, [UNREADABLE_UNIT_WARNING])

    def test_failed_unnumbered_read_counts_only_when_no_numbered_unit_was_read(self) -> None:
        failures = _failure_records("sudo -n /usr/sbin/mprutil show adapter")
        fabric = _core_fabric({}, slots=[_inventory_slot()], command_failures=failures)
        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr", "result": "command_failed"}])
        self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "not_read")

        fabric = _core_fabric(NUMBERED_UNIT_OUTPUTS, slots=[_inventory_slot()], command_failures=failures)
        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "complete")

        # Control: the unnumbered context names only the unnumbered controller.
        other = [{"context": "sas_fabric_mprutil", "controller": "mpr1"}, {"context": "sas_fabric_mprutil"}]
        fabric = _core_fabric({}, slots=[_inventory_slot()], command_failures=other)
        self.assertEqual(fabric.raw["controller_read"]["units"], [])

    def test_failed_read_of_a_unit_number_that_is_not_admitted_is_counted_as_rejected(self) -> None:
        failures = _failure_records("sudo -n /usr/sbin/mprutil -u 12345 show adapter")
        fabric = _core_fabric(NUMBERED_UNIT_OUTPUTS, slots=[_inventory_slot()], command_failures=failures)

        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 1)
        self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "partial")

    def test_other_failed_commands_do_not_name_a_controller(self) -> None:
        # Control: a failure that is not a unit read leaves a whole read complete.
        failures = _failure_records(
            "sudo -n /usr/sbin/mprutil show adapters",
            "sudo -n /usr/sbin/mprutil show anything-else",
            "sudo -n /usr/sbin/mprutil -u 1 show anything",
            "sudo -n /sbin/camcontrol devlist -v",
        )
        fabric = _core_fabric(NUMBERED_UNIT_OUTPUTS, slots=[_inventory_slot()], command_failures=failures)

        self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr0", "result": "read"}])
        self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 0)
        self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "complete")

        # A record must carry both the unit-read context and its controller to name one.
        for record in (
            {"context": "sas_fabric_mprutil_adapter"},
            {"controller": "mpr1"},
            {"context": "sas_fabric_expander_phy_error_counters", "controller": "mpr1"},
            {"context": "sas_fabric_mprutil", "controller": "mpr1"},
            {"context": "sas_fabric_mprutil_adapter", "controller": "mpr"},
            {"context": "sas_fabric_mprutil_adapter", "controller": "mpr1\n"},
            {"context": None, "controller": None},
        ):
            with self.subTest(record=record):
                fabric = _core_fabric(NUMBERED_UNIT_OUTPUTS, slots=[_inventory_slot()], command_failures=[record])
                self.assertEqual(fabric.raw["controller_read"]["units"], [{"controller": "mpr0", "result": "read"}])
                self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 0)

    def test_unit_numbers_must_be_one_to_four_ascii_digits(self) -> None:
        board = " SAS3008 Synthetic HBA 1.2.3\n"
        rejected = {
            "2000 digits": "/dev/mpr" + "9" * 2000 + board,
            "five digits": "/dev/mpr12345" + board,
            "non-ASCII digit": "/dev/mpr\u0663" + board,
        }
        for name, summary in rejected.items():
            with self.subTest(name):
                fabric = _core_fabric({"mprutil show adapters": summary}, slots=[_inventory_slot()])

                self.assertEqual(fabric.controllers, [])
                self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 1)
                self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "not_read")
                self.assertTrue(all(len(warning) < 300 for warning in fabric.warnings), fabric.warnings)
                self.assertFalse(any("9999" in warning or "\u0663" in warning for warning in fabric.warnings))
                self.assertIn(
                    "CORE mprutil listed 1 controller unit that is not a unit number, so it was not read.",
                    fabric.warnings,
                )
                self.assertEqual(build_core_mprutil_unit_commands(summary), [])
        fabric = _core_fabric({**NUMBERED_UNIT_OUTPUTS, "mprutil -u 12345 show adapter": LEGACY_OUTPUTS["mprutil show adapter"]})
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 1)
        self.assertEqual(fabric.raw["controller_read"]["counts"]["controllers"], "partial")

    def test_four_digit_unit_number_is_admitted(self) -> None:
        # Control: the bound sits far above real unit numbers and admits them.
        summary = "/dev/mpr9999 SAS3008 Synthetic HBA 1.2.3\n"
        fabric = _core_fabric({"mprutil show adapters": summary}, slots=[])

        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr9999"])
        self.assertEqual(fabric.raw["controller_read"]["rejected_units"], 0)
        self.assertIn("sudo -n /usr/sbin/mprutil -u 9999 show adapter", build_core_mprutil_unit_commands(summary))

    def test_zero_phy_expander_with_no_rows_is_a_measured_zero(self) -> None:
        [parsed] = parse_mpr_expanders(f"0 {EXPANDER_ADDRESS} 000a 0001 0016 1\n")
        self.assertEqual((parsed["linked_phys"], parsed["device_counts"]), (0, {}))
        # A positive header without its rows stays unknown.
        [parsed] = parse_mpr_expanders(f"1 {EXPANDER_ADDRESS} 000a 0001 0016 1\n")
        self.assertEqual((parsed["linked_phys"], parsed["device_counts"]), (None, None))

    def test_rejected_phy_tables_are_not_read_and_make_the_view_partial(self) -> None:
        adapter = LEGACY_OUTPUTS["mprutil show adapter"]
        cases = {
            "controller table": ({**NUMBERED_UNIT_OUTPUTS, "mprutil -u 0 show adapter": adapter + "0 malformed\n"}, "controller", ["phy_count", "linked_phy_count"], "1 controller"),
            "expander table": ({**NUMBERED_UNIT_OUTPUTS, **SELECTED_UNIT_ENCLOSURE, "mprutil -u 0 show expanders": f"2 {EXPANDER_ADDRESS} 000a 0001 0016 1\n  0 1 000b 6.0 1.5 6.0 SAS Target\n"}, "expander", ["linked_phys"], "1 expander"),
        }
        for name, (outputs, kind, keys, noun) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
                [node] = [node for node in fabric.nodes if node.kind == kind]
                self.assertEqual(node.metrics["not_read"], keys)
                self.assertEqual(fabric.raw["controller_read"]["unread_phy_tables"], 1)
                self.assertEqual(
                    fabric.warnings,
                    [f"{PHY_TABLE_WARNING_PREFIX}{noun}, so their linked PHY counts were not read."],
                )

    def test_whole_or_absent_phy_tables_stay_ok(self) -> None:
        # Controls: a table as mprutil prints it (unattached PHYs have blank
        # columns), one with no linked PHY, and an adapter reply without a PHY
        # table (no counts, as before) raise no notice.
        cases = {
            "printed table": (_printed_adapter([True, True, False, False]), (4, 2)),
            "no table": (LEGACY_OUTPUTS["mprutil show adapter"], (None, None)),
            "printed table, nothing linked": (_printed_adapter([False]), (1, 0)),
        }
        for name, (adapter, counts) in cases.items():
            with self.subTest(name):
                fabric = _core_fabric({**NUMBERED_UNIT_OUTPUTS, "mprutil -u 0 show adapter": adapter}, slots=[_inventory_slot()])
                [node] = [node for node in fabric.nodes if node.kind == "controller"]
                self.assertEqual((node.metrics["phy_count"], node.metrics["linked_phy_count"]), counts)
                self.assertNotIn("not_read", node.metrics)
                self.assertEqual(fabric.warnings, [])
                self.assertEqual(fabric.raw["controller_read"]["unread_phy_tables"], 0)
        fabric = _core_fabric(
            {**NUMBERED_UNIT_OUTPUTS, **SELECTED_UNIT_ENCLOSURE, "mprutil -u 0 show expanders": _printed_expanders()},
            slots=[_inventory_slot()],
            selected=SELECTED_ENCLOSURE,
        )
        [expander] = [node for node in fabric.nodes if node.kind == "expander"]
        self.assertEqual(expander.metrics["linked_phys"], 1)
        self.assertNotIn("not_read", expander.metrics)
        self.assertEqual(fabric.warnings, [])


class CoreFabricHealthyReplyShapeTests(unittest.TestCase):
    """A complete, successful reply in a shape mprutil prints adds no warning and lowers no status."""

    def healthy(self, name: str):
        fabric = _core_fabric(_healthy_reply_cases()[name], slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        read = fabric.raw["controller_read"]
        self.assertEqual(fabric.warnings, [])
        self.assertEqual(read["counts"], {count: "complete" for count in ALL_COUNTS})
        self.assertTrue(all(item["result"] == "read" for item in read["units"]), read["units"])
        self.assertEqual(read["unread_phy_tables"], 0)
        return fabric

    @staticmethod
    def kind_metrics(fabric, kind: str) -> list[dict]:
        return [node.metrics for node in fabric.nodes if node.kind == kind]

    def test_blank_expander_handles_as_printed_are_read(self) -> None:
        for name, (parent, enclosure) in {
            "blank_expander_parent": (None, "0010"),
            "blank_expander_enclosure": ("0001", None),
            "blank_expander_handles": (None, None),
        }.items():
            with self.subTest(name):
                fabric = self.healthy(name)
                self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["expanders"], "read")
                [parsed] = parse_mpr_expanders(_healthy_reply_cases()[name]["mprutil -u 0 show expanders"])
                self.assertEqual((parsed["parent"], parsed["enc_handle"], parsed["linked_phys"]), (parent, enclosure, 1))

    def test_blank_sep_handle_enclosure_as_printed_is_read(self) -> None:
        fabric = self.healthy("blank_sep_unknown_enclosure")
        self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr0"]["enclosures"], "read")
        self.assertEqual([metrics["device_count"] for metrics in self.kind_metrics(fabric, "mpr-enclosure")], [1])
        [parsed] = parse_mpr_enclosures(_printed_enclosure(BLANK_HANDLE, "Unknown"))
        self.assertEqual((parsed["sep_handle"], parsed["enc_handle"], parsed["type"]), (None, "0010", "Unknown"))
        # Control: an SES enclosure keeps both handles.
        [parsed] = parse_mpr_enclosures(_printed_enclosure())
        self.assertEqual((parsed["sep_handle"], parsed["enc_handle"], parsed["type"]), ("000b", "0010", "External SES-2"))

    def test_show_all_alone_reads_its_sections(self) -> None:
        fabric = self.healthy("show_all_only")
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0"])
        self.assertEqual(fabric.controllers[0]["board"], "Synthetic HBA")
        self.assertEqual(fabric.raw["controller_read"]["lists"], {"mpr0": {"expanders": "read", "enclosures": "read", "devices": "read"}})
        self.assertEqual((len(fabric.expanders), len(fabric.enclosures)), (1, 1))
        [controller] = self.kind_metrics(fabric, "controller")
        self.assertEqual((controller["phy_count"], controller["linked_phy_count"]), (2, 1))
        self.assertEqual([metrics["device_count"] for metrics in self.kind_metrics(fabric, "mpr-enclosure")], [1])

    def test_second_listed_controller_supplied_only_by_show_all(self) -> None:
        fabric = self.healthy("show_all_second_unit")
        self.assertEqual([controller["id"] for controller in fabric.controllers], ["controller:mpr0", "controller:mpr1"])
        self.assertEqual(fabric.raw["controller_read"]["lists"]["mpr1"], {"expanders": "read", "enclosures": "read", "devices": "read"})
        self.assertEqual(
            [(metrics["phy_count"], metrics["linked_phy_count"]) for metrics in self.kind_metrics(fabric, "controller")],
            [(2, 1), (2, 1)],
        )

    def test_dedicated_reply_wins_over_the_show_all_section(self) -> None:
        outputs = {"mprutil -u 0 show all": _show_all(), "mprutil -u 0 show expanders": EXPANDERS_HEADING + "\n"}
        fabric = _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        self.assertEqual(fabric.raw["controller_read"]["lists"].get("mpr0", {}).get("expanders"), "empty")
        self.assertEqual(fabric.expanders, [])
        self.assertEqual(len(fabric.enclosures), 1)
        self.assertEqual(fabric.warnings, [])

    def test_show_all_reads_only_its_four_section_labels(self) -> None:
        other_device = f"00   11    5{4:015x} 000c    000a      SAS Target    12    0010 01    1\n"
        sections = _show_all().split("Enclosures:\n")
        outputs = {
            "mprutil -u 0 show all": (
                sections[0] + "Other:\n" + DEVICES_HEADING + other_device + "\nEnclosures:\n" + sections[1]
                + "Summary:\nunrecognized synthetic text\n"
            ),
        }
        fabric = _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        # Text under another label is ignored: no second device, no unread expander table.
        self.assertEqual([metrics["device_count"] for metrics in self.kind_metrics(fabric, "mpr-enclosure")], [1])
        [expander] = self.kind_metrics(fabric, "expander")
        self.assertEqual(expander["linked_phys"], 1)
        self.assertEqual(fabric.warnings, [])

    def test_show_all_label_with_nothing_under_it_is_not_read(self) -> None:
        # mprutil prints each list's column heading when the list was read, so
        # a label with nothing under it is a list that was not read, not zero.
        devices = DEVICES_HEADING + _printed_device_row() + "\n"
        outputs = {"mprutil -u 0 show all": _show_all().replace("Devices:\n" + devices, "Devices:\n\n")}
        fabric = _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        self.assertEqual(fabric.raw["controller_read"]["lists"].get("mpr0", {}).get("devices"), "not_read")
        [enclosure] = self.kind_metrics(fabric, "mpr-enclosure")
        self.assertEqual((enclosure["device_count"], enclosure["not_read"]), (None, ["device_count"]))

    def test_discovery_line_in_place_of_a_phy_row_is_recognised(self) -> None:
        # What main shows: the printed rows are counted, no PHY is invented for
        # the missing row, and no warning or lower status is added.
        for position, counts in {"first": (2, 2), "interior": (2, 2), "last": (2, 2), "every": (0, 0)}.items():
            with self.subTest(position):
                fabric = self.healthy(f"discovery_{position}")
                [controller] = self.kind_metrics(fabric, "controller")
                self.assertEqual((controller["phy_count"], controller["linked_phy_count"]), counts)
                self.assertNotIn("not_read", controller)
        # Control: a gap with no discovery line is still not read whole.
        detail = parse_mpr_adapter_detail(_adapter_with_discovery(3, {1}).replace("Discovery still in progress\n", ""))
        self.assertEqual((detail["phy_count"], detail["phy_table"]), (None, "rejected"))

    def test_unparseable_or_empty_replies_are_still_not_read(self) -> None:
        for name, (result, warning) in {
            "show_all_unparseable": ("unreadable", UNREADABLE_MPRUTIL_WARNING),
            "show_all_empty": ("no_output", NO_CORE_DATA_WARNING),
        }.items():
            with self.subTest(name):
                fabric = _core_fabric(_unread_reply_cases()[name], slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
                read = fabric.raw["controller_read"]
                self.assertEqual(fabric.controllers, [])
                self.assertEqual(read["units"], [{"controller": "mpr0", "result": result}])
                self.assertEqual(read["counts"], {count: "not_read" for count in ALL_COUNTS})
                self.assertEqual(fabric.warnings, [warning])


PHY_TABLE_WARNING = "CORE PHY tables could not be read whole for 1 controller, so their linked PHY counts were not read."
UTILITY_ERROR = "mprutil: Error retrieving enclosure page: Input/output error\n"


class CoreFabricShapeAdmissionTests(unittest.TestCase):
    """A reply shape is admitted only by its own structure; anything else leaves that list or table not read."""

    def partial(self, outputs: dict[str, str], warning: str):
        fabric = _core_fabric(outputs, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        self.assertIn(warning, fabric.warnings)
        return fabric

    def test_enclosure_row_that_lost_its_type_is_not_read(self) -> None:
        truncated = _printed_enclosure().replace("0010     External SES-2", "0010")
        # A one-handle row is read only at mprutil's printed columns.
        for text in (truncated, f"01 {SELECTED_ENCLOSURE} 0010 Unknown\n", _printed_enclosure(BLANK_HANDLE, "")):
            with self.subTest(text):
                self.assertEqual(parse_mpr_enclosures(text), [])
        for name, outputs in {
            "dedicated": {**_healthy_unit(), "mprutil -u 0 show enclosures": truncated},
            "show all": {"mprutil -u 0 show all": _show_all().replace(_printed_enclosure(), truncated)},
        }.items():
            with self.subTest(name):
                fabric = self.partial(outputs, LISTS_MISSING_WARNING)
                read = fabric.raw["controller_read"]
                self.assertEqual(read["lists"]["mpr0"]["enclosures"], "unreadable")
                self.assertEqual(read["counts"]["enclosures"], "partial")
                # No enclosure appears that the reply did not print.
                self.assertEqual([node.id for node in fabric.nodes if node.kind == "mpr-enclosure"], [])
        # Controls: every blank-SEP type mprutil prints still reads, with its own handle and type.
        for kind in ("Unknown", "Direct Attached SGPIO", "Expander SGPIO", "Direct Attached GPIO"):
            with self.subTest(kind):
                [parsed] = parse_mpr_enclosures(_printed_enclosure(BLANK_HANDLE, kind))
                self.assertEqual((parsed["sep_handle"], parsed["enc_handle"], parsed["type"]), (None, "0010", kind))

    def test_utility_error_in_a_show_all_section_leaves_it_not_read(self) -> None:
        with self.subTest("enclosures"):
            enclosure_failed = _show_all().replace("Expanders:\n", UTILITY_ERROR + "Expanders:\n")
            fabric = self.partial({"mprutil -u 0 show all": enclosure_failed}, LISTS_MISSING_WARNING)
            read = fabric.raw["controller_read"]
            self.assertEqual(read["lists"]["mpr0"]["enclosures"], "not_read")
            self.assertEqual(read["counts"]["enclosures"], "partial")
            self.assertEqual(read["lists"]["mpr0"]["expanders"], "read")
        with self.subTest("adapter"):
            # An error after the PHY rows leaves the adapter's PHY table not read.
            adapter_failed = _show_all().replace("Devices:\n", "mprutil: Error retrieving SAS IO Unit page 0\nDevices:\n", 1)
            fabric = self.partial({"mprutil -u 0 show all": adapter_failed}, PHY_TABLE_WARNING)
            [controller] = [node.metrics for node in fabric.nodes if node.kind == "controller"]
            self.assertEqual((controller["phy_count"], controller.get("not_read")), (None, ["phy_count", "linked_phy_count"]))
        # Control: an unmodelled line in a section keeps its current treatment.
        noted = _show_all().replace("Expanders:\n", "synthetic notice text\nExpanders:\n")
        fabric = _core_fabric({"mprutil -u 0 show all": noted}, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
        self.assertEqual((fabric.warnings, fabric.raw["controller_read"]["lists"]["mpr0"]["enclosures"]), ([], "read"))

    def test_any_column_zero_label_ends_the_previous_section(self) -> None:
        other_device = f"00   11    5{4:015x} 000c    000a      SAS Target    12    0010 01    1\n"
        for label in ("Other:", "Other1:", "Other_Section:", "other:", "Other Section:"):
            with self.subTest(label):
                text = _show_all().replace("Enclosures:\n", f"{label}\n{DEVICES_HEADING}{other_device}\nEnclosures:\n")
                fabric = _core_fabric({"mprutil -u 0 show all": text}, slots=[_inventory_slot()], selected=SELECTED_ENCLOSURE)
                # The unknown label's row is never counted.
                self.assertEqual([n.metrics["device_count"] for n in fabric.nodes if n.kind == "mpr-enclosure"], [1])
                self.assertEqual(fabric.warnings, [])

    def test_discovery_line_stands_for_the_phy_at_its_own_position(self) -> None:
        rows = _printed_adapter([]).replace("Device\n\n", "Device\n{rows}\n")
        for name, table in {
            "gap before a trailing line": _adapter_phy_row(0, attached=True) + _adapter_phy_row(2, attached=True)
            + "Discovery still in progress\n",
            "line before the PHY it displaced": "Discovery still in progress\n" + _adapter_phy_row(0, attached=True)
            + _adapter_phy_row(1, attached=True),
        }.items():
            with self.subTest(name):
                detail = parse_mpr_adapter_detail(rows.format(rows=table))
                self.assertEqual((detail["phy_count"], detail["phy_table"]), (None, "rejected"))
                self.partial({**_healthy_unit(), "mprutil -u 0 show adapter": rows.format(rows=table)}, PHY_TABLE_WARNING)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--write-render-fixture"]:
        RENDER_FIXTURE.write_text(json.dumps(_render_cases(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        unittest.main()
