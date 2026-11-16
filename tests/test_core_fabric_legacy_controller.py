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
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.sas_fabric_parsers import parse_mpr_adapter_detail, parse_mpr_expanders

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
    "CORE controller mpr0 did not return an expander or enclosure list, so those counts may be incomplete."
)
ALL_COUNTS = ("controllers", "paths", "expanders", "enclosures")

LEGACY_OUTPUTS = {
    "mprutil show adapter": (
        "Synthetic Adapter:\n"
        "Board Name: Synthetic HBA\n"
        "Firmware Revision: 1.2.3\n"
    ),
    "mprutil show expanders": f"1 {EXPANDER_ADDRESS} 000a 0001 0010 1\n",
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


def _render_case_inputs() -> dict[str, tuple[dict[str, str], list[SlotView]]]:
    """Builder inputs for the payloads the Node page test draws with the real page code."""
    second_physical = _inventory_slot().model_copy(update={"slot": 1, "slot_label": "01", "column_index": 1})
    return {
        "no_controller_bays": ({}, [_inventory_slot()]),
        "no_controller_virtual": ({}, [_virtual_slot()]),
        "no_controller_mixed": ({}, [second_physical.model_copy(update={"slot": 0, "slot_label": "00", "column_index": 0}), _virtual_slot(1)]),
        "unreadable_second_unit": ({**NUMBERED_UNIT_OUTPUTS, "mprutil -u 1 show adapter": "unrecognized synthetic text\n"}, [_inventory_slot()]),
        "complete": (dict(NUMBERED_UNIT_OUTPUTS), [_inventory_slot()]),
    }


def _render_case_payload(outputs: dict[str, str], slots: list[SlotView]) -> dict:
    fabric = _core_fabric(outputs, slots=slots).model_dump(mode="json")
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
        self.assertEqual(
            fabric.warnings,
            ["CORE controller data could not be read for mpr0, so controller, expander and enclosure counts may be incomplete."],
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
        failure = {"command": "sudo -n /usr/sbin/mprutil -u 1 show adapter", "canonical_command": "mprutil -u 1 show adapter"}
        cases = {
            "unreadable text": ({"mprutil -u 1 show adapter": "unrecognized synthetic text\n"}, None, "unreadable"),
            "no output": ({"mprutil -u 1 show adapter": ""}, None, "no_output"),
            "command failed": ({}, [failure], "command_failed"),
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
        header = f"2 {EXPANDER_ADDRESS} 000a 0001 0010 1\n"
        phy0 = "  0 1 000b 6.0 1.5 6.0 SAS Target\n"
        phy1 = "  1 1.5 6.0 No Device\n"
        cases = {
            "fewer rows than reported PHYs": (header + phy0, None),
            "a rejected row": (header + phy0 + "  1 malformed\n", None),
            "a repeated PHY": (header + phy0 + phy0, None),
            "a PHY number out of range": (header + phy0 + "  2 1.5 6.0 No Device\n", None),
            "every reported PHY": (header + phy0 + phy1, 1),
            "one reported PHY with no device": (f"1 {EXPANDER_ADDRESS} 000a 0001 0010 1\n" + "  0 1.5 6.0 No Device\n", 0),
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


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--write-render-fixture"]:
        RENDER_FIXTURE.write_text(json.dumps(_render_cases(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        unittest.main()
