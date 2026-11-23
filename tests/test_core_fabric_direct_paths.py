"""CORE direct-device paths use SES bay evidence, never cross-HBA similarity."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import MultipathMember, MultipathView, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot
from app.services.ssh_probe import SSHCommandResult
from app.services.truenas_ws import TrueNASRawData
from tests.inventory_fixtures import build_inventory_service


SHELF = "5000c50000000f01"
RENDER_FIXTURE = Path(__file__).parent / "fixtures" / "sas_fabric" / "core_direct_paths.json"


def synthetic_outputs() -> dict[str, str]:
    outputs = {"mprutil show adapters": "\n".join(
        f"/dev/mpr{unit} SAS3008 Synthetic 16.00.12.00" for unit in range(2)
    )}
    for unit, device in enumerate(("da3", "da11")):
        outputs[f"mprutil -u {unit} show adapter"] = f"mpr{unit} Adapter:\n  Board Name: Synthetic HBA"
        outputs[f"mprutil -u {unit} show expanders"] = (
            f"1 5000c50000000e0{unit} 0009 0001 0002 1\n"
            "  0 0001 0001 12.0 1.5 12.0 SAS Initiator"
        )
        outputs[f"mprutil -u {unit} show enclosures"] = f"2 {SHELF} 0008 0002 SES"
        # Controller-local target 4, intentionally different from either da number.
        outputs[f"mprutil -u {unit} show devices"] = (
            f"00 4 5000c50000000d0{unit} 0020 0009 SAS Target 12.0 0002 0"
        )
    outputs["camcontrol devlist -v"] = "\n".join(
        f"scbus{unit} on mpr{unit} bus 0:\n"
        f"<ExampleCo SAME-MODEL 0001> at scbus{unit} target 4 lun 0 ({device},pass{unit})"
        for unit, device in enumerate(("da3", "da11"))
    )
    outputs["sesutil map"] = "".join(
        f"""ses{unit}:
  Enclosure Name: ExampleCo SyntheticShelf
  Enclosure ID: {SHELF}
  Element 7, Type: Array Device Slot
    Status: OK
    Description: Slot01
    Device Names: {device}, pass{unit}
  Element 9, Type: Array Device Slot
    Status: Not Installed
    Description: Slot02
"""
        for unit, device in enumerate(("da3", "da11"))
    )
    return outputs


class CoreFabricDirectPathTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, api_device="da11", pool_device="da11"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        settings.layout.slot_count = 2
        settings.layout.rows = 1
        settings.layout.columns = 2
        outputs = synthetic_outputs()
        system = SystemConfig(
            id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core"),
            ssh=SSHConfig(enabled=True, host="core.example.test", commands=list(outputs)),
        )
        client = AsyncMock()
        client.fetch_all.return_value = TrueNASRawData(
            enclosures=[], disks=[{"name": api_device, "serial": "SANITIZED-DIRECT-DISK"}],
            pools=[{"name": "synthetic-pool", "topology": {"special": [{
                "type": "MIRROR", "name": "mirror-0", "children": [
                    {"type": "DISK", "disk": pool_device, "status": "ONLINE"},
                ],
            }]}}], disk_temperatures={}, smart_test_results=[],
        )
        probe = AsyncMock()
        probe.run_planned_commands.return_value = [
            SSHCommandResult(command=command, ok=True, stdout=output, exit_code=0)
            for command, output in outputs.items()
        ]
        service = build_inventory_service(settings, system, client, probe, directory.name)
        snapshot = await service.get_snapshot()
        self.assertEqual(snapshot.slots[0].raw_status["device_names"], ["da3", "da11"])
        self.assertIsNone(snapshot.slots[0].multipath)
        self.assertEqual(snapshot.slots[0].pool_name, "synthetic-pool")
        return system, snapshot, outputs

    def build(self, system, snapshot, outputs):
        return build_sas_fabric_snapshot(system=system, snapshot=snapshot, ssh_outputs=outputs)

    def bay(self, fabric):
        return next(trace for trace in fabric.traces if trace.id == "bay:0")

    async def test_direct_disk_has_both_ports_and_controller_local_mpr_context(self):
        system, snapshot, outputs = await self.fixture()
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                fabric = self.build(system, snapshot, dict(reversed(list(outputs.items()))) if reverse else outputs)
                bay = self.bay(fabric)
                self.assertEqual(bay.metrics["path_states"], [
                    {"controller": "mpr0", "state": "standby", "device_name": "da3"},
                    {"controller": "mpr1", "state": "active", "device_name": "da11"},
                ])
                self.assertEqual({row["member_device_name"] for row in bay.metrics["mpr_devices"]}, {"da3", "da11"})
                for row in bay.metrics["mpr_devices"]:
                    self.assertEqual(row["handle"], "0020")
                    self.assertEqual(row["speed"], "12.0")
                    self.assertEqual(row["mpr_slot"], "0")
                    self.assertTrue(row["expander_ids"])
                    self.assertTrue(row["sas_address"].startswith("5000c500"))
                links = {link.id: link for link in fabric.links}
                nodes = {node.id: node for node in fabric.nodes}
                for controller, state in (("mpr0", "standby"), ("mpr1", "active")):
                    path = f"path:{controller}:{state}"
                    self.assertIn(path, nodes)
                    self.assertIn(f"path-bay:{path}->bay:0:0", links)
                    self.assertIn(path, bay.node_ids)
                    self.assertEqual(nodes[path].related_slots, [0])
                    self.assertNotIn("multipath members", nodes[path].evidence)
                self.assertFalse(next(trace for trace in fabric.traces if trace.id == "bay:1").metrics["path_states"])
                self.assertEqual({row["controller"] for row in fabric.paths}, {"mpr0", "mpr1"})

    async def test_pool_uses_peer_path_instead_of_api_preferred_device(self):
        system, snapshot, outputs = await self.fixture(api_device="da3")
        states = {row["device_name"]: row["state"] for row in self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]}
        self.assertEqual(states, {"da3": "standby", "da11": "active"})

    async def test_missing_cam_controller_or_target_is_not_read_without_location_guess(self):
        system, snapshot, outputs = await self.fixture()
        for cam, field in (
            ("", "controller"),
            (outputs["camcontrol devlist -v"].replace("target 4", "target 1234567890"), "target"),
            (outputs["camcontrol devlist -v"] + "\n" + outputs["camcontrol devlist -v"].replace("target 4", "target 5"), "target"),
        ):
            with self.subTest(field=field):
                fabric = self.build(system, snapshot, {**outputs, "camcontrol devlist -v": cam})
                bay = self.bay(fabric)
                self.assertEqual(len(bay.metrics["path_states"]), 2)
                self.assertTrue(all(field in row["not_read"] for row in bay.metrics["path_states"]))
                if field == "target":
                    self.assertEqual({row["controller"] for row in bay.metrics["path_states"]}, {"mpr0", "mpr1"})
                self.assertEqual(bay.metrics["mpr_devices"], [])

    async def test_other_bay_claim_prevents_borrowing_even_with_same_cam_target(self):
        system, snapshot, outputs = await self.fixture()
        snapshot.slots[1] = SlotView(
            slot=1, slot_label="02", row_index=0, column_index=1,
            present=True, device_name="da3", raw_status={"device_names": ["da3"]},
        )
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da11"])
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da11"])

    async def test_bay_must_name_own_disk_and_not_infer_from_cam_similarity(self):
        system, snapshot, outputs = await self.fixture()
        for names in ([], ["da3"], None):
            with self.subTest(names=names):
                snapshot.slots[0].raw_status["device_names"] = names
                bay = self.bay(self.build(system, snapshot, outputs))
                self.assertEqual(bay.metrics["path_states"], [])
                self.assertEqual(bay.metrics["mpr_devices"], [])

    async def test_unverified_ses_or_unknown_physical_bay_does_not_supply_paths(self):
        system, snapshot, outputs = await self.fixture()
        for field, value in (("ssh_ses_targets", []), ("physical_location_known", False), ("present", False)):
            with self.subTest(field=field):
                changed = snapshot.model_copy(deep=True)
                setattr(changed.slots[0], field, value)
                if field == "ssh_ses_targets":
                    changed.slots[0].ssh_ses_device = None
                    changed.slots[0].raw_status.pop("ses_device", None)
                bay = self.bay(self.build(system, changed, outputs))
                self.assertEqual(bay.metrics["path_states"], [])

    async def test_render_fixture_matches_current_producer(self):
        system, snapshot, outputs = await self.fixture()
        expected = {
            "slots": [slot.model_dump(mode="json") for slot in snapshot.slots],
            "fabric": self.build(system, snapshot, outputs).model_dump(mode="json", exclude={"generated_at"}),
        }
        self.assertEqual(json.loads(RENDER_FIXTURE.read_text()), expected)

    async def test_explicit_multipath_members_keep_precedence(self):
        system, snapshot, outputs = await self.fixture()
        snapshot.slots[0].multipath = MultipathView(name="disk0", device_name="multipath/disk0", members=[
            MultipathMember(device_name="da11", controller_label="mpr1", state="PASSIVE"),
        ])
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual(bay.metrics["path_states"], [{"controller": "mpr1", "state": "passive", "device_name": "da11"}])

    async def test_offline_export_preserves_both_direct_path_ports(self):
        from app.route_support import templates
        from app.services.snapshot_export import EXPORT_RENDER_CACHE, EXPORT_HISTORY_CACHE, SnapshotExportService
        from tests.test_snapshot_export import FakeHistoryBackend, build_request

        EXPORT_RENDER_CACHE.clear()
        EXPORT_HISTORY_CACHE.clear()
        self.addCleanup(EXPORT_RENDER_CACHE.clear)
        self.addCleanup(EXPORT_HISTORY_CACHE.clear)
        system, snapshot, outputs = await self.fixture()
        fabric = self.build(system, snapshot, outputs)
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        rendered = await exporter.build_enclosure_snapshot_html(
            request=build_request(), snapshot=snapshot, sas_fabric=fabric,
            selected_slot=0, history_window_hours=24, io_chart_mode="total",
        )
        match = re.search(r"preloadedSasFabric:\s*(.*),\n", rendered.html)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        bay = next(trace for trace in payload["traces"] if trace["id"] == "bay:0")
        self.assertEqual(len(bay["metrics"]["path_states"]), 2)
        self.assertEqual(len(bay["metrics"]["mpr_devices"]), 2)


if __name__ == "__main__":
    import asyncio
    import sys

    if sys.argv[1:] == ["--write-fixture"]:
        async def write_fixture():
            case = CoreFabricDirectPathTests()
            try:
                system, snapshot, outputs = await case.fixture()
                payload = {
                    "slots": [slot.model_dump(mode="json") for slot in snapshot.slots],
                    "fabric": case.build(system, snapshot, outputs).model_dump(mode="json", exclude={"generated_at"}),
                }
                RENDER_FIXTURE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            finally:
                case.doCleanups()
        asyncio.run(write_fixture())
    else:
        unittest.main()
