"""Invented fabric shapes exercised through the real Storage Fabric builder.

No appliance captures, local config, optional adapter or remote reads are used.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from app.config import SystemConfig, TrueNASConfig
from app.models.domain import InventorySnapshot, SlotView
from app.services.sas_fabric import build_sas_fabric_snapshot

class Survey:
    """A synthetic survey in the shape of the collector's read: {version, complete, rows}."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.phys: dict[str, dict] = {}

    def row(self, kind: str, name: str, path: str, attrs: dict | None = None, links: dict | None = None) -> dict:
        item = {"kind": kind, "name": name, "path": path, "attrs": attrs or {}, "links": links or {}}
        self.rows.append(item)
        return item

    def _phy(self, name: str, path: str, ident: int, address: str, kind: str, maximum: str = "12.0 Gbit") -> None:
        self.phys[name] = self.row("sas_phy", name, path, {
            "phy_identifier": str(ident), "negotiated_linkrate": "Unknown", "maximum_linkrate": maximum, "minimum_linkrate": "3.0 Gbit",
            "invalid_dword_count": "0", "running_disparity_error_count": "0", "loss_of_dword_sync_count": "0", "phy_reset_problem_count": "0",
            "sas_address": "0x" + address, "device_type": kind,
        })

    def host(self, number: int, *, address: str, board: str | None = "HBA-9999", firmware: str | None = "1.2.3", driver: str | None = "examplesas", phys: int = 8, slot: str = "0000:03:00.0") -> dict:
        path = f"/sys/devices/pci0000:00/{slot}/host{number}"
        self.row("sas_host", f"host{number}", path)
        self.row("scsi_host", f"host{number}", path, {"board_name": board, "version_fw": firmware, "proc_name": driver, "host_sas_address": "0x" + address})
        for index in range(phys):
            self._phy(f"phy-{number}:{index}", f"{path}/phy-{number}:{index}", index, address, "end device")
        return {"n": number, "path": path, "prefix": str(number), "ports": 0}

    def _port(self, owner: dict, phys: list[int], rate: float) -> tuple[str, str, dict]:
        name = f"port-{owner['prefix']}:{owner['ports']}"
        owner["ports"] += 1
        path = f"{owner['path']}/{name}"
        links = {}
        for ident in phys:
            phy = self.phys[f"phy-{owner['prefix']}:{ident}"]
            phy["attrs"]["negotiated_linkrate"] = f"{rate} Gbit"
            phy["links"]["port"] = path
            links[phy["name"]] = phy["path"]
        return name, path, self.row("sas_port", name, path, {"num_phys": str(len(phys))}, links)

    def expander(self, parent: dict, parent_phys: list[int], *, index: int, address: str, vendor: str | None = "ACME", product: str | None = "EXP-36", revision: str | None = "a1",
                 phys: int = 36, uplink: list[int] | None = None, level: str = "1", rate: float = 12.0, errors: dict | None = None) -> dict:
        _, port_path, port = self._port(parent, parent_phys, rate)
        name = f"expander-{parent['n']}:{index}"
        path = f"{port_path}/{name}"
        port["links"][name] = path
        self.row("sas_expander", name, path, {"vendor_id": vendor, "product_id": product, "product_rev": revision, "level": level})
        self.row("sas_device", name, path, {"sas_address": "0x" + address, "device_type": "edge expander", "phy_identifier": str(parent_phys[0])})
        prefix = f"{parent['n']}:{index}"
        for ident in range(phys):
            self._phy(f"phy-{prefix}:{ident}", f"{path}/phy-{prefix}:{ident}", ident, address, "edge expander")
        # The expander's own side of the cable: linked, and in no port (sysfs has none there).
        for ident in uplink or []:
            self.phys[f"phy-{prefix}:{ident}"]["attrs"]["negotiated_linkrate"] = f"{rate} Gbit"
        for ident, counts in (errors or {}).items():
            keys = ("invalid_dword_count", "running_disparity_error_count", "loss_of_dword_sync_count", "phy_reset_problem_count")
            self.phys[f"phy-{prefix}:{ident}"]["attrs"].update({key: (None if value is None else str(value)) for key, value in zip(keys, counts)})
        return {"n": parent["n"], "path": path, "prefix": prefix, "ports": 0, "address": address}

    def end(self, owner: dict, phys: list[int], *, bay: int, enclosure: str, address: str, block: str | None, rate: float = 12.0) -> None:
        port_name, port_path, port = self._port(owner, phys, rate)
        name = "end_device-" + port_name.removeprefix("port-")
        path = f"{port_path}/{name}"
        port["links"][name] = path
        self.row("sas_device", name, path, {"sas_address": "0x" + address, "bay_identifier": str(bay), "enclosure_identifier": "0x" + enclosure, "phy_identifier": str(phys[0]), "device_type": "end device"})
        target = name.removeprefix("end_device-").replace(":", "_")
        self.row("sas_end_device", name, path, {}, {block: f"{path}/target{target}/block/{block}"} if block else {})

    def text(self, complete: bool = True) -> str:
        return json.dumps({"version": 1, "complete": complete, "rows": self.rows})

def grid(rows: int, columns: int) -> list[list[int]]:
    """Layout rows, top row first, bay 0 at the bottom left."""
    return [[column * rows + (rows - 1 - row) for column in range(columns)] for row in range(rows)]

def enriched(system: dict, enclosure: dict, survey: str) -> tuple[dict, dict, bool]:
    ses = enclosure.get("ses", True)
    device = "/dev/" + enclosure.get("sg", "sg9")
    slots = [SlotView(
        slot=item["slot"], slot_label=f"{item['slot']:02d}", row_index=0, column_index=item["slot"],
        present=bool(item.get("block")), device_name=item.get("block"), sas_address=item.get("address"),
        enclosure_id=enclosure["id"] if ses else None, ssh_ses_device=device if ses else None,
        ssh_ses_targets=[{"ses_device": device, "enclosure_id": enclosure["id"]}] if ses else [],
        transport_protocol=item.get("protocol", "SAS") if item.get("block") else None,
        target_port_protocol=("STP" if item.get("protocol") == "SATA" else "SSP") if item.get("block") else None,
    ) for item in enclosure["slots"]]
    snapshot = InventorySnapshot(slots=slots, refresh_interval_seconds=30,
                                 selected_system_id=system["id"], selected_enclosure_id=enclosure["id"],
                                 selected_enclosure_label=enclosure.get("label"),
                                 last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc))
    result = build_sas_fabric_snapshot(
        system=SystemConfig(id=system["id"], label=system["label"], truenas=TrueNASConfig(platform="scale")),
        snapshot=snapshot, ssh_outputs={"sas transport sysfs": survey} if survey else {})
    fabric=result.model_dump(mode="json")
    fabric.pop("generated_at", None)
    inventory = {
        "selected_system_id": system["id"], "selected_enclosure_id": enclosure["id"],
        "selected_enclosure_label": enclosure.get("label"),
        "slots": [slot.model_dump(mode="json") for slot in slots],
        "enclosures": [{"id": item["id"], "label": item.get("label"), "alias": item.get("alias"),
                        "name": item.get("name"), "slot_count": len(item["slots"])}
                       for item in system["enclosures"] if item.get("listed", True)],
        "selected_profile": {"id": "example", "row_groups": [], "latch_edge": "right", "edge_label": "Front"},
    }
    if enclosure.get("layout") is not None:
        inventory["layout_rows"] = enclosure["layout"]
    return fabric, inventory, result.raw.get("sas_transport_available") is True



def disks(expander: dict, survey: Survey, enclosure_id: str, count: int, *, first_phy: int, prefix: str, serial: int, empty: tuple[int, ...] = (), sata: tuple[int, ...] = (), wide: tuple[int, ...] = ()) -> list[dict]:
    """Slots of one enclosure and their end devices on one expander. Block names are made up."""
    slots = []
    for bay in range(count):
        if bay in empty:
            slots.append({"slot": bay})
            continue
        block, address = f"{prefix}{bay}", f"5eee{serial:04x}{bay:08x}"
        survey.end(expander, [first_phy + bay], bay=bay, enclosure=enclosure_id, address=address, block=block, rate=6.0 if bay in sata else 12.0)
        slots.append({"slot": bay, "block": block, "address": address, "protocol": "SATA" if bay in sata else "SAS"})
    return slots

def storage_view(system: dict, node: dict, platform: str, owner: str | None) -> tuple[dict, dict]:
    """A storage system's own view of a shared backplane (one node of a cluster), no transport."""
    controller_id, enclosure_id = f"controller:node:{node['id']}", f"storage-enclosure:node:{node['id']}"
    item = lambda **value: {"display_label": None, "alias": None, "raw_id": None, "status": None, "slot": None, "controller_id": None, "related_slots": [], "metrics": {}, "evidence": [], "raw": {}, **value}  # noqa: E731
    nodes = [item(id="host", kind="host", label=system["label"]),
             item(id=controller_id, kind="controller", label=node["label"], status="online", raw={"pool_owner_label": owner, "fence_owner_label": owner, "io_fencing_enabled": True if owner else None}),
             item(id=enclosure_id, kind="storage-enclosure", label=node["label"], controller_id=controller_id, raw={"enclosure_id": node["id"]}),
             item(id="pool:tank", kind="pool", label="tank")]
    links = [{"id": f"host-controller:host->{controller_id}", "source": "host", "target": controller_id, "kind": "host-controller", "status": None, "related_slots": [], "evidence": [], "reported_lanes": None}]
    traces = []
    for slot in node["slots"]:
        bay_id, present = f"bay:{slot['slot']}", slot.get("present")
        nodes.append(item(id=bay_id, kind="bay", label=f"Bay {slot['slot']:02d}", status="online" if present else "empty", slot=slot["slot"], related_slots=[slot["slot"]], metrics={"present": present, "device_name": slot.get("device")}))
        links.append({"id": f"storage-enclosure-bay:{enclosure_id}->{bay_id}", "source": enclosure_id, "target": bay_id, "kind": "storage-enclosure-bay", "status": None, "related_slots": [slot["slot"]], "evidence": [], "reported_lanes": None})
        states = [{"controller": controller_id.removeprefix("controller:"), "controller_label": node["label"], "state": "online", "state_basis": "member-health-not-transport", "device_name": slot.get("device")}] if present else []
        traces.append({"id": bay_id, "label": f"Bay {slot['slot']:02d}", "kind": "bay", "node_ids": [bay_id], "link_ids": [], "slots": [slot["slot"]], "metrics": {"device_name": slot.get("device"), "path_states": states, "mpr_devices": []}, "evidence": []})
    fabric = {"available": True, "system_id": system["id"], "system_label": system["label"], "platform": platform, "selected_enclosure_id": node["id"], "selected_enclosure_label": node["label"],
              "nodes": nodes, "links": links, "traces": traces, "controllers": [], "expanders": [], "enclosures": [], "paths": [], "aliases": [],
              "warnings": ["Example cluster fabric: controller and expander hops appear only when those sources report them."], "sources": {}, "raw": {"fabric_kind": f"storage_{platform}"}}
    inventory = {"selected_system_id": system["id"], "selected_enclosure_id": node["id"], "selected_enclosure_label": node["label"], "layout_rows": [list(range(len(node["slots"])))],
                 "slots": [{"slot": slot["slot"], "present": slot.get("present"), "serial": slot.get("serial"), "device_name": slot.get("device"), "physical_location_known": True} for slot in node["slots"]],
                 "enclosures": [{"id": member["id"], "label": member["label"], "alias": None, "slot_count": len(member["slots"])} for member in system["nodes"]],
                 "platform_context": {"cluster_id": system["cluster"], "cluster_node_ids": [member["id"] for member in system["nodes"]]},
                 "selected_profile": {"id": "example-shared", "label": "Example shared shelf", "row_groups": [], "latch_edge": "top"}}
    return fabric, inventory

def fixtures() -> list[dict]:
    made = []

    def add(name: str, title: str, what: str, fabric: dict, inventory: dict, *, transport: bool, expect: dict, extra: dict | None = None) -> None:
        made.append({"id": name, "title": title, "what": what, "fabric": fabric, "options": {"inventory": inventory, **(extra or {})}, "transport": transport, "expect": expect})

    # 1. A three-level cascade: HBA -> A -> B -> C, seen from each enclosure.
    survey = Survey()
    hba = survey.host(4, address="5aaa000000000401", board="HBA-9999", firmware="1.2.3")
    a = survey.expander(hba, [0, 1, 2, 3], index=0, address="5bbb0000000a00ff", uplink=[32, 33, 34, 35], errors={33: (7, 0, 1, 0)})
    system = {"id": "example-cascade", "label": "Example cascade", "enclosures": []}
    shelf_a = {"id": "5bbb0000000a00f0", "label": "Shelf A", "name": "ACME EXP-36 a1", "sg": "sg3", "layout": grid(2, 6)}
    shelf_a["slots"] = disks(a, survey, shelf_a["id"], 12, first_phy=0, prefix="xda", serial=1, empty=(5,))
    b = survey.expander(a, [28, 29, 30, 31], index=1, address="5bbb0000000b00ff", product="EXP-24", phys=28, uplink=[24, 25, 26, 27])
    shelf_b = {"id": "5bbb0000000b00f0", "label": "Shelf B", "alias": "Scratch shelf", "name": "ACME EXP-24 a1", "sg": "sg4", "layout": grid(2, 4)}
    shelf_b["slots"] = disks(b, survey, shelf_b["id"], 8, first_phy=0, prefix="xdb", serial=2)
    c = survey.expander(b, [20, 21, 22, 23], index=2, address="5bbb0000000c00ff", product="EXP-24", phys=28, uplink=[24, 25, 26, 27])
    shelf_c = {"id": "5bbb0000000c00f0", "label": "Shelf C", "name": "ACME EXP-24 a1", "sg": "sg5", "layout": grid(1, 6)}
    shelf_c["slots"] = disks(c, survey, shelf_c["id"], 6, first_phy=2, prefix="xdc", serial=3, empty=(0, 1))
    system["enclosures"] = [shelf_a, shelf_b, shelf_c]
    for shelf, place, stubs in ((shelf_a, "top", ["Next in chain: Scratch shelf", "Further down the chain: Shelf C"]), (shelf_b, "middle", ["Reached through: Shelf A", "Next in chain: Shelf C"]),
                                (shelf_c, "bottom", ["Further up the chain: Shelf A", "Reached through: Scratch shelf"])):
        fabric, inventory, ok = enriched(system, shelf, survey.text())
        add(f"cascade3-{place}", f"Three-level cascade, seen from the {place}", "HBA to A to B to C, x4 cables; each enclosure's page shows the others as stubs, any depth", fabric, inventory, transport=ok,
            expect={"stubs": stubs, "boards": 1, "lanes": [4, 4, 4]})

    # 2. One expander with two children, and with five (grouped), one of them with no known enclosure.
    for count in (2, 5):
        survey = Survey()
        hba = survey.host(2, address="5aaa000000000201")
        root = survey.expander(hba, [0, 1, 2, 3, 4, 5, 6, 7], index=0, address="5ccc00000000c0ff", product="EXP-48", phys=48, uplink=[40, 41, 42, 43, 44, 45, 46, 47])
        system = {"id": f"example-fanout-{count}", "label": f"Example fan-out of {count}", "enclosures": []}
        head = {"id": "5ccc0000000000f0", "label": "Head shelf", "name": "ACME EXP-48 a1", "sg": "sg2", "layout": grid(2, 4)}
        head["slots"] = disks(root, survey, head["id"], 8, first_phy=0, prefix="yda", serial=10)
        system["enclosures"].append(head)
        children = []
        for index in range(count):
            child = survey.expander(root, [16 + 4 * index + lane for lane in range(4)], index=index + 1, address=f"5ccc00000000c{index + 1:x}7e", product="EXP-12", phys=20, uplink=[16, 17, 18, 19])
            shelf = {"id": f"5ccc00000000{index + 1:02x}f0", "label": f"Leaf {index + 1}", "name": "ACME EXP-12 a1", "sg": f"sg{10 + index}", "layout": grid(1, 4),
                     # The last of five is cabled but nothing says which enclosure it is: not in the inventory, and no disks on it.
                     "listed": not (count == 5 and index == 4)}
            shelf["slots"] = [] if not shelf["listed"] else disks(child, survey, shelf["id"], 4, first_phy=0, prefix=f"yd{chr(98 + index)}", serial=11 + index)
            system["enclosures"].append(shelf)
            children.append(shelf)
        fabric, inventory, ok = enriched(system, head, survey.text())
        add(f"fanout-{count}", f"One expander with {count} children", "one stub per child" if count == 2 else "more than three children are one stub that lists them; one child's enclosure is not read",
            fabric, inventory, transport=ok, expect={"stubs": ["Next in chain: Leaf 1", "Next in chain: Leaf 2"] if count == 2 else ["Next in chain: 5 enclosures"], "boards": 1})
        if count == 2:
            fabric, inventory, ok = enriched(system, children[0], survey.text())
            add("fanout-2-leaf", "One expander with two children, seen from a child", "the parent is a stub above; the sibling is elsewhere in the fabric", fabric, inventory, transport=ok,
                expect={"stubs": ["Reached through: Head shelf", "Elsewhere in the fabric: Leaf 2"], "boards": 1})

    # 3. Dual-domain on Linux: two HBAs to one expander. sysfs shows the expander once under each host.
    survey = Survey()
    system = {"id": "example-dual", "label": "Example dual-domain", "enclosures": []}
    shelf = {"id": "5ddd0000000000f0", "label": "Dual shelf", "name": "ACME EXP-36 a1", "sg": "sg6", "layout": grid(2, 4), "slots": []}
    for number, slot_name in ((6, "0000:04:00.0"), (7, "0000:05:00.0")):
        hba = survey.host(number, address=f"5aaa00000000{number:02x}01", slot=slot_name)
        expander = survey.expander(hba, [0, 1, 2, 3, 4, 5, 6, 7], index=0, address="5ddd0000000000ff", uplink=[28, 29, 30, 31, 32, 33, 34, 35] if number == 6 else [20, 21, 22, 23, 24, 25, 26, 27])
        # The same eight disks through each host, under a different block name; the SES view names the first.
        slots = disks(expander, survey, shelf["id"], 8, first_phy=0, prefix="zda" if number == 6 else "zdb", serial=20 + number)
        if number == 6:
            shelf["slots"] = slots
    system["enclosures"] = [shelf]
    fabric, inventory, ok = enriched(system, shelf, survey.text())
    add("dual-domain-linux", "Two HBAs to one expander (Linux)", "one chip reached via both HBAs; the collector joins one block device per bay, so the second path is not read, not missing", fabric, inventory, transport=ok,
        expect={"boards": 1, "chips": 1, "hbas": 2, "no_status": ["critical", "warning"]})

    # 4. An x4 wide port beside an x8 one; 5. a SATA disk on a single port.
    survey = Survey()
    hba = survey.host(1, address="5aaa000000000101", phys=12)
    wide = survey.expander(hba, [0, 1, 2, 3], index=0, address="5fff00000000a1b2", product="EXP-16", phys=24, uplink=[16, 17, 18, 19])
    system = {"id": "example-wide", "label": "Example wide ports", "enclosures": []}
    shelf = {"id": "5fff0000000000f0", "label": "x4 shelf", "name": "ACME EXP-16 a1", "sg": "sg7", "layout": grid(2, 4)}
    shelf["slots"] = disks(wide, survey, shelf["id"], 8, first_phy=0, prefix="wda", serial=30, sata=(2, 3), empty=(7,))
    other = survey.expander(hba, [4, 5, 6, 7, 8, 9, 10, 11], index=1, address="5fff00000000c3d4", product="EXP-16", phys=24, uplink=[16, 17, 18, 19, 20, 21, 22, 23])
    second = {"id": "5fff0000000001f0", "label": "x8 shelf", "name": "ACME EXP-16 a1", "sg": "sg8", "layout": grid(1, 4)}
    second["slots"] = disks(other, survey, second["id"], 4, first_phy=0, prefix="wdb", serial=31)
    system["enclosures"] = [shelf, second]
    fabric, inventory, ok = enriched(system, shelf, survey.text())
    add("wide-x4-sata", "An x4 wide port, and SATA disks", "x4 beside another HBA port that is x8; two SATA disks at 6 Gb/s on one port each, not judged for rate or redundancy", fabric, inventory, transport=ok,
        expect={"boards": 1, "lanes": [4, 8], "sata": 2, "stubs": ["Elsewhere in the fabric: x8 shelf"]})

    # 6. An expander with no SES device and no enclosure: bays joined by the collector's transport-bay links.
    survey = Survey()
    hba = survey.host(3, address="5aaa000000000301")
    bare = survey.expander(hba, [0, 1, 2, 3], index=0, address="5eee0000000077aa", product="EXP-8", phys=12, uplink=[8, 9, 10, 11])
    system = {"id": "example-noses", "label": "Example without SES", "enclosures": []}
    shelf = {"id": "no-ses-view", "label": "Disks", "sg": "sg0", "ses": False, "layout": None}
    shelf["slots"] = disks(bare, survey, "5eee0000000000f0", 4, first_phy=0, prefix="vda", serial=40)
    system["enclosures"] = [shelf]
    fabric, inventory, ok = enriched(system, shelf, survey.text())
    add("no-ses", "An expander with no SES device or enclosure", "no enclosure is drawn or named; the disks hang off the expander by the collector's own links", fabric, inventory, transport=ok, expect={"boards": 0, "enclosure_named": False})

    # 7. An enclosure with no layout rows.
    survey = Survey()
    hba = survey.host(5, address="5aaa000000000501")
    plain = survey.expander(hba, [0, 1, 2, 3], index=0, address="5abc000000004242", product="EXP-12", phys=20, uplink=[16, 17, 18, 19])
    system = {"id": "example-nolayout", "label": "Example without a layout", "enclosures": []}
    shelf = {"id": "5abc0000000000f0", "label": "Unprofiled shelf", "name": "ACME EXP-12 a1", "sg": "sg1", "layout": None}
    shelf["slots"] = disks(plain, survey, shelf["id"], 10, first_phy=0, prefix="uda", serial=50, empty=(9,))
    system["enclosures"] = [shelf]
    fabric, inventory, ok = enriched(system, shelf, survey.text())
    add("no-layout", "An enclosure with no layout rows", "the bays are drawn in number order and the drawing says no layout was given; no rows are made up", fabric, inventory, transport=ok, expect={"layout_rows": False})

    # 8. Unknown vendor, model and firmware strings; an enclosure with no label.
    survey = Survey()
    hba = survey.host(8, address="5aaa000000000801", board=None, firmware=None, driver=None)
    odd = survey.expander(hba, [0, 1, 2, 3], index=0, address="5def000000009c9c", vendor=None, product=None, revision=None, phys=20, uplink=[16, 17, 18, 19], errors={17: (None, None, None, None)})
    system = {"id": "example-unknown", "label": "Example unknown hardware", "enclosures": []}
    shelf = {"id": "5def0000000000f0", "label": None, "name": None, "sg": "sg12", "layout": grid(1, 6)}
    shelf["slots"] = disks(odd, survey, shelf["id"], 6, first_phy=0, prefix="tda", serial=60)
    system["enclosures"] = [shelf]
    fabric, inventory, ok = enriched(system, shelf, survey.text())
    add("unknown-strings", "Unknown vendor, model and firmware", "nothing is filled in for a string the survey does not have; the chip is named by its address", fabric, inventory, transport=ok, expect={"boards": 1})

    # 9. A pair where the standby node reports nothing (and the platform is not one the sandbox owns).
    system = {"id": "example-pair", "label": "Example pair", "cluster": "cluster-1", "nodes": []}
    active = {"id": "node-aaaa", "label": "node-a", "slots": [{"slot": bay, "present": bay < 5, "serial": f"SANITIZED-PAIR-{bay:03d}" if bay < 5 else None, "device": f"disk/by-id/example-{bay}" if bay < 5 else None} for bay in range(8)]}
    standby = {"id": "node-bbbb", "label": "node-b", "slots": [{"slot": bay, "present": None} for bay in range(8)]}
    system["nodes"] = [active, standby]
    fabric, inventory = storage_view(system, active, "linux", "node-a")
    peer_fabric, peer_inventory = storage_view(system, standby, "linux", None)
    add("pair-standby-silent", "A pair whose standby node reports nothing", "one shared backplane, two head ends; the silent node is said to report nothing and no bay is flagged for it", fabric, inventory, transport=False,
        expect={"shared": True, "no_status": ["critical", "warning"]}, extra={"peers": [{"fabric": peer_fabric, "inventory": peer_inventory}]})

    # 10. An empty survey, a survey with no SAS host, and an incomplete one: the enricher admits none.
    survey = Survey()
    hba = survey.host(9, address="5aaa000000000901")
    box = survey.expander(hba, [0, 1, 2, 3], index=0, address="5123000000000aff", phys=12, uplink=[8, 9, 10, 11])
    system = {"id": "example-empty", "label": "Example empty survey", "enclosures": []}
    shelf = {"id": "5123000000000af0", "label": "Quiet shelf", "name": "ACME EXP-36 a1", "sg": "sg14", "layout": grid(1, 4)}
    shelf["slots"] = disks(box, survey, shelf["id"], 4, first_phy=0, prefix="sda", serial=70)
    system["enclosures"] = [shelf]
    refused = []
    for label, text in (("empty", ""), ("no host", json.dumps({"version": 1, "complete": True, "rows": []})), ("incomplete", survey.text(complete=False))):
        fabric, inventory, ok = enriched(system, shelf, text)
        refused.append((label, ok))
    assert not any(ok for _, ok in refused), refused
    add("empty-survey", "An empty, host-less or incomplete survey", "the collector admits none of the three; the view is the SES view it was, with its hops not collected", fabric, inventory, transport=False, expect={"boards": 1, "reported_chip": True})
    return made
