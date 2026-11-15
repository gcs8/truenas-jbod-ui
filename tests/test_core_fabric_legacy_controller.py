"""CORE Storage Fabric controller identity without a numbered MPR unit (#1219).

When no numbered ``mprutil -u N`` output exists, the CORE builder falls back
to the unnumbered ``mprutil show ...`` commands. That fallback must not invent
a controller when nothing was observed, must keep the adapter details it was
given, and every link, trace and controller reference must name a node that
exists in the same graph.
"""

import unittest

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot

NO_CORE_DATA_WARNING = "No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions."
SELECTED_ENCLOSURE = "5000000000000001"
EXPANDER_ADDRESS = "5000000000000002"

LEGACY_OUTPUTS = {
    "mprutil show adapter": (
        "Synthetic Adapter:\n"
        "Board Name: Synthetic HBA\n"
        "Firmware Revision: 1.2.3\n"
    ),
    "mprutil show expanders": f"1 {EXPANDER_ADDRESS} 000a 0001 0010 1\n",
    "mprutil show enclosures": f"1 {SELECTED_ENCLOSURE} 000b 0010 SES\n",
}


def _slot(**overrides) -> SlotView:
    values = {"slot": 0, "slot_label": "00", "row_index": 0, "column_index": 0}
    values.update(overrides)
    return SlotView(**values)


def _core_fabric(outputs: dict[str, str], *, slots: list[SlotView] | None = None, selected: str | None = None):
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core")),
        snapshot=InventorySnapshot(
            slots=slots if slots is not None else [_slot()],
            refresh_interval_seconds=30,
            selected_enclosure_id=selected,
        ),
        ssh_outputs=outputs,
    )


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

        self.assertFalse(fabric.available)
        self.assertEqual(fabric.controllers, [])
        self.assertEqual([node.id for node in fabric.nodes if node.kind == "controller"], [])
        self.assertIn(NO_CORE_DATA_WARNING, fabric.warnings)
        self.assertNotIn("mpr-1", fabric.model_dump_json())
        self.assert_references_resolve(fabric)

    def test_blank_unnumbered_output_does_not_invent_a_controller(self) -> None:
        fabric = _core_fabric({"mprutil show adapter": "  \n", "mprutil show devices": ""})

        self.assertFalse(fabric.available)
        self.assertEqual(fabric.controllers, [])
        self.assertIn(NO_CORE_DATA_WARNING, fabric.warnings)

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


if __name__ == "__main__":
    unittest.main()
