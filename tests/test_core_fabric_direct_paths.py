"""CORE direct-device paths use SES bay evidence, never cross-HBA similarity."""
from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from app.config import SSHConfig, Settings, SystemConfig, TrueNASConfig
from app.models.domain import MultipathMember, MultipathView, SlotView
from app.services import sas_fabric
from app.services.sas_fabric import (
    FabricDiskOwnership, _cam_member_target, _core_path_members, attach_fabric_disk_ownership,
    build_sas_fabric_snapshot, fabric_disk_ownership, fabric_path_evidence, snapshot_fabric_disk_ownership,
)
from app.services.parsers import parse_camcontrol_devlist
from app.services.ssh_probe import SSHCommandResult
from app.services.truenas_ws import TrueNASRawData
from tests.inventory_fixtures import build_inventory_service


SHELF = f"5000c500{0xF01:08x}"
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
        # Distinct controller-local targets, neither equal to its da number.
        outputs[f"mprutil -u {unit} show devices"] = (
            f"00 {4 + unit * 8} 5000c50000000d0{unit} 0020 0009 SAS Target 12.0 0002 0"
        )
    outputs["camcontrol devlist -v"] = "\n".join(
        f"scbus{unit} on mpr{unit} bus 0:\n"
        f"<ExampleCo SAME-MODEL 0001> at scbus{unit} target {4 + unit * 8} lun 0 ({device},pass{unit})"
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
    async def fixture(self, *, api_device="da11", pool_device="da11", pool_label=None, extra_disks=(), api_disk=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        settings.layout.slot_count = 2
        settings.layout.rows = 1
        settings.layout.columns = 2
        outputs = synthetic_outputs()
        if pool_label:
            outputs["glabel status"] = f"{pool_label} N/A {pool_device}p1\n"
        system = SystemConfig(
            id="synthetic-core", label="Synthetic CORE", truenas=TrueNASConfig(platform="core"),
            ssh=SSHConfig(enabled=True, host="core.example.test", commands=list(outputs)),
        )
        client = AsyncMock()
        client.fetch_all.return_value = TrueNASRawData(
            enclosures=[], disks=[api_disk or {"name": api_device, "serial": "SANITIZED-DIRECT-DISK"}, *extra_disks],
            pools=[{"name": "synthetic-pool", "topology": {"special": [{
                "type": "MIRROR", "name": "mirror-0", "children": [
                    {"type": "DISK", "disk": pool_device, "status": "ONLINE", **({"path": f"/dev/{pool_label}"} if pool_label else {})},
                ],
            }]}}], disk_temperatures={}, smart_test_results=[],
        )
        probe = AsyncMock()
        probe.run_planned_commands.return_value = [
            SSHCommandResult(command=command, ok=True, stdout=output, exit_code=0)
            for command, output in outputs.items()
        ]
        service = build_inventory_service(settings, system, client, probe, directory.name)
        self.inventory_service = service
        snapshot = await service.get_snapshot()
        # The process-local API ownership evidence recorded by this build.
        self.assertIsNotNone(snapshot_fabric_disk_ownership(snapshot))
        self.assertEqual(snapshot.slots[0].raw_status["device_names"], ["da3", "da11"])
        self.assertIsNone(snapshot.slots[0].multipath)
        self.assertEqual(snapshot.slots[0].pool_name, "synthetic-pool")
        return system, snapshot, outputs

    def build(self, system, snapshot, outputs, *, coherent=True):
        """Build as if the snapshot had been read from these outputs.

        `coherent=False` keeps the snapshot's own recorded build evidence, so
        outputs that differ from it model a later source read.
        """
        ownership = snapshot_fabric_disk_ownership(snapshot)
        if coherent and ownership is not None:
            snapshot = snapshot.model_copy()
            attach_fabric_disk_ownership(snapshot, replace(ownership, path_evidence=fabric_path_evidence(outputs)))
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
            (re.sub(r"target [0-9]+", "target 1234567890", outputs["camcontrol devlist -v"]), "target"),
            (outputs["camcontrol devlist -v"] + "\n" + re.sub(r"target [0-9]+", "target 5", outputs["camcontrol devlist -v"]), "target"),
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
        outputs["camcontrol devlist -v"] = outputs["camcontrol devlist -v"].replace("target 12", "target 4")
        outputs["mprutil -u 1 show devices"] = outputs["mprutil -u 1 show devices"].replace("00 12 ", "00 4 ")
        snapshot.slots[1] = SlotView(
            slot=1, slot_label="02", row_index=0, column_index=1,
            present=True, device_name="da3", raw_status={"device_names": ["da3"]},
        )
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da11"])
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da11"])

    async def test_independent_api_disk_outside_rendered_slots_is_not_a_peer(self):
        for enclosure in (None, {"id": "synthetic-other-shelf", "slot": 1}):
            with self.subTest(enclosure=enclosure):
                extra: dict[str, Any] = {"name": "da11", "serial": "SANITIZED-INDEPENDENT-DISK"}
                if enclosure is not None:
                    extra["enclosure"] = enclosure
                system, snapshot, outputs = await self.fixture(
                    api_device="da3", pool_device="da3", extra_disks=[extra],
                )
                self.assertEqual(snapshot.summary.source_disk_count, 2)
                self.assertEqual(snapshot.summary.unplaced_disk_count, 1)
                self.assertEqual(snapshot.slots[0].device_name, "da3")
                self.assertEqual(snapshot.slots[0].raw_status["device_names"], ["da3", "da11"])
                # Exercise the real service as well as export's standalone
                # snapshot builder; the second API owner is not a visible bay.
                service_fabric = await self.inventory_service.get_sas_fabric_snapshot()
                for fabric in (service_fabric, self.build(system, snapshot, outputs)):
                    bay = self.bay(fabric)
                    self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da3"])
                    self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da3"])
                    self.assertEqual([row.source for row in fabric.links if row.kind == "path-bay" and row.slot == 0], ["path:mpr0:active"])

    async def test_missing_inventory_peer_validation_does_not_admit_raw_ses_peer(self):
        system, snapshot, outputs = await self.fixture()
        # The ownership evidence lives only on the snapshot object its build
        # produced. A snapshot rebuilt from its payload has none, so rendered
        # bays alone cannot prove a co-listed name is unowned: no peer.
        payload = snapshot.model_dump(mode="json")
        reloaded = type(snapshot).model_validate(payload)
        self.assertIsNone(snapshot_fabric_disk_ownership(reloaded))
        self.assertEqual(reloaded.model_dump(mode="json"), payload)
        bay = self.bay(self.build(system, reloaded, outputs))
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da11"])
        # Nor can it show which CAM/MPR read it came from: those are not read.
        self.assertIsNone(bay.metrics["path_states"][0]["controller"])
        self.assertEqual(bay.metrics["mpr_devices"], [])
        fabric = self.build(system, snapshot, outputs)
        serialized = json.dumps(payload) + snapshot.model_dump_json() + fabric.model_dump_json() + repr(snapshot)
        for marker in ("_fabric_disk_ownership", "FabricDiskOwnership"):
            self.assertNotIn(marker, serialized)
        # Copies of the same build (cache adoption, enclosure views) keep it.
        for copied in (snapshot.model_copy(), snapshot.model_copy(deep=True), snapshot.model_copy(update={"warnings": ["x"]})):
            self.assertEqual(len(self.bay(self.build(system, copied, outputs)).metrics["path_states"]), 2)

    async def test_invalid_cam_controller_is_not_read_before_direct_projection(self):
        system, snapshot, outputs = await self.fixture()
        for token in ("mpr" + "9" * 4096, "mpr0-untrusted", "mpr0/1", "mpr00000"):
            with self.subTest(length=len(token), suffix=token[-12:]):
                changed = {**outputs, "camcontrol devlist -v": outputs["camcontrol devlist -v"].replace("on mpr0 bus", f"on {token} bus")}
                cam = parse_camcontrol_devlist(changed["camcontrol devlist -v"])
                direct = _core_path_members(snapshot.slots, cam, snapshot_fabric_disk_ownership(snapshot))[0]
                self.assertIsNone(next(member for member in direct if member.device_name == "da3").controller_label)
                fabric = self.build(system, snapshot, changed)
                bay = self.bay(fabric)
                row = next(row for row in bay.metrics["path_states"] if row["device_name"] == "da3")
                self.assertIsNone(row["controller"])
                self.assertIn("controller", row["not_read"])
                self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da11"])
                payload = fabric.model_dump_json()
                self.assertNotIn(token, payload)
                self.assertLess(len(payload), 60_000)
                self.assertEqual([row.source for row in fabric.links if row.kind == "path-bay" and row.slot == 0], ["path:mpr1:active"])
        for token in ("mpr0", "mpr9999", "mps2", "mpt0", "mps9", "mpr1"):
            with self.subTest(valid_controller=token):
                cam = parse_camcontrol_devlist(outputs["camcontrol devlist -v"].replace("on mpr0 bus", f"on {token} bus"))
                direct = _core_path_members(snapshot.slots, cam, snapshot_fabric_disk_ownership(snapshot))[0]
                self.assertEqual(next(member for member in direct if member.device_name == "da3").controller_label, token)

    async def test_preexisting_multipath_projection_uses_same_controller_guard(self):
        system, snapshot, outputs = await self.fixture()
        for token in ("mpr" + "9" * 4096, "mpr0-untrusted"):
            with self.subTest(length=len(token)):
                snapshot.slots[0].multipath = MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                    MultipathMember(device_name="da11", controller_label=token, state="ACTIVE"),
                ])
                fabric = self.build(system, snapshot, outputs)
                self.assertIsNone(self.bay(fabric).metrics["path_states"][0]["controller"])
                self.assertNotIn(token, fabric.model_dump_json())
                self.assertFalse(any(row.kind == "path-bay" for row in fabric.links))

    async def source_only_refresh(self, *, drop_independent_disk):
        """Publish a newer source bundle while the bay snapshot stays cached."""
        service = self.inventory_service
        if drop_independent_disk:
            raw = copy.deepcopy(service.truenas_client.fetch_all.return_value)
            raw.disks = raw.disks[:1]
            service.truenas_client.fetch_all.return_value = raw
            service.ssh_probe.run_planned_commands.return_value = [
                replace(row, stdout=row.stdout.split("ses1:")[0]) if row.command == "sesutil map" else row
                for row in service.ssh_probe.run_planned_commands.return_value
            ]
        before = service._source_bundle.publication
        self.assertIsNotNone(await service._background_source_bundle_refresh())
        self.assertNotEqual(service._source_bundle.publication, before)

    async def test_source_only_refresh_keeps_ownership_from_the_snapshot_publication(self):
        system, snapshot, outputs = await self.fixture(
            api_device="da3", pool_device="da3", extra_disks=[{"name": "da11", "serial": "SANITIZED-INDEPENDENT-DISK"}],
        )
        service = self.inventory_service
        rows = lambda fabric: [row["device_name"] for row in self.bay(fabric).metrics["path_states"]]  # noqa: E731
        first = await service.get_sas_fabric_snapshot()
        self.assertEqual(rows(first), ["da3"])
        # The newer publication no longer lists the independent disk (nor SES
        # the shared bay). The cached bay snapshot is still the first one.
        await self.source_only_refresh(drop_independent_disk=True)
        cached = await service.get_snapshot()
        self.assertEqual(cached.generated_at, snapshot.generated_at)
        self.assertEqual(cached.slots[0].raw_status["device_names"], ["da3", "da11"])
        mixed = await service.get_sas_fabric_snapshot()
        self.assertEqual(rows(mixed), ["da3"])
        self.assertEqual([row["member_device_name"] for row in self.bay(mixed).metrics["mpr_devices"]], ["da3"])
        bundle, snapshots, _known = service._sas_fabric_alias_observation()
        self.assertNotIn("path:mpr1:standby", {node.id for node in service._observed_sas_fabric_nodes(bundle, snapshots)})
        # Coherent control after a forced rebuild from the newer publication.
        self.assertEqual(rows(await service.get_sas_fabric_snapshot(force_refresh=True)), ["da3"])

    async def test_source_only_refresh_keeps_a_proven_peer_of_the_cached_snapshot(self):
        system, snapshot, outputs = await self.fixture()
        service = self.inventory_service
        rows = lambda fabric: [row["device_name"] for row in self.bay(fabric).metrics["path_states"]]  # noqa: E731
        self.assertEqual(rows(await service.get_sas_fabric_snapshot()), ["da3", "da11"])
        await self.source_only_refresh(drop_independent_disk=False)
        self.assertEqual((await service.get_snapshot()).generated_at, snapshot.generated_at)
        self.assertEqual(rows(await service.get_sas_fabric_snapshot()), ["da3", "da11"])
        bundle, snapshots, _known = service._sas_fabric_alias_observation()
        self.assertIn("path:mpr0:standby", {node.id for node in service._observed_sas_fabric_nodes(bundle, snapshots)})
        # Evidence from another build is never borrowed by a snapshot it did
        # not produce: an unknown snapshot reads as no ownership evidence.
        self.assertIsNone(snapshot_fabric_disk_ownership(type(snapshot).model_validate(snapshot.model_dump())))

    async def test_source_only_refresh_never_draws_newer_cam_or_mpr_evidence_on_a_cached_bay(self):
        system, snapshot, outputs = await self.fixture(api_device="da3", pool_device="da3")
        service = self.inventory_service
        ports = lambda fabric: [(row["device_name"], row["controller"]) for row in self.bay(fabric).metrics["path_states"]]  # noqa: E731
        self.assertEqual(ports(await service.get_sas_fabric_snapshot()), [("da3", "mpr0"), ("da11", "mpr1")])
        # Publication B: SES no longer co-lists da11, an independent API disk
        # owns it, and CAM reports it on another controller with a new address.
        moved_address = f"5000c500{0xD0B:08x}"
        newer = dict(outputs)
        newer["sesutil map"] = outputs["sesutil map"].split("ses1:")[0]
        newer["camcontrol devlist -v"] = outputs["camcontrol devlist -v"].replace("on mpr1 bus", "on mpr2 bus")
        newer["mprutil show adapters"] = outputs["mprutil show adapters"] + "\n/dev/mpr2 SAS3008 Synthetic 16.00.12.00"
        for subcommand in ("adapter", "expanders", "enclosures", "devices"):
            newer[f"mprutil -u 2 show {subcommand}"] = outputs[f"mprutil -u 1 show {subcommand}"].replace("mpr1", "mpr2")
        newer["mprutil -u 2 show devices"] = newer["mprutil -u 2 show devices"].replace(f"5000c500{0xD01:08x}", moved_address)
        raw = copy.deepcopy(service.truenas_client.fetch_all.return_value)
        raw.disks = [*raw.disks, {"name": "da11", "serial": "SANITIZED-INDEPENDENT-DISK"}]
        service.truenas_client.fetch_all.return_value = raw
        service.system.ssh.commands = list(newer)
        service.ssh_probe.run_planned_commands.return_value = [
            SSHCommandResult(command=command, ok=True, stdout=output, exit_code=0) for command, output in newer.items()
        ]
        before = service._source_bundle.publication
        self.assertIsNotNone(await service._background_source_bundle_refresh())
        self.assertNotEqual(service._source_bundle.publication, before)
        cached = await service.get_snapshot()
        self.assertEqual(cached.generated_at, snapshot.generated_at)
        mixed = await service.get_sas_fabric_snapshot()
        self.assertEqual((mixed.snapshot_cache_state, mixed.source_cache_state), ("hit", "hit"))
        bay = self.bay(mixed)
        # The cached bay keeps its own SES and ownership facts; CAM/MPR from a
        # different read cannot be tied to it, so they are not read.
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da3", "da11"])
        for row in bay.metrics["path_states"]:
            self.assertIsNone(row["controller"])
            self.assertIn("controller", row["not_read"])
            self.assertIn("target", row["not_read"])
            self.assertEqual(row["not_read_reasons"]["controller"], sas_fabric.PATH_EVIDENCE_REASON)
        self.assertEqual(bay.metrics["mpr_devices"], [])
        self.assertFalse([link for link in mixed.links if link.kind == "path-bay" and link.slot == 0])
        self.assertNotIn(moved_address, json.dumps(bay.model_dump(mode="json")))
        self.assertFalse([node.id for node in mixed.nodes if node.id.startswith("path:mpr2:")])
        self.assertEqual(sum("not from the read" in warning for warning in mixed.warnings), 1)
        bundle, snapshots, _known = service._sas_fabric_alias_observation()
        observed = {node.id for node in service._observed_sas_fabric_nodes(bundle, snapshots)}
        self.assertFalse([node for node in observed if node.startswith("path:mpr2:")])
        # Coherent control: a forced rebuild from publication B.
        coherent = await service.get_sas_fabric_snapshot(force_refresh=True)
        self.assertEqual(ports(coherent), [("da3", "mpr0")])
        self.assertEqual([row["member_device_name"] for row in self.bay(coherent).metrics["mpr_devices"]], ["da3"])

    async def test_direct_cam_and_mpr_evidence_must_be_the_snapshot_build_read(self):
        system, snapshot, outputs = await self.fixture()
        moved = {**outputs, "camcontrol devlist -v": outputs["camcontrol devlist -v"].replace("on mpr1 bus", "on mpr2 bus")}
        for coherent in (True, False):
            with self.subTest(coherent=coherent):
                fabric = self.build(system, snapshot, moved, coherent=coherent)
                rows = self.bay(fabric).metrics["path_states"]
                if coherent:
                    self.assertEqual({row["controller"] for row in rows}, {"mpr0", "mpr2"})
                    self.assertTrue(all("not_read" not in row for row in rows))
                else:
                    self.assertEqual([row["controller"] for row in rows], [None, None])
                    self.assertTrue(all({"controller", "target"} <= set(row["not_read"]) for row in rows))
                    self.assertEqual(self.bay(fabric).metrics["mpr_devices"], [])
        # Unchanged evidence from a newer read is the same evidence.
        same = self.build(system, snapshot, dict(outputs), coherent=False)
        self.assertEqual({row["member_device_name"] for row in self.bay(same).metrics["mpr_devices"]}, {"da3", "da11"})

    async def test_adapter_summary_units_count_against_the_controller_limit(self):
        system, snapshot, outputs = await self.fixture()
        limit = sas_fabric.MAX_FABRIC_CONTROLLERS
        for units in (limit, limit + 1):
            with self.subTest(units=units):
                changed = {**outputs, "mprutil show adapters": "\n".join(
                    f"/dev/mpr{unit} SAS3008 Synthetic 16.00.12.00" for unit in range(units)
                )}
                # The highest unit also reports its own expander and enclosure.
                last = units - 1
                for subcommand in ("adapter", "expanders", "enclosures", "devices"):
                    changed[f"mprutil -u {last} show {subcommand}"] = outputs[f"mprutil -u 1 show {subcommand}"].replace("mpr1", f"mpr{last}")
                fabric = self.build(system, snapshot, changed)
                controller_ids = [node.id for node in fabric.nodes if node.kind == "controller"]
                self.assertEqual(len(controller_ids), limit)
                self.assertEqual(len(fabric.controllers), limit)
                limited = [warning for warning in fabric.warnings if "controller limit" in warning]
                payload = fabric.model_dump_json()
                read = fabric.raw["controller_read"]
                if units == limit:
                    self.assertFalse(limited)
                    self.assertNotIn("units_over_controller_limit", read)
                    self.assertIn(f"mpr{limit - 1}", {row["controller"] for row in read["units"]})
                else:
                    self.assertEqual(len(limited), 1)
                    self.assertIn("1 ", limited[0])
                    omitted = f"controller:mpr{limit}"
                    # The graph leaves it out everywhere and says how many.
                    self.assertNotIn(omitted, controller_ids)
                    self.assertNotIn(omitted, {controller["id"] for controller in fabric.controllers})
                    self.assertFalse([link.id for link in fabric.links if omitted in (link.source, link.target)])
                    self.assertFalse([trace.id for trace in fabric.traces if omitted in trace.node_ids])
                    self.assertFalse([node.id for node in fabric.nodes if node.controller_id == omitted])
                    self.assertNotIn(omitted, payload)
                    self.assertEqual(read.get("units_over_controller_limit"), 1)
                    self.assertEqual(read["counts"]["controllers"], "partial")
                    # Collection results still report the unit as read.
                    self.assertIn({"controller": f"mpr{limit}", "result": "read"}, read["units"])
                # The bay's own paths keep their controllers inside the limit.
                self.assertEqual(
                    {row["controller"] for row in self.bay(fabric).metrics["path_states"]}, {"mpr0", "mpr1"},
                )
                self.assertTrue({link.target for link in fabric.links if link.kind == "host-controller"} <= set(controller_ids))

    async def test_four_direct_paths_with_partial_mpr_rows_keep_their_own_rows(self):
        system, snapshot, outputs = await self.fixture()
        changed = snapshot.model_copy(deep=True)
        changed.slots[0].raw_status["device_names"] = ["da3", "da4", "da11", "da12"]
        cam = "\n".join(
            f"scbus{unit} on mpr{unit} bus 0:\n"
            + "\n".join(
                f"<ExampleCo SAME-MODEL 0001> at scbus{unit} target {target} lun 0 ({device},pass{target})"
                for device, target in devices
            )
            for unit, devices in enumerate(((("da3", 4), ("da4", 5)), (("da11", 12), ("da12", 13))))
        )
        fabric = self.build(system, changed, {**outputs, "camcontrol devlist -v": cam})
        bay = self.bay(fabric)
        # Mirrored by the rendered Disk Path test in core_fabric_direct_paths.test.js.
        self.assertEqual(bay.metrics["path_states"], [
            {"controller": "mpr0", "state": "standby", "device_name": "da3"},
            {"controller": "mpr0", "state": "standby", "device_name": "da4"},
            {"controller": "mpr1", "state": "active", "device_name": "da11"},
            {"controller": "mpr1", "state": "standby", "device_name": "da12"},
        ])
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da3", "da11"])

    async def test_direct_row_rejects_conflicting_target_even_when_named(self):
        system, snapshot, outputs = await self.fixture()
        outputs["mprutil -u 0 show devices"] = "00 5 5000c50000000d00 0020 0009 SAS Target da3 12.0 0002 0"
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da11"])
        self.assertEqual(len(bay.metrics["path_states"]), 2)

    async def test_direct_row_never_joins_by_position_without_target(self):
        system, snapshot, outputs = await self.fixture()
        # Simulate parsed rows because the normal text format requires a target.
        from unittest.mock import patch
        original = sas_fabric._parse_unit_data
        parsed = original(sas_fabric._canonical_outputs(outputs))
        parsed[0]["devices"][0]["target"] = None
        with patch.object(sas_fabric, "_parse_unit_data", return_value=parsed):
            bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da11"])

    async def test_member_expander_is_only_its_controller_local_parent(self):
        system, snapshot, outputs = await self.fixture()
        outputs["mprutil -u 0 show expanders"] = (
            "1 5000c50000000eff 000a 0001 0002 1\n"
            + outputs["mprutil -u 0 show expanders"]
        )
        fabric = self.build(system, snapshot, outputs)
        row = next(row for row in self.bay(fabric).metrics["mpr_devices"] if row["member_device_name"] == "da3")
        self.assertEqual(row["expander_ids"], ["expander:controller:mpr0:5000c50000000e00"])
        outputs["mprutil -u 0 show devices"] = outputs["mprutil -u 0 show devices"].replace("0020 0009", "0020 000b")
        unknown = next(row for row in self.bay(self.build(system, snapshot, outputs)).metrics["mpr_devices"] if row["member_device_name"] == "da3")
        self.assertEqual(unknown["expander_ids"], [])

    async def test_withheld_member_counts_are_partial_not_exact_zero(self):
        system, snapshot, outputs = await self.fixture()
        unread = self.build(system, snapshot, {**outputs, "camcontrol devlist -v": ""})
        self.assertEqual(unread.raw["controller_read"]["counts"]["paths"], "partial")
        limit = sas_fabric.MAX_FABRIC_CONTROLLERS
        changed = {**outputs, "mprutil show adapters": "\n".join(
            f"/dev/mpr{unit} SAS3008 Synthetic 16.00.12.00" for unit in range(limit)
        ), "camcontrol devlist -v": outputs["camcontrol devlist -v"].replace("on mpr1 bus", f"on mpr{limit} bus")}
        counts = self.build(system, snapshot, changed).raw["controller_read"]["counts"]
        self.assertEqual(counts["controllers"], "partial")
        self.assertEqual(counts["paths"], "partial")

    async def test_path_digest_collapses_command_aliases_with_last_writer_wins(self):
        system, snapshot, outputs = await self.fixture()
        duplicate = {**outputs, "/sbin/camcontrol devlist -v": outputs["camcontrol devlist -v"]}
        self.assertEqual(fabric_path_evidence(duplicate), fabric_path_evidence(outputs))
        self.assertEqual(fabric_path_evidence({**duplicate, "camcontrol devlist -v": "ignored"}),
                         fabric_path_evidence(outputs))
        bay = self.bay(self.build(system, snapshot, duplicate, coherent=False))
        self.assertEqual(len(bay.metrics["mpr_devices"]), 2)

    async def test_four_multipath_members_same_location_keep_every_own_target_row(self):
        system, snapshot, outputs = await self.fixture()
        members = []
        for unit, names in enumerate((("da3", "da4"), ("da11", "da12"))):
            controller = f"mpr{unit}"
            outputs[f"mprutil -u {unit} show devices"] += (
                f"\n01 {5 + unit * 8} 5000c50000000d1{unit} 0021 000a SAS Target 6.0 0002 0"
            )
            outputs[f"mprutil -u {unit} show expanders"] += f"\n1 5000c50000000e1{unit} 000a 0001 0002 1"
            outputs["camcontrol devlist -v"] += (
                f"\nscbus{unit} on {controller} bus 0:\n"
                f"<ExampleCo SAME-MODEL 0001> at scbus{unit} target {5 + unit * 8} lun 0 ({names[1]},pass{20 + unit})"
            )
            members.extend(MultipathMember(device_name=name, controller_label=controller, state="ACTIVE") for name in names)
        snapshot.slots[0].multipath = MultipathView(name="synthetic", device_name="multipath/synthetic", members=members)
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual(len(bay.metrics["path_states"]), 4)
        self.assertEqual({row["member_device_name"] for row in bay.metrics["mpr_devices"]}, {"da3", "da4", "da11", "da12"})
        self.assertTrue(all(len(row["expander_ids"]) == 1 for row in bay.metrics["mpr_devices"]))

    async def test_real_enclosure_key_merges_hba_handles_and_covers_empty_bays(self):
        system, snapshot, outputs = await self.fixture()
        outputs["mprutil -u 1 show enclosures"] = outputs["mprutil -u 1 show enclosures"].replace("0002", "0007")
        outputs["mprutil -u 1 show devices"] = outputs["mprutil -u 1 show devices"].replace("0002", "0007")
        outputs["mprutil -u 1 show expanders"] = outputs["mprutil -u 1 show expanders"].replace("0002", "0007")
        fabric = self.build(system, snapshot, outputs)
        boards = [node for node in fabric.nodes if node.kind == "enclosure"]
        self.assertEqual([node.id for node in boards], [f"enclosure:{SHELF}"])
        self.assertEqual(boards[0].related_slots, [0, 1])
        self.assertIsNone(boards[0].controller_id)
        self.assertEqual({link.target for link in fabric.links if link.kind == "enclosure-bay"}, {"bay:0", "bay:1"})
        self.assertFalse([node for node in fabric.nodes if node.kind == "backplane"])
        self.assertNotIn("Backplane Zone", fabric.model_dump_json())
        self.assertEqual({row["enclosure_id"] for row in self.bay(fabric).metrics["mpr_devices"]}, {boards[0].id})

    async def test_same_disk_alias_listed_by_ses_is_an_alternate_path(self):
        alias_disk = {"name": "da11", "devname": "da3", "serial": "SANITIZED-DIRECT-DISK"}
        system, snapshot, outputs = await self.fixture(pool_device="da11", api_disk=alias_disk)
        ownership = snapshot_fabric_disk_ownership(snapshot)
        self.assertEqual(ownership.owners["da3"], ownership.owners["da11"])
        self.assertEqual(snapshot.slots[0].raw_status.get("pool_device_name"), "da11")
        bay = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual(
            {(row["device_name"], row["controller"], row["state"]) for row in bay.metrics["path_states"]},
            {("da3", "mpr0", "standby"), ("da11", "mpr1", "active")},
        )
        self.assertEqual({row["member_device_name"] for row in bay.metrics["mpr_devices"]}, {"da3", "da11"})
        # Another bay's claim still wins over the alias.
        own = snapshot.slots[0].device_name
        alias = ({"da3", "da11"} - {own}).pop()
        claimed = snapshot.model_copy(deep=True)
        claimed.slots[1].raw_status["device_names"] = [alias]
        bay = self.bay(self.build(system, claimed, outputs))
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], [own])
        # A later, independent API owner of the alias wins too (last writer).
        independent = snapshot.model_copy()
        attach_fabric_disk_ownership(independent, fabric_disk_ownership([{"da3", "da11"}, {alias}]))
        bay = self.bay(self.build(system, independent, outputs))
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], [own])

    async def test_mixed_cam_controllers_keep_existing_multipath_links(self):
        system, _snapshot, _outputs = await self.fixture()
        service = self.inventory_service
        for controller in ("mpt0", "mps9", "mpr1"):
            with self.subTest(controller=controller):
                outputs = synthetic_outputs()
                outputs["camcontrol devlist -v"] = outputs["camcontrol devlist -v"].replace("on mpr0 bus", f"on {controller} bus")
                outputs["gmultipath list"] = (
                    "Geom name: synthetic\nConsumers:\n1. Name: da11\n   State: ACTIVE\n2. Name: da3\n   State: PASSIVE\n"
                )
                service.system.ssh.commands = list(outputs)
                service.ssh_probe.run_planned_commands.return_value = [
                    SSHCommandResult(command=command, ok=True, stdout=output, exit_code=0) for command, output in outputs.items()
                ]
                snapshot = await service.get_snapshot(force_refresh=True)
                self.assertEqual(
                    [(member.device_name, member.controller_label) for member in snapshot.slots[0].multipath.members],
                    [("da11", "mpr1"), ("da3", controller)],
                )
                fabric = self.build(system, snapshot, outputs)
                self.assertEqual(
                    {(row["device_name"], row["controller"]) for row in self.bay(fabric).metrics["path_states"]},
                    {("da11", "mpr1"), ("da3", controller)},
                )
                self.assertTrue(all("not_read" not in row for row in self.bay(fabric).metrics["path_states"]))
                self.assertEqual(
                    sorted(link.source for link in fabric.links if link.kind == "path-bay" and link.slot == 0),
                    sorted({"path:mpr1:active", f"path:{controller}:passive"}),
                )

    async def test_rejected_multipath_controller_is_recorded_not_read(self):
        system, snapshot, outputs = await self.fixture()
        for token in ("mpr" + "9" * 4096, "mpr0-untrusted", "MPT0", "mpt00000", "mpt0/1", "0mpt"):
            with self.subTest(length=len(token), suffix=token[-12:]):
                snapshot.slots[0].multipath = MultipathView(name="synthetic", device_name="multipath/synthetic", members=[
                    MultipathMember(device_name="da11", controller_label=token, state="ACTIVE"),
                    MultipathMember(device_name="da3", controller_label=None, state="PASSIVE"),
                ])
                rows = self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]
                self.assertEqual(rows[0], {"controller": None, "state": "active", "device_name": "da11", "not_read": ["controller"]})
                # A member that never had a controller keeps its existing shape.
                self.assertEqual(rows[1], {"controller": None, "state": "passive", "device_name": "da3"})

    def scaled_snapshot(self, snapshot, *, bays, paths, multipath, state=lambda n: "ACTIVE"):
        """Many bays, each listing many paths on distinct controller names."""
        template = snapshot.slots[0]
        slots, keys, cam = [], [], []
        for bay in range(bays):
            names = [f"da{bay * paths + index}" for index in range(paths)]
            raw = {key: value for key, value in template.raw_status.items() if key != "pool_device_name"}
            raw["device_names"] = names
            members = [
                MultipathMember(device_name=name, controller_label=f"hba{bay * paths + index}", state=state(bay * paths + index))
                for index, name in enumerate(names)
            ] if multipath else []
            slots.append(template.model_copy(deep=True, update={
                "slot": bay, "device_name": names[0], "smart_device_names": [], "raw_status": raw,
                "multipath": MultipathView(name=f"disk{bay}", device_name=f"multipath/disk{bay}", members=members) if multipath else None,
            }))
            keys.append(frozenset({names[0]}))
            for index, name in enumerate(names):
                unit = bay * paths + index
                cam.append(f"scbus{unit} on hba{unit} bus 0:\n<ExampleCo SAME-MODEL 0001> at scbus{unit} target 1 lun 0 ({name},pass{unit})")
        scaled = snapshot.model_copy(update={"slots": slots})
        attach_fabric_disk_ownership(scaled, fabric_disk_ownership(keys))
        return scaled, tuple(keys), "\n".join(cam)

    def assert_within_limits(self, fabric, *, bays):
        from app.services import sas_fabric
        self.assertLessEqual(sum(node.kind == "controller" for node in fabric.nodes), sas_fabric.MAX_FABRIC_CONTROLLERS)
        self.assertLessEqual(sum(node.id.startswith("path:") for node in fabric.nodes), sas_fabric.MAX_FABRIC_PATH_NODES)
        traces = {trace.id: trace for trace in fabric.traces}
        for bay in range(bays):
            self.assertLessEqual(len(traces[f"bay:{bay}"].metrics["path_states"]), sas_fabric.MAX_FABRIC_PATH_MEMBERS_PER_BAY)
        self.assertEqual(sum("path limit" in warning for warning in fabric.warnings), 1)

    async def test_many_bays_with_many_distinct_paths_stay_within_fabric_limits(self):
        from app.services import sas_fabric
        system, snapshot, outputs = await self.fixture()
        bays, paths = 40, 16
        for multipath in (False, True):
            with self.subTest(multipath=multipath):
                scaled, keys, cam = self.scaled_snapshot(snapshot, bays=bays, paths=paths, multipath=multipath)
                fabric = self.build(system, scaled, {**outputs, "camcontrol devlist -v": cam})
                self.assert_within_limits(fabric, bays=bays)
                traces = {trace.id: trace for trace in fabric.traces}
                for bay in range(bays):
                    metrics = traces[f"bay:{bay}"].metrics
                    self.assertEqual(len(metrics["path_states"]), sas_fabric.MAX_FABRIC_PATH_MEMBERS_PER_BAY)
                    self.assertIn("path_states", metrics["not_read"])
                    self.assertEqual(metrics["not_read_reasons"]["path_states"], "path limit reached")
                rows = [row for trace in fabric.traces if trace.kind == "bay" for row in trace.metrics["path_states"]]
                # The total includes the controllers mprutil itself reported.
                self.assertEqual(sum(node.kind == "controller" for node in fabric.nodes), sas_fabric.MAX_FABRIC_CONTROLLERS)
                named = [row for row in rows if row["controller"]]
                self.assertEqual(len({row["controller"] for row in named}), sas_fabric.MAX_FABRIC_CONTROLLERS - 2)
                limited = [row for row in rows if (row.get("not_read_reasons") or {}).get("controller") == "controller limit reached"]
                self.assertEqual(len(limited), len(rows) - len(named))
                self.assertEqual(len(rows), bays * sas_fabric.MAX_FABRIC_PATH_MEMBERS_PER_BAY)
                self.assertTrue(all(row["controller"] is None and "controller" in row["not_read"] for row in limited))
                self.assertLess(len(fabric.model_dump_json()), 2_000_000)

    async def test_distinct_multipath_states_cannot_exceed_the_path_node_limit(self):
        from app.services import sas_fabric
        system, snapshot, outputs = await self.fixture()
        bays, paths = 400, 8
        scaled, keys, cam = self.scaled_snapshot(snapshot, bays=bays, paths=paths, multipath=True, state=lambda n: f"state{n}")
        # Reuse a few controllers so the controller limit is not what binds.
        for slot in scaled.slots:
            slot.multipath.members = [
                member.model_copy(update={"controller_label": f"hba{index}"}) for index, member in enumerate(slot.multipath.members)
            ]
        fabric = self.build(system, scaled, {**outputs, "camcontrol devlist -v": cam})
        self.assert_within_limits(fabric, bays=bays)
        self.assertEqual(sum(node.id.startswith("path:") for node in fabric.nodes), sas_fabric.MAX_FABRIC_PATH_NODES)
        limited = [trace for trace in fabric.traces if trace.kind == "bay" and "path_states" in (trace.metrics.get("not_read") or [])]
        self.assertTrue(limited)
        self.assertTrue(all(trace.metrics["not_read_reasons"]["path_states"] == "path limit reached" for trace in limited))

    async def test_two_and_four_path_bays_are_unchanged_by_the_limits(self):
        system, snapshot, outputs = await self.fixture()
        for paths in (2, 4):
            for multipath in (False, True):
                with self.subTest(paths=paths, multipath=multipath):
                    scaled, keys, cam = self.scaled_snapshot(snapshot, bays=3, paths=paths, multipath=multipath)
                    fabric = self.build(system, scaled, {**outputs, "camcontrol devlist -v": cam})
                    self.assertFalse([warning for warning in fabric.warnings if "limit" in warning])
                    for trace in (trace for trace in fabric.traces if trace.kind == "bay"):
                        self.assertEqual(len(trace.metrics["path_states"]), paths)
                        self.assertNotIn("not_read", trace.metrics)
                        self.assertNotIn("not_read_reasons", trace.metrics)
                        self.assertTrue(all("not_read_reasons" not in row for row in trace.metrics["path_states"]))
                        self.assertEqual(len({row["controller"] for row in trace.metrics["path_states"]}), paths)

    async def test_ownership_is_freed_with_its_snapshot_across_forced_and_rejected_builds(self):
        import gc
        import weakref

        system, snapshot, outputs = await self.fixture()
        service = self.inventory_service

        def live_ownership():
            gc.collect()
            return sum(isinstance(item, FabricDiskOwnership) for item in gc.get_objects())

        first = weakref.ref(snapshot)
        del snapshot
        baseline = None
        for round_ in range(6):
            # A forced, published build replaces the cached snapshot; a build
            # that is never published is discarded at once.
            published = await service.get_snapshot(force_refresh=True)
            self.assertIsNotNone(snapshot_fabric_disk_ownership(published))
            discarded = await service._build_snapshot(force_source_refresh=True)
            self.assertIsNotNone(snapshot_fabric_disk_ownership(discarded))
            del published, discarded
            count = live_ownership()
            baseline = count if baseline is None else baseline
            self.assertEqual(count, baseline, f"ownership grew on round {round_}")
        gc.collect()
        self.assertIsNone(first())
        self.assertLessEqual(baseline, 2)
        self.assertFalse([
            name for name, value in vars(service).items()
            if isinstance(value, dict) and any(isinstance(item, FabricDiskOwnership) for item in value.values())
        ])
        self.assertFalse([name for name, value in vars(sas_fabric).items() if isinstance(value, (dict, list, set)) and any(
            isinstance(item, FabricDiskOwnership) for item in (value.values() if isinstance(value, dict) else value)
        )])

    async def test_ownership_over_its_limit_admits_no_direct_peer_and_says_so(self):
        system, snapshot, outputs = await self.fixture()
        self.assertEqual(len(self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]), 2)
        keys = [{f"da{index}"} for index in range(sas_fabric.MAX_FABRIC_OWNERSHIP_DEVICE_KEYS)]
        at_limit = fabric_disk_ownership([*keys, {"da11", "da3"}, {"serial-only", "multipath/disk0"}])
        self.assertFalse(at_limit.over_limit)
        over = fabric_disk_ownership([*keys, {"da99999"}])
        self.assertTrue(over.over_limit)
        self.assertIsNone(over.owners)
        # A name the peer rule could never match is not indexed at all.
        self.assertNotIn("da123456", fabric_disk_ownership([{"da123456", "da1"}]).owners)
        changed = snapshot.model_copy(deep=True)
        attach_fabric_disk_ownership(changed, over)
        fabric = self.build(system, changed, outputs)
        bay = self.bay(fabric)
        self.assertEqual([row["device_name"] for row in bay.metrics["path_states"]], ["da11"])
        self.assertIn("alternate_paths", bay.metrics["not_read"])
        self.assertEqual(bay.metrics["not_read_reasons"]["alternate_paths"], "ownership limit reached")
        self.assertEqual(sum("ownership" in warning for warning in fabric.warnings), 1)
        self.assertNotIn("not_read", self.bay(self.build(system, snapshot, outputs)).metrics)

    async def test_other_bay_claim_of_own_direct_device_refuses_the_bay(self):
        system, snapshot, outputs = await self.fixture()
        self.assertEqual(len(self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]), 2)
        for claim in ("device_name", "smart_device_names", "raw_status", "multipath"):
            with self.subTest(claim=claim):
                changed = snapshot.model_copy(deep=True)
                other = changed.slots[1]
                if claim == "device_name":
                    other.device_name = "da11"
                elif claim == "smart_device_names":
                    other.smart_device_names = ["da11"]
                elif claim == "raw_status":
                    other.raw_status["device_names"] = ["da11"]
                else:
                    other.multipath = MultipathView(name="other", device_name="multipath/other", members=[
                        MultipathMember(device_name="da11", controller_label="mpr1", state="ACTIVE"),
                    ])
                bay = self.bay(self.build(system, changed, outputs))
                self.assertEqual(bay.metrics["path_states"], [])
                self.assertEqual(bay.metrics["mpr_devices"], [])

    async def test_missing_or_stale_usage_hint_keeps_both_states_not_read(self):
        system, snapshot, outputs = await self.fixture()
        for hint in (None, "da99"):
            with self.subTest(hint=hint):
                changed = snapshot.model_copy(deep=True)
                changed.slots[0].raw_status.pop("pool_device_name", None)
                if hint is not None:
                    changed.slots[0].raw_status["pool_device_name"] = hint
                fabric = self.build(system, changed, outputs)
                bay = self.bay(fabric)
                self.assertEqual(len(bay.metrics["path_states"]), 2)
                self.assertEqual({row["state"] for row in bay.metrics["path_states"]}, {"unknown"})
                self.assertTrue(all("state" in row["not_read"] for row in bay.metrics["path_states"]))
                self.assertEqual({row["state"] for row in bay.metrics["mpr_devices"]}, {"unknown"})
                self.assertEqual({row["state"] for row in fabric.paths}, {"unknown"})

    async def test_normal_usage_hint_selects_only_the_observed_path(self):
        system, snapshot, outputs = await self.fixture()
        for hint in ("da3", "da11"):
            with self.subTest(hint=hint):
                snapshot.slots[0].raw_status["pool_device_name"] = hint
                rows = self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]
                self.assertEqual({row["device_name"]: row["state"] for row in rows}, {
                    name: "active" if name == hint else "standby" for name in ("da3", "da11")
                })
                self.assertTrue(all("not_read" not in row for row in rows))

    async def test_single_direct_path_state_requires_its_own_usage_hint(self):
        system, snapshot, outputs = await self.fixture()
        snapshot.slots[0].raw_status["device_names"] = ["da11"]
        for hint in ("da11", None, "da99"):
            with self.subTest(hint=hint):
                snapshot.slots[0].raw_status["pool_device_name"] = hint
                bay = self.bay(self.build(system, snapshot, outputs))
                self.assertEqual(len(bay.metrics["path_states"]), 1)
                self.assertEqual(len(bay.metrics["mpr_devices"]), 1)
                self.assertEqual(bay.metrics["path_states"][0]["state"], "active" if hint == "da11" else "unknown")

    async def test_mixed_case_glabel_retains_the_observed_pool_path_hint(self):
        system, snapshot, outputs = await self.fixture(pool_label="gptid/SYNTHETIC-Mixed-Label")
        self.assertEqual(snapshot.slots[0].raw_status.get("pool_device_name"), "da11")
        rows = self.bay(self.build(system, snapshot, outputs)).metrics["path_states"]
        self.assertEqual({row["device_name"]: row["state"] for row in rows}, {"da3": "standby", "da11": "active"})

    async def test_cam_targets_never_pair_across_controllers(self):
        system, snapshot, outputs = await self.fixture()
        cam = parse_camcontrol_devlist(outputs["camcontrol devlist -v"])
        # Same model/LUN, different targets. The opposite controller cannot
        # supply this device's target even if its MPR table has that number.
        for controller, device, target, other in (("mpr0", "da3", 4, "mpr1"), ("mpr1", "da11", 12, "mpr0")):
            with self.subTest(controller=controller):
                self.assertEqual(_cam_member_target(cam, controller, device), target)
                self.assertIsNone(_cam_member_target(cam, other, device))
        wrong_target = dict(outputs)
        wrong_target["mprutil -u 1 show devices"] = outputs["mprutil -u 1 show devices"].replace("00 12 ", "00 4 ")
        bay = self.bay(self.build(system, snapshot, wrong_target))
        self.assertEqual({row["controller"] for row in bay.metrics["path_states"]}, {"mpr0", "mpr1"})
        self.assertEqual([row["member_device_name"] for row in bay.metrics["mpr_devices"]], ["da3"])
        normal = self.bay(self.build(system, snapshot, outputs))
        self.assertEqual({row["member_device_name"] for row in normal.metrics["mpr_devices"]}, {"da3", "da11"})

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
        for field, value in (
            ("ssh_ses_targets", []), ("physical_location_known", False), ("present", False),
            ("presence_known", False),
        ):
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
        exporter = SnapshotExportService(Settings(), FakeHistoryBackend(), templates)
        for hint in ("da11", None, "da99"):
            with self.subTest(hint=hint):
                snapshot.slots[0].raw_status["pool_device_name"] = hint
                fabric = self.build(system, snapshot, outputs)
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
                self.assertEqual(bay["metrics"], self.bay(fabric).model_dump(mode="json")["metrics"])
                if hint != "da11":
                    self.assertEqual({row["state"] for row in bay["metrics"]["path_states"]}, {"unknown"})
                    self.assertTrue(all("state" in row["not_read"] for row in bay["metrics"]["path_states"]))


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
