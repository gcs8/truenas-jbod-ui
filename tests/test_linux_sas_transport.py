"""Synthetic Linux SAS transport coverage. No appliance access."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot

KEY = "sas transport sysfs"
HOST = "5" + "0" * 14 + "1"
FRONT = "5" + "0" * 13 + "10"
REAR = "5" + "0" * 13 + "20"
DISK = "5" + "0" * 13 + "30"
CLASSES = ("sas_host", "sas_expander", "sas_port", "sas_phy", "sas_device", "sas_end_device")
COUNTERS = (
    "invalid_dword_count",
    "running_disparity_error_count",
    "loss_of_dword_sync_count",
    "phy_reset_problem_count",
)


class SyntheticSasTree:
    """Synthetic mpt3sas-shaped class links, downstream ports and targets."""

    def __init__(self, root: Path):
        self.root = root
        for name in (*CLASSES, "scsi_host"):
            (root / "class" / name).mkdir(parents=True)
        self.host = root / "devices/pci0000:00/0000:00:01.0/host2"
        self.front = self.host / "port-2:0/expander-2:0"
        self.rear = self.front / "port-2:0:1/expander-2:1"
        self.disk = self.rear / "port-2:1:2/end_device-2:1:2"
        self.add("sas_host", self.host)
        self.add(
            "scsi_host",
            self.host,
            proc_name="mpt3sas",
            board_name="Synthetic HBA",
            version_fw="1.2.3",
            host_sas_address="0x" + HOST,
        )
        for obj, address in ((self.front, FRONT), (self.rear, REAR), (self.disk, DISK)):
            self.add(
                "sas_device",
                obj,
                sas_address="0x" + address,
                phy_identifier="0",
                device_type="end device" if obj == self.disk else "edge expander",
                bay_identifier="7",
                enclosure_identifier="0x" + REAR,
            )
        self.add(
            "sas_expander", self.front, vendor_id="Synthetic", product_id="SAS switch A", product_rev="0001", level="1"
        )
        self.add(
            "sas_expander", self.rear, vendor_id="Synthetic", product_id="SAS switch B", product_rev="0002", level="1"
        )
        self.add("sas_end_device", self.disk)
        (self.disk / "target2:0:0/2:0:0:0/block/sdz").mkdir(parents=True)
        self.port(self.host / "port-2:0", self.front, [self.phy(self.host, f"phy-2:{i}", i, HOST) for i in range(2)])
        # Incoming PHYs have no invented reverse sas_port toward the parent.
        for i in range(2):
            self.phy(self.front, f"phy-2:0:{i}", i, FRONT)
        self.port(
            self.front / "port-2:0:1", self.rear, [self.phy(self.front, f"phy-2:0:{i}", i, FRONT) for i in (2, 3)]
        )
        for i in range(2):
            self.phy(self.rear, f"phy-2:1:{i}", i, REAR)
        self.port(self.rear / "port-2:1:2", self.disk, [self.phy(self.rear, "phy-2:1:7", 7, REAR, invalid="9")])
        self.phy(self.rear, "phy-2:1:8", 8, REAR, rate="Unknown", invalid="Invalid argument")

    def add(self, cls, obj, **attrs):
        directory = obj / cls / obj.name
        directory.mkdir(parents=True, exist_ok=True)
        (self.root / "class" / cls / obj.name).symlink_to(directory)
        (directory / "device").symlink_to(obj)
        for key, value in attrs.items():
            (directory / key).write_text(value + "\n")
        return obj

    def phy(self, owner, name, number, address, *, rate="12.0 Gbit", invalid="0"):
        obj = owner / name
        self.add(
            "sas_phy",
            obj,
            phy_identifier=str(number),
            negotiated_linkrate=rate,
            minimum_linkrate="1.5 Gbit",
            maximum_linkrate="12.0 Gbit",
            sas_address="0x" + address,
            **dict(zip(COUNTERS, (invalid, "0", "0", "0"))),
        )
        return obj

    def port(self, obj, remote, phys):
        self.add("sas_port", obj, num_phys=str(len(phys)))
        for phy in phys:
            (obj / phy.name).symlink_to(phy)
            (phy / "port").symlink_to(obj)
        if remote.parent != obj:
            raise ValueError("Only observed downstream nesting is modelled")


def shared_port_survey(payload, disks=16, phys=64):
    """Invented adversarial shape: many end devices share one wide port."""
    payload = deepcopy(payload)
    rows = payload["rows"]
    end = next(row for row in rows if row["kind"] == "sas_end_device")
    phy = next(row for row in rows if row["kind"] == "sas_phy" and row["name"] == "phy-2:1:7")
    device = next(row for row in rows if row["kind"] == "sas_device" and row["name"].startswith("end_device"))
    port = next(row for row in rows if row["kind"] == "sas_port" and row["name"] == "port-2:1:2")
    owner = port["path"].rsplit("/", 1)[0]
    payload["rows"] = [
        row
        for row in rows
        if row["kind"] != "sas_end_device"
        and not (row["kind"] == "sas_device" and row["name"].startswith("end_device"))
        and not (row["kind"] == "sas_phy" and row["path"].rsplit("/", 1)[0] == owner)
    ]
    port["attrs"]["num_phys"] = str(phys)
    port["links"] = {}
    slots = []
    for number in range(phys):
        row = deepcopy(phy)
        row["name"] = f"phy-2:1:{number}"
        row["path"] = owner + "/" + row["name"]
        row["attrs"]["phy_identifier"] = str(number)
        row["links"] = {"port": port["path"]}
        port["links"][row["name"]] = row["path"]
        payload["rows"].append(row)
    for number in range(disks):
        row = deepcopy(end)
        row["name"] = f"end_device-2:1:{number}"
        row["path"] = port["path"] + "/" + row["name"]
        block = f"sdz{number}"
        row["links"] = {block: row["path"] + "/target2:0:0/2:0:0:0/block/" + block}
        port["links"][row["name"]] = row["path"]
        detail = deepcopy(device)
        detail.update(name=row["name"], path=row["path"])
        payload["rows"].extend((row, detail))
        slots.append(
            SlotView(
                slot=number,
                slot_label=str(number),
                row_index=0,
                column_index=number,
                present=True,
                device_name=block,
                ssh_ses_device="/dev/sg9",
            )
        )
    return payload, slots


def port_phys(result, metric):
    ref = result.raw["sas_transport_port_index"][metric["sas_transport_port_id"]]
    owner = next(node for node in result.nodes if node.id == ref["owner_id"])
    return [row for row in owner.raw.get("phys", owner.raw.get("phy_rows", [])) if row["name"] in ref["phy_names"]]


def fabric(output: str):
    slot = SlotView(
        slot=0,
        slot_label="00",
        row_index=0,
        column_index=0,
        device_name="sdz",
        present=True,
        ssh_ses_device="/dev/sg9",
        sg_device="/dev/sg4",
        scsi_hctl="2:0:0:0",
        transport_protocol="sas",
        target_port_protocol="SSP",
        enclosure_id=REAR,
    )
    return build_sas_fabric_snapshot(
        system=SystemConfig(id="synthetic-scale", label="Synthetic SCALE", truenas=TrueNASConfig(platform="scale")),
        snapshot=InventorySnapshot(slots=[slot], refresh_interval_seconds=30, selected_enclosure_id=REAR),
        ssh_outputs={KEY: output},
    )


class LinuxSasMissingFeatureTests(unittest.TestCase):
    def test_scale_builder_admits_transport_host_instead_of_synthetic_controller(self):
        # This wire response is invented, not copied from any survey.
        output = json.dumps(
            {
                "version": 1,
                "complete": True,
                "rows": [
                    {
                        "kind": "sas_host",
                        "name": "host2",
                        "path": "/sys/devices/pci0000:00/0000:00:01.0/host2",
                        "attrs": {},
                        "links": {},
                    },
                    {
                        "kind": "scsi_host",
                        "name": "host2",
                        "path": "/sys/devices/pci0000:00/0000:00:01.0/host2",
                        "attrs": {"board_name": "Synthetic HBA", "host_sas_address": "0x" + HOST},
                        "links": {},
                    },
                ],
            }
        )
        result = fabric(output)
        self.assertIn("controller:host2", {node.id for node in result.nodes})
        self.assertFalse(any("HBA and expander hop detail is not exposed" in warning for warning in result.warnings))

    def test_missing_transport_keeps_linux_ses_warning(self):
        result = fabric("")
        self.assertIn("controller:linux-ses", {node.id for node in result.nodes})
        self.assertTrue(any("HBA and expander hop detail is not exposed" in warning for warning in result.warnings))


def run_survey(root: Path, replacements: dict[str, str] | None = None) -> dict:
    from app.services.linux_sas_transport_read import SAS_TRANSPORT_SCRIPT

    script = SAS_TRANSPORT_SCRIPT.replace('ROOT = "/sys"', "ROOT = " + repr(str(root)))
    for old, new in (replacements or {}).items():
        script = script.replace(old, new)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=20, check=True
    )
    return json.loads(result.stdout)


class LinuxSasTransportTreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "sys"
        self.tree = SyntheticSasTree(self.root)
        self.payload = run_survey(self.root)
        self.assertTrue(self.payload["complete"], self.payload)

    def test_exact_script_collects_all_classes_and_end_device_block(self):
        from app.services.linux_sas_transport import parse_sas_transport

        data = parse_sas_transport(json.dumps(self.payload))
        self.assertTrue(data.complete, data.reason)
        self.assertEqual({row.kind for row in data.rows}, {*CLASSES, "scsi_host"})
        self.assertEqual(data.of_kind("sas_end_device")[0].links.keys(), {"sdz"})
        self.assertEqual(data.of_kind("scsi_host")[0].attrs["board_name"], "Synthetic HBA")
        disk = next(row for row in data.of_kind("sas_device") if row.name.startswith("end_device"))
        self.assertEqual((disk.bay_identifier, disk.enclosure_identifier), (7, REAR))

    def test_tree_builds_controller_cascade_and_bay_trace(self):
        result = fabric(json.dumps(self.payload))
        self.assertEqual(len(result.expanders), 2)
        self.assertIs(result.raw["sas_transport_available"], True)
        self.assertEqual(result.controllers[0]["board"], "Synthetic HBA")
        self.assertEqual(len(result.controllers[0]["phy_rows"]), 2)
        self.assertEqual([row["sas_level"] for row in result.expanders], [1, 2])
        cascade = next(link for link in result.links if link.kind == "expander-expander")
        self.assertEqual(cascade.reported_lanes.lane_count, 2)
        self.assertEqual(cascade.reported_lanes.source_phys, [2, 3])
        self.assertEqual(cascade.reported_lanes.target_phys, [])
        self.assertEqual(cascade.reported_lanes.basis, "sysfs_parent_port_membership")
        self.assertEqual(cascade.reported_lanes.attached_sas_address, REAR)
        self.assertEqual(cascade.reported_lanes.negotiated_rate_gbps, 12)
        self.assertEqual(cascade.reported_lanes.lanes, [])
        trace = next(trace for trace in result.traces if trace.id == "bay:0")
        ports = trace.metrics["sas_transport_ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["member_device_name"], "sdz")
        self.assertEqual(ports[0]["bay_identifier"], 7)  # not misused as a slot offset
        self.assertEqual(ports[0]["sas_address"], DISK)
        self.assertEqual(port_phys(result, ports[0])[0]["invalid_dword_count"], 9)
        self.assertEqual(len(result.paths), 1)
        self.assertEqual(result.paths[0]["state"], "unknown")
        self.assertIn(ports[0]["path_id"], trace.node_ids)
        self.assertEqual(len(ports[0]["expander_ids"]), 2)
        self.assertIn(cascade.id, trace.link_ids)
        node_ids = {node.id for node in result.nodes}
        link_ids = {link.id for link in result.links}
        for item in result.traces:
            self.assertTrue(set(item.node_ids) <= node_ids)
            self.assertTrue(set(item.link_ids) <= link_ids)
        self.assertNotIn("controller:linux-ses", node_ids)

    def test_port_census_includes_cables_and_disks_outside_selected_bays(self):
        result = fabric(json.dumps(self.payload))
        census = result.raw.get("sas_transport_ports", [])
        self.assertEqual(len(census), 3, "all ports, not only the one selected disk's port")
        by_id = {row["id"]: row for row in census}
        cable = by_id["sas-port:port-2:0:1"]
        self.assertEqual(cable["owner_id"], result.expanders[0]["id"])
        self.assertEqual(cable["remote_id"], result.expanders[1]["id"])
        self.assertEqual(cable["remote_kind"], "expander")
        self.assertEqual(cable["phys"], [2, 3])
        self.assertEqual(cable["phy_names"], ["phy-2:0:2", "phy-2:0:3"])
        self.assertEqual(cable["state"], "read")
        disk = by_id["sas-port:port-2:1:2"]
        self.assertEqual((disk["remote_kind"], disk["has_block"]), ("end_device", True))
        self.assertEqual((disk["bay_identifier"], disk["enclosure_identifier"]), (7, REAR))
        self.assertEqual(disk["phys"], [7])
        # No selected primary block binding: the host-wide census is still the
        # same evidence, not invented from selected bay ordinals.
        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0,
                        present=True, device_name="unjoined", ssh_ses_device="/dev/sg9", enclosure_id=REAR)
        unjoined = self.selection_fabric("scale", [slot], json.dumps(self.payload))
        self.assertEqual(unjoined.raw.get("sas_transport_ports"), census)
        self.assertEqual(unjoined.raw["sas_transport_port_index"], {})
        reversed_payload = deepcopy(self.payload)
        reversed_payload["rows"].reverse()
        self.assertEqual(fabric(json.dumps(reversed_payload)).raw.get("sas_transport_ports"), census)

    def test_census_refuses_ambiguous_target_and_incoherent_phy_membership(self):
        for mutation in ("wrong_remote", "two_remotes", "missing_phy", "foreign_owner"):
            payload = deepcopy(self.payload)
            port = next(row for row in payload["rows"] if row["kind"] == "sas_port" and row["name"] == "port-2:1:2")
            if mutation == "wrong_remote":
                port["links"]["end_device-2:1:2"] = port["path"] + "/end_device-2:1:9"
            elif mutation == "two_remotes":
                port["links"]["end_device-2:1:9"] = port["path"] + "/end_device-2:1:9"
            elif mutation == "missing_phy":
                port["links"].pop("phy-2:1:7")
            else:
                port["path"] = port["path"].replace("expander-2:1", "expander-2:9")
            with self.subTest(mutation=mutation):
                census = fabric(json.dumps(payload)).raw.get("sas_transport_ports", [])
                self.assertEqual(len(census), 3)
                row = next(row for row in census if row["id"] == "sas-port:port-2:1:2")
                if mutation in {"wrong_remote", "two_remotes"}:
                    self.assertEqual(row["remote_kind"], "not_read")
                    self.assertIsNone(row["enclosure_identifier"])
                    self.assertIsNone(row["has_block"])
                else:
                    self.assertEqual(row["state"], "not_read")
                    self.assertEqual(row["phys"], [])

    def test_sysfs_far_end_basis_roundtrips_as_not_read_without_invented_phys(self):
        from app.models.domain import SasFabricSnapshot

        result = fabric(json.dumps(self.payload))
        for link in result.links:
            if link.kind not in {"controller-expander", "expander-expander"}:
                continue
            self.assertIsNotNone(link.reported_lanes)
            assert link.reported_lanes is not None
            value = link.reported_lanes.model_dump()
            self.assertEqual(value.get("target_basis"), "not_read")
            self.assertEqual(value["target_phys"], [])
            self.assertEqual(value["lanes"], [])
        again = SasFabricSnapshot.model_validate_json(result.model_dump_json())
        self.assertEqual(again.raw.get("sas_transport_ports"), result.raw.get("sas_transport_ports"))
        self.assertEqual([link.reported_lanes for link in again.links], [link.reported_lanes for link in result.links])

    def test_unknown_and_einval_are_not_zero_but_measured_zero_is_kept(self):
        from app.services.linux_sas_transport import parse_sas_transport

        script_replacements = {
            "value = stream.read(MAX_VALUE + 1)": 'value = stream.read(MAX_VALUE + 1)\n            if value.startswith(b"Invalid argument"):\n                raise OSError(22, "Invalid argument")'
        }
        payload = run_survey(self.root, script_replacements)
        data = parse_sas_transport(json.dumps(payload))
        linked = next(row for row in data.of_kind("sas_phy") if row.name == "phy-2:1:7")
        unlinked = next(row for row in data.of_kind("sas_phy") if row.name == "phy-2:1:8")
        self.assertEqual(linked.counters["phy_reset_problem_count"], 0)
        self.assertIsNone(unlinked.negotiated_rate_gbps)
        self.assertIsNone(unlinked.counters["invalid_dword_count"])
        self.assertIn("invalid_dword_count", unlinked.not_read)
        rear = next(row for row in fabric(json.dumps(payload)).expanders if row["sas_level"] == 2)
        unlinked_phy = next(row for row in rear["phys"] if row["phy"] == 8)
        self.assertEqual(unlinked_phy["counter_state"], "not_read")
        self.assertIsNone(unlinked_phy["invalid_dword_count"])

    def test_missing_counter_and_rate_do_not_become_zero(self):
        entry = self.root / "class/sas_phy/phy-2:1:7"
        (entry / "running_disparity_error_count").unlink()
        (entry / "maximum_linkrate").write_text("Unknown\n")
        result = fabric(json.dumps(run_survey(self.root)))
        rear = next(row for row in result.expanders if row["sas_level"] == 2)
        phy = next(row for row in rear["phys"] if row["phy"] == 7)
        self.assertIsNone(phy["running_disparity_error_count"])
        self.assertIsNone(phy["maximum_rate_gbps"])
        self.assertEqual(phy["invalid_dword_count"], 9)

    def test_limits_refuse_whole_survey_without_truncated_topology(self):
        for old, new in (
            ("MAX_ROWS = 4096", "MAX_ROWS = 2"),
            ("MAX_DIRECTORY = 2048", "MAX_DIRECTORY = 1"),
            ("MAX_VISITS = 32768", "MAX_VISITS = 3"),
            ("MAX_BYTES = 2 * 1024 * 1024", "MAX_BYTES = 64"),
        ):
            with self.subTest(old=old):
                payload = run_survey(self.root, {old: new})
                self.assertFalse(payload["complete"])
                self.assertEqual(payload["rows"], [])
                result = fabric(json.dumps(payload))
                self.assertEqual(result.expanders, [])
                self.assertTrue(any("not exposed" in text for text in result.warnings))

    def test_parent_port_reports_cascade_lanes_without_invented_reverse_ports(self):
        from app.services.linux_sas_transport import parse_sas_transport

        data = parse_sas_transport(json.dumps(self.payload))
        self.assertEqual(len(data.of_kind("sas_port")), 3)  # HBA + cascade + disk
        self.assertFalse(
            any(
                "host2" in row.links or "expander-2:0" in row.links
                for row in data.of_kind("sas_port")
                if row.name != "port-2:0"
            )
        )
        result = fabric(json.dumps(self.payload))
        for kind, address, phys in (("controller-expander", FRONT, [0, 1]), ("expander-expander", REAR, [2, 3])):
            link = next(link for link in result.links if link.kind == kind)
            self.assertIsNotNone(link.reported_lanes)
            lanes = link.reported_lanes
            assert lanes is not None
            self.assertEqual(lanes.basis, "sysfs_parent_port_membership")
            self.assertEqual(lanes.lane_count, 2)
            self.assertEqual(lanes.source_phys, phys)
            self.assertEqual(lanes.target_phys, [])  # not read, never guessed
            self.assertEqual(lanes.lanes, [])
            self.assertEqual(lanes.attached_sas_address, address)
            self.assertEqual(lanes.negotiated_rate_gbps, 12)

    def test_contradictory_parent_port_does_not_report_lane_metadata(self):
        for mutation in ("foreign_owner", "wrong_port", "duplicate_phy", "missing_phy"):
            with self.subTest(mutation=mutation):
                payload = deepcopy(self.payload)
                port = next(row for row in payload["rows"] if row["kind"] == "sas_port" and row["name"] == "port-2:0:1")
                phy = next(row for row in payload["rows"] if row["kind"] == "sas_phy" and row["name"] == "phy-2:0:2")
                if mutation == "foreign_owner":
                    phy["path"] = phy["path"].replace("/expander-2:0/", "/expander-2:9/")
                    port["links"][phy["name"]] = phy["path"]
                elif mutation == "wrong_port":
                    phy["links"]["port"] = port["path"].replace("port-2:0:1", "port-2:0:9")
                elif mutation == "duplicate_phy":
                    phy["attrs"]["phy_identifier"] = "3"
                else:
                    payload["rows"].remove(phy)
                link = next(link for link in fabric(json.dumps(payload)).links if link.kind == "expander-expander")
                self.assertIsNone(link.reported_lanes)

    def test_control_character_is_not_read_only_for_the_affected_attribute(self):
        from app.services.linux_sas_transport import parse_sas_transport

        payload = deepcopy(self.payload)
        host = next(row for row in payload["rows"] if row["kind"] == "scsi_host")
        host["attrs"]["board_name"] = "Synthetic\x01HBA"
        data = parse_sas_transport(json.dumps(payload))
        self.assertTrue(data.complete, data.reason)
        decoded = data.of_kind("scsi_host")[0]
        self.assertIsNone(decoded.attrs["board_name"])
        self.assertIn("board_name", decoded.not_read)
        self.assertEqual(decoded.attrs["proc_name"], "mpt3sas")
        self.assertEqual(host["attrs"]["board_name"], "Synthetic\x01HBA")  # input unchanged
        result = fabric(json.dumps(payload))
        self.assertIsNone(result.controllers[0]["board"])
        self.assertEqual(len(result.expanders), 2)
        self.assertEqual(
            len(next(trace for trace in result.traces if trace.id == "bay:0").metrics["sas_transport_ports"]), 1
        )

    def test_cumulative_attribute_read_cap_is_independent_of_output_cap(self):
        payload = run_survey(self.root, {"read_bytes = 0": "read_bytes = MAX_BYTES - 1"})
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["reason"], "byte_budget")
        self.assertEqual(payload["rows"], [])

    def test_running_output_cap_is_independent_of_final_encoded_size(self):
        payload = run_survey(self.root, {"output_bytes = 0": "output_bytes = MAX_BYTES"})
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["reason"], "output_budget")
        self.assertEqual(payload["rows"], [])

    def test_final_encoded_output_cap_checks_envelope_not_just_rows(self):
        payload = run_survey(
            self.root,
            {
                'return {"version": 1, "complete": True, "rows": rows}': 'return {"version": 1, "complete": True, "rows": rows, "padding": " " * MAX_BYTES}'
            },
        )
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["reason"], "output_budget")
        self.assertEqual(payload["rows"], [])

    def test_parser_byte_limit_refuses_otherwise_valid_json(self):
        from app.services.linux_sas_transport import MAX_TRANSPORT_BYTES, parse_sas_transport

        self.assertTrue(parse_sas_transport(json.dumps(self.payload)).complete)
        for padding in (" " * MAX_TRANSPORT_BYTES, "\u00e9" * (MAX_TRANSPORT_BYTES // 2)):
            payload = {**self.payload, "padding": padding}
            output = json.dumps(payload, ensure_ascii=False)
            self.assertGreater(len(output.encode("utf-8")), MAX_TRANSPORT_BYTES)
            if padding.startswith("\u00e9"):
                self.assertLess(len(output), MAX_TRANSPORT_BYTES)
            self.assertIsInstance(json.loads(output), dict)
            decoded = parse_sas_transport(output)
            self.assertFalse(decoded.complete)
            self.assertEqual(decoded.reason, "byte_budget")

    def test_unmatched_bay_warnings_are_one_bounded_build_summary(self):
        slots = [
            SlotView(
                slot=i,
                slot_label=str(i),
                row_index=0,
                column_index=i,
                device_name=f"dm-{i}",
                present=True,
                ssh_ses_device="/dev/sg9",
                enclosure_id=REAR,
            )
            for i in range(128)
        ]
        result = build_sas_fabric_snapshot(
            system=SystemConfig(truenas=TrueNASConfig(platform="scale")),
            snapshot=InventorySnapshot(slots=slots, refresh_interval_seconds=30, selected_enclosure_id=REAR),
            ssh_outputs={KEY: json.dumps(self.payload)},
        )
        summary = [warning for warning in result.warnings if "end-device port" in warning]
        self.assertEqual(len(summary), 1)
        self.assertIn("128", summary[0])
        self.assertLess(len(summary[0]), 256)
        enclosure = next(node for node in result.nodes if node.kind == "ses-enclosure")
        self.assertIsNone(enclosure.controller_id)
        self.assertEqual(enclosure.raw["controller_ids"], [])
        self.assertEqual(enclosure.related_slots, list(range(128)))

    def test_no_reciprocal_backlink_keeps_parent_but_not_reported_lanes(self):
        payload = deepcopy(self.payload)
        # Without a reverse port, a missing parent-side membership is still
        # not read. The positive no-backlink case is covered separately.
        for row in payload["rows"]:
            if row["kind"] == "sas_port" and row["name"] == "port-2:0:1":
                row["links"].pop("phy-2:0:2")
        result = fabric(json.dumps(payload))
        cascade = next(link for link in result.links if link.kind == "expander-expander")
        self.assertIsNone(cascade.reported_lanes)

    def test_missing_phy_membership_and_conflicting_counts_are_not_lanes(self):
        for change in ("count", "membership"):
            payload = deepcopy(self.payload)
            port = next(row for row in payload["rows"] if row["kind"] == "sas_port" and row["name"] == "port-2:0:1")
            if change == "count":
                port["attrs"]["num_phys"] = "4"
            else:
                port["links"].pop("phy-2:0:2")
            cascade = next(link for link in fabric(json.dumps(payload)).links if link.kind == "expander-expander")
            self.assertIsNone(cascade.reported_lanes)

    def test_mixed_rates_are_retained_per_phy_not_summarized_as_uniform(self):
        payload = deepcopy(self.payload)
        row = next(row for row in payload["rows"] if row["kind"] == "sas_phy" and row["name"] == "phy-2:0:3")
        row["attrs"]["negotiated_linkrate"] = "6.0 Gbit"
        result = fabric(json.dumps(payload))
        cascade = next(link for link in result.links if link.kind == "expander-expander")
        self.assertEqual(cascade.reported_lanes.lane_count, 2)
        self.assertIsNone(cascade.reported_lanes.negotiated_rate_gbps)
        front = next(row for row in result.expanders if row["sas_level"] == 1)
        self.assertEqual(next(row for row in front["phys"] if row["phy"] == 3)["negotiated_rate_gbps"], 6)

    def test_no_block_identity_never_joins_by_bay_or_sas_address(self):
        payload = deepcopy(self.payload)
        next(row for row in payload["rows"] if row["kind"] == "sas_end_device")["links"] = {}
        result = fabric(json.dumps(payload))
        trace = next(trace for trace in result.traces if trace.id == "bay:0")
        self.assertEqual(trace.metrics["sas_transport_ports"], [])
        self.assertFalse(any(link.kind == "expander-ses-enclosure" for link in result.links))

    def test_ninety_phy_census_is_bounded_and_does_not_modify_fixture(self):
        # Invented 8 + 50 + 32 PHY inventory, including unlinked lanes.
        for owner, prefix, start, stop, address in (
            (self.tree.host, "phy-2:", 2, 8, HOST),
            (self.tree.front, "phy-2:0:", 4, 50, FRONT),
            (self.tree.rear, "phy-2:1:", 2, 32, REAR),
        ):
            for number in range(start, stop):
                if number in (7, 8) and owner == self.tree.rear:
                    continue
                self.tree.phy(owner, prefix + str(number), number, address, rate="Unknown", invalid="Invalid argument")
        before = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        payload = run_survey(self.root)
        after = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        self.assertEqual(before, after)
        self.assertTrue(payload["complete"])
        self.assertEqual(sum(row["kind"] == "sas_phy" for row in payload["rows"]), 90)
        self.assertLess(len(json.dumps(payload).encode()), 2 * 1024 * 1024)

    def test_attribute_permission_denied_is_not_measured_zero(self):
        from app.services.linux_sas_transport import parse_sas_transport

        payload = run_survey(
            self.root,
            {
                "fd = os.open(path,": 'if path.endswith("/invalid_dword_count"):\n            raise PermissionError(13, "Permission denied")\n        fd = os.open(path,'
            },
        )
        data = parse_sas_transport(json.dumps(payload))
        self.assertTrue(data.complete)
        self.assertTrue(all(row.counters["invalid_dword_count"] is None for row in data.of_kind("sas_phy")))
        self.assertTrue(all(row.counters["phy_reset_problem_count"] == 0 for row in data.of_kind("sas_phy")))

    def test_missing_upstream_port_never_invents_parent_or_descendant_chain(self):
        payload = deepcopy(self.payload)
        payload["rows"] = [
            row for row in payload["rows"] if not (row["kind"] == "sas_port" and row["name"] == "port-2:0")
        ]
        result = fabric(json.dumps(payload))
        self.assertFalse(any(link.kind in {"controller-expander", "expander-expander"} for link in result.links))
        self.assertTrue(all(row["parent_link_state"] == "unknown" for row in result.expanders))
        self.assertEqual(
            next(trace for trace in result.traces if trace.id == "bay:0").metrics["sas_transport_ports"], []
        )

    def test_duplicate_block_claims_cannot_choose_an_end_device(self):
        payload = deepcopy(self.payload)
        row = deepcopy(next(row for row in payload["rows"] if row["kind"] == "sas_end_device"))
        row["name"] = "end_device-2:1:9"
        row["path"] = row["path"].replace("port-2:1:2/end_device-2:1:2", "port-2:1:9/end_device-2:1:9")
        payload["rows"].append(row)
        result = fabric(json.dumps(payload))
        self.assertEqual(
            next(trace for trace in result.traces if trace.id == "bay:0").metrics["sas_transport_ports"], []
        )

    def test_empty_sas_classes_preserve_ses_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "sys"
            (root / "class").mkdir(parents=True)
            payload = run_survey(root)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["rows"], [])
        self.assertEqual(fabric(json.dumps(payload)).expanders, [])
        self.assertTrue(any("not exposed" in warning for warning in fabric(json.dumps(payload)).warnings))

    def test_missing_ses_still_projects_block_identified_transport(self):
        from app.services.linux_sas_transport_read import SAS_TRANSPORT_COMMAND

        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="sdz", present=True)
        result = build_sas_fabric_snapshot(
            system=SystemConfig(truenas=TrueNASConfig(platform="scale")),
            snapshot=InventorySnapshot(slots=[slot], refresh_interval_seconds=30),
            ssh_outputs={SAS_TRANSPORT_COMMAND: json.dumps(self.payload)},
        )
        self.assertEqual(len(result.expanders), 2)
        self.assertTrue(any(link.kind == "transport-bay" for link in result.links))
        self.assertEqual(
            next(trace for trace in result.traces if trace.id == "bay:0").metrics["sas_transport_ports"][0][
                "sas_address"
            ],
            DISK,
        )

    def test_shared_port_projection_bounds_response_and_phy_allocations(self):
        from unittest.mock import patch
        from app.services import sas_fabric_linux_sas as projection

        payload, slots = shared_port_survey(self.payload)
        with patch.object(projection, "_phy", wraps=projection._phy) as allocate:
            result = self.selection_fabric("scale", slots, json.dumps(payload))
        encoded = result.model_dump_json().encode()
        self.assertLess(len(encoded), 350_000)
        self.assertLessEqual(allocate.call_count, sum(row["kind"] == "sas_phy" for row in payload["rows"]))
        refs = set()
        for trace in result.traces:
            if trace.kind != "bay":
                continue
            metric = trace.metrics["sas_transport_ports"][0]
            self.assertNotIn("expander_phys", metric)
            refs.add(metric["sas_transport_port_id"])
            self.assertEqual(len(port_phys(result, metric)), 64)
        self.assertEqual(len(refs), 1)
        self.assertEqual(len(result.raw["sas_transport_port_index"]), 1)
        again = self.selection_fabric("scale", slots, json.dumps(payload))
        self.assertEqual(result.raw["sas_transport_port_index"], again.raw["sas_transport_port_index"])

    def test_shared_port_phy_materialization_is_linear(self):
        from unittest.mock import patch
        from app.services import sas_fabric_linux_sas as projection

        payload, slots = shared_port_survey(self.payload, disks=8, phys=32)
        with patch.object(projection, "_phy", wraps=projection._phy) as allocate:
            self.selection_fabric("scale", slots, json.dumps(payload))
        self.assertEqual(allocate.call_count, sum(row["kind"] == "sas_phy" for row in payload["rows"]))

    def test_projection_fact_budget_boundary_preserves_fallback(self):
        from unittest.mock import patch
        from app.services import sas_fabric_linux_sas as projection

        payload, slots = shared_port_survey(self.payload, disks=3, phys=8)
        output = json.dumps(payload)
        control = self.selection_fabric("scale", slots, output)
        facts = control.raw.get("sas_transport_projected_facts", 0)
        self.assertGreater(facts, 0)
        baseline = self.selection_fabric("scale", slots, "")
        with patch.object(projection, "MAX_PROJECTED_TRANSPORT_FACTS", facts, create=True):
            exact = self.selection_fabric("scale", slots, output)
        self.assertIs(exact.raw["sas_transport_available"], True)
        with (
            patch.object(projection, "MAX_PROJECTED_TRANSPORT_FACTS", facts - 1, create=True),
            patch.object(projection, "_phy", wraps=projection._phy) as allocate,
        ):
            refused = self.selection_fabric("scale", slots, output)
        self.assertEqual(allocate.call_count, 0)  # refuse before expanding facts
        self.assertEqual(refused.raw.get("sas_transport_state"), "not_read")
        self.assertEqual(refused.raw.get("sas_transport_reason"), "projection_budget")
        self.assertFalse(refused.raw.get("sas_transport_available", False))
        warnings = [warning for warning in refused.warnings if "projection budget" in warning]
        self.assertEqual(len(warnings), 1)
        self.assertLess(len(warnings[0]), 200)
        self.assertEqual(refused.controllers, baseline.controllers)
        self.assertEqual(refused.paths, baseline.paths)
        self.assertEqual([link.model_dump() for link in refused.links], [link.model_dump() for link in baseline.links])
        self.assertEqual(refused.expanders, [])
        self.assertNotIn("sas_transport_port_index", refused.raw)
        self.assertNotIn("sas_transport_ports", refused.raw)

    def test_no_ses_budget_refusal_preserves_platform_routes(self):
        from app.services.linux_sas_transport import parse_sas_transport, transport_matches_selected_blocks

        payload, slots = shared_port_survey(self.payload, disks=200, phys=256)
        slots = [slot.model_copy(update={"ssh_ses_device": None}) for slot in slots]
        output = json.dumps(payload)
        self.assertTrue(
            transport_matches_selected_blocks(parse_sas_transport(output), [slot.device_name for slot in slots])
        )
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                before = self.selection_fabric(platform, slots, "")
                after = self.selection_fabric(platform, slots, output)
                self.assertEqual(after.raw["fabric_kind"], before.raw["fabric_kind"])
                for field in ("controllers", "paths", "nodes", "links", "traces", "enclosures"):
                    self.assertEqual(getattr(after, field), getattr(before, field), field)
                self.assertEqual(after.raw.get("sas_transport_reason"), "projection_budget")
                self.assertEqual(after.raw.get("sas_transport_state"), "not_read")
                added = [warning for warning in after.warnings if warning not in before.warnings]
                self.assertEqual(len(added), 1)
                self.assertIn("projection budget", added[0])
                self.assertLess(len(added[0]), 200)

    def test_no_ses_other_enrichment_refusal_preserves_platform_routes(self):
        from unittest.mock import patch

        slot = SlotView(slot=0, slot_label="00", row_index=0, column_index=0, device_name="sdz", present=True)
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                before = self.selection_fabric(platform, [slot], "")
                with patch("app.services.sas_fabric_linux_sas.enrich_linux_sas_fabric", return_value=None) as refused:
                    after = self.selection_fabric(platform, [slot], json.dumps(self.payload))
                refused.assert_called_once()
                self.assertEqual(after.raw["fabric_kind"], before.raw["fabric_kind"])
                for field in ("controllers", "paths", "nodes", "links", "traces", "enclosures"):
                    self.assertEqual(getattr(after, field), getattr(before, field), field)

    def selection_fabric(self, platform, slots, output):
        return build_sas_fabric_snapshot(
            system=SystemConfig(id="synthetic-system", truenas=TrueNASConfig(platform=platform)),
            snapshot=InventorySnapshot(
                slots=slots,
                refresh_interval_seconds=30,
                selected_enclosure_id=REAR,
                selected_enclosure_label="Synthetic storage view",
            ),
            ssh_outputs={KEY: output} if output else {},
        )

    def assert_same_selected_routes(self, platform, slots, output):
        before = self.selection_fabric(platform, slots, "")
        after = self.selection_fabric(platform, slots, output)
        self.assertEqual(after.raw["fabric_kind"], before.raw["fabric_kind"])
        self.assertEqual(after.controllers, before.controllers)
        self.assertEqual(after.paths, before.paths)
        self.assertEqual(after.expanders, before.expanders)
        self.assertEqual([node.model_dump() for node in after.nodes], [node.model_dump() for node in before.nodes])
        self.assertEqual([link.model_dump() for link in after.links], [link.model_dump() for link in before.links])
        self.assertEqual(
            [trace.model_dump() for trace in after.traces], [trace.model_dump() for trace in before.traces]
        )
        return after

    def test_nvme_only_selection_keeps_routes_with_unrelated_sas_survey(self):
        # Same regression as the missing-route reproduction: selected NVMe,
        # unrelated SAS HBA, no shared selected disk identity.
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            device_name="nvme0n1",
            smart_device_names=["/dev/nvme0n1"],
            transport_protocol="nvme",
            model="Synthetic NVMe",
            raw_status={"device_names": ["nvme0n1", "0000:01:00.0"]},
        )
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                result = self.assert_same_selected_routes(platform, [slot], json.dumps(self.payload))
                self.assertEqual([path["path_type"] for path in result.paths], ["nvme"])
                self.assertFalse(any(node.id == "controller:host2" for node in result.nodes))

    def test_unmatched_selected_block_cannot_use_other_view_sas_identity(self):
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            device_name="sdx",
            transport_protocol="sas",
            enclosure_id=REAR,
            # Neither shared enclosure metadata nor another disk's alias is a
            # primary block join for the selected row.
            smart_device_names=["/dev/sdz"],
        )
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                self.assert_same_selected_routes(platform, [slot], json.dumps(self.payload))

    def test_selected_sas_view_still_admits_transport_without_ses(self):
        disk = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            device_name="/dev/sdz",
            transport_protocol="sas",
        )
        empty = SlotView(slot=1, slot_label="01", row_index=0, column_index=1, present=False)
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                result = self.selection_fabric(platform, [disk, empty], json.dumps(self.payload))
                self.assertIs(result.raw["sas_transport_available"], True)
                self.assertIn("controller:host2", {node.id for node in result.nodes})
                self.assertEqual(len(result.expanders), 2)
                self.assertTrue(any(link.kind == "transport-bay" and link.target == "bay:0" for link in result.links))
                trace = next(trace for trace in result.traces if trace.id == "bay:0")
                self.assertEqual(trace.metrics["sas_transport_ports"][0]["member_device_name"], "sdz")
                self.assertIn("bay:1", {node.id for node in result.nodes})

    def test_mixed_selected_view_keeps_non_sas_routes(self):
        slots = [
            SlotView(
                slot=0,
                slot_label="00",
                row_index=0,
                column_index=0,
                present=True,
                device_name="sdz",
                transport_protocol="sas",
            ),
            SlotView(
                slot=1,
                slot_label="01",
                row_index=0,
                column_index=1,
                present=True,
                device_name="nvme0n1",
                transport_protocol="nvme",
            ),
        ]
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                result = self.assert_same_selected_routes(platform, slots, json.dumps(self.payload))
                self.assertIn("nvme", {path["path_type"] for path in result.paths})

    def test_empty_selection_does_not_admit_unrelated_transport_hardware(self):
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                self.assert_same_selected_routes(platform, [], json.dumps(self.payload))

    def test_ambiguous_selected_block_does_not_replace_existing_routes(self):
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            device_name="sdz",
            transport_protocol="sas",
        )
        payload = deepcopy(self.payload)
        end = deepcopy(next(row for row in payload["rows"] if row["kind"] == "sas_end_device"))
        end["name"] = "end_device-2:1:9"
        end["path"] = end["path"].replace("port-2:1:2/end_device-2:1:2", "port-2:1:9/end_device-2:1:9")
        payload["rows"].append(end)
        for platform in ("linux", "scale"):
            with self.subTest(platform=platform):
                self.assert_same_selected_routes(platform, [slot], json.dumps(payload))
                duplicate = slot.model_copy(update={"slot": 1, "slot_label": "01", "column_index": 1})
                self.assert_same_selected_routes(platform, [slot, duplicate], json.dumps(self.payload))

    def test_unusable_selected_sas_chain_retains_existing_routes(self):
        slot = SlotView(
            slot=0,
            slot_label="00",
            row_index=0,
            column_index=0,
            present=True,
            device_name="sdz",
            transport_protocol="sas",
        )
        for mutation in ("upstream_port", "end_port", "remote_membership", "host", "incomplete"):
            payload = deepcopy(self.payload)
            if mutation == "incomplete":
                payload["complete"] = False
            elif mutation == "remote_membership":
                next(row for row in payload["rows"] if row["kind"] == "sas_port" and row["name"] == "port-2:1:2")[
                    "links"
                ].pop("end_device-2:1:2")
            else:
                kind, name = {
                    "upstream_port": ("sas_port", "port-2:0"),
                    "end_port": ("sas_port", "port-2:1:2"),
                    "host": ("sas_host", "host2"),
                }[mutation]
                payload["rows"] = [row for row in payload["rows"] if (row["kind"], row["name"]) != (kind, name)]
            for platform in ("linux", "scale"):
                with self.subTest(platform=platform, mutation=mutation):
                    self.assert_same_selected_routes(platform, [slot], json.dumps(payload))

    def test_transport_keeps_ses_membership_and_existing_path_details(self):
        result = fabric(json.dumps(self.payload))
        enclosure = next(node for node in result.nodes if node.kind == "ses-enclosure")
        self.assertEqual(enclosure.controller_id, "controller:host2")
        self.assertEqual(enclosure.raw["controller_ids"], ["controller:host2"])
        trace = next(trace for trace in result.traces if trace.id == "bay:0")
        self.assertEqual(trace.metrics["path_states"][0]["ses_device"], "/dev/sg9")
        self.assertEqual(trace.metrics["path_states"][0]["sas_address"], DISK)
        self.assertEqual(trace.metrics["path_states"][0]["sg_device"], "/dev/sg4")
        self.assertEqual(trace.metrics["path_states"][0]["scsi_hctl"], "2:0:0:0")
        self.assertEqual(trace.metrics["path_states"][0]["transport_protocol"], "sas")
        self.assertEqual(trace.metrics["path_states"][0]["target_port_protocol"], "SSP")

    def test_unjoined_transport_retains_the_ses_path_observation(self):
        payload = deepcopy(self.payload)
        next(row for row in payload["rows"] if row["kind"] == "sas_end_device")["links"] = {}
        trace = next(trace for trace in fabric(json.dumps(payload)).traces if trace.id == "bay:0")
        self.assertEqual(len(trace.metrics["path_states"]), 1)
        self.assertEqual(trace.metrics["path_states"][0]["ses_device"], "/dev/sg9")
        self.assertIsNone(trace.metrics["path_states"][0]["controller"])
        self.assertIsNone(trace.metrics["path_states"][0]["path_id"])

    def test_reordered_classes_preserve_graph(self):
        payload = deepcopy(self.payload)
        payload["rows"].reverse()
        first, second = fabric(json.dumps(self.payload)), fabric(json.dumps(payload))
        self.assertEqual([link.model_dump() for link in first.links], [link.model_dump() for link in second.links])
        self.assertEqual(first.expanders, second.expanders)

    def test_oversized_attribute_and_unreadable_directory_remain_not_read(self):
        (self.root / "class/scsi_host/host2/board_name").write_text("x" * 600)
        payload = run_survey(self.root)
        self.assertIsNone(next(row for row in payload["rows"] if row["kind"] == "scsi_host")["attrs"]["board_name"])
        denied = run_survey(
            self.root,
            {
                "with os.scandir(path) as entries:": 'raise PermissionError(13, "Permission denied")\n        with os.scandir(path) as entries:'
            },
        )
        self.assertFalse(denied["complete"])

    def test_malformed_and_duplicate_payloads_fail_closed(self):
        from app.services.linux_sas_transport import parse_sas_transport

        bad = ["{", "[" * 2000, "x" * (2 * 1024 * 1024 + 1)]
        for field, value in (
            ("name", "x" * 500),
            ("kind", []),
            ("path", "/sys/devices/../bad"),
            ("attrs", {"bad": []}),
        ):
            payload = deepcopy(self.payload)
            payload["rows"][0][field] = value
            bad.append(json.dumps(payload))
        duplicate = deepcopy(self.payload)
        duplicate["rows"].append(duplicate["rows"][0])
        bad.append(json.dumps(duplicate))
        for output in bad:
            with self.subTest(size=len(output)):
                self.assertFalse(parse_sas_transport(output).complete)
                self.assertEqual(fabric(output).expanders, [])


class LinuxSasCommandTests(unittest.TestCase):
    def test_fixed_command_and_single_platform_probe(self):
        from app.services.inventory_sources import InventorySourceCollectionMixin
        from app.services.linux_sas_transport_read import SAS_TRANSPORT_COMMAND, SAS_TRANSPORT_SCRIPT
        from app.services.parsers import canonicalize_ssh_command

        self.assertEqual(shlex.split(SAS_TRANSPORT_COMMAND), ["/usr/bin/python3", "-I", "-c", SAS_TRANSPORT_SCRIPT])
        self.assertEqual(canonicalize_ssh_command(SAS_TRANSPORT_COMMAND), KEY)
        for platform in ("scale", "linux", "core", "esxi"):
            service = SimpleNamespace(system=SystemConfig(truenas=TrueNASConfig(platform=platform)))
            commands = InventorySourceCollectionMixin._linux_storage_enrichment_probe_commands(service, [])
            self.assertEqual(commands.count(SAS_TRANSPORT_COMMAND), int(platform in {"scale", "linux"}))
            commands = InventorySourceCollectionMixin._linux_storage_enrichment_probe_commands(
                service, [SimpleNamespace(command=SAS_TRANSPORT_COMMAND)]
            )
            self.assertNotIn(SAS_TRANSPORT_COMMAND, commands)
        self.assertNotIn("sudo", SAS_TRANSPORT_COMMAND)
        self.assertNotIn("smp_utils", SAS_TRANSPORT_COMMAND)

    def test_script_uses_no_write_open_or_recursive_walk(self):
        import ast
        from app.services.linux_sas_transport_read import SAS_TRANSPORT_SCRIPT

        tree = ast.parse(SAS_TRANSPORT_SCRIPT)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        attrs = {node.func.attr for node in calls if isinstance(node.func, ast.Attribute)}
        self.assertFalse({"walk", "glob", "rglob", "write", "unlink", "mkdir", "chmod"} & attrs)
        opens = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "open"]
        self.assertEqual(len(opens), 1)
        self.assertIn("O_RDONLY", ast.unparse(opens[0]))


if __name__ == "__main__":
    unittest.main()
