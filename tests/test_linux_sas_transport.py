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
    """Kernel-shaped class links, transport objects, port backlinks and targets."""

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
        self.port(
            self.front / "port-2:0:0", self.host, [self.phy(self.front, f"phy-2:0:{i}", i, FRONT) for i in range(2)]
        )
        self.port(
            self.front / "port-2:0:1", self.rear, [self.phy(self.front, f"phy-2:0:{i}", i, FRONT) for i in (2, 3)]
        )
        self.port(
            self.rear / "port-2:1:0", self.front, [self.phy(self.rear, f"phy-2:1:{i}", i, REAR) for i in range(2)]
        )
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
            (obj / remote.name).symlink_to(remote)


def fabric(output: str):
    slot = SlotView(
        slot=0,
        slot_label="00",
        row_index=0,
        column_index=0,
        device_name="sdz",
        present=True,
        ssh_ses_device="/dev/sg9",
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
        self.assertEqual(cascade.reported_lanes.target_phys, [0, 1])
        self.assertEqual(cascade.reported_lanes.negotiated_rate_gbps, 12)
        self.assertEqual(cascade.reported_lanes.lanes, [])
        trace = next(trace for trace in result.traces if trace.id == "bay:0")
        ports = trace.metrics["sas_transport_ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["member_device_name"], "sdz")
        self.assertEqual(ports[0]["bay_identifier"], 7)  # not misused as a slot offset
        self.assertEqual(ports[0]["sas_address"], DISK)
        self.assertEqual(ports[0]["expander_phys"][0]["invalid_dword_count"], 9)
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

    def test_no_reciprocal_backlink_keeps_parent_but_not_reported_lanes(self):
        payload = deepcopy(self.payload)
        for row in payload["rows"]:
            if row["kind"] == "sas_port" and row["name"] == "port-2:1:0":
                row["links"].pop("expander-2:0")
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
