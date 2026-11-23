"""Project admitted Linux SAS transport evidence onto the SES fabric graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.models.domain import (
    SasFabricLink,
    SasFabricNode,
    SasFabricReportedLane,
    SasFabricReportedLanes,
    SasFabricTrace,
)
from app.services.linux_sas_transport import SasTransportData, SasTransportRow, parse_sas_transport

EVIDENCE = ["Linux SAS transport sysfs"]


def _phy(row: SasTransportRow) -> dict[str, Any]:
    return {
        "name": row.name,
        "phy": row.phy,
        "sas_address": row.sas_address,
        "negotiated_link_rate": row.attrs.get("negotiated_linkrate") if row.negotiated_rate_gbps is not None else None,
        "negotiated_rate_gbps": row.negotiated_rate_gbps,
        "maximum_link_rate": row.attrs.get("maximum_linkrate") if row.maximum_rate_gbps is not None else None,
        "maximum_rate_gbps": row.maximum_rate_gbps,
        "minimum_rate_gbps": row.minimum_rate_gbps,
        "speed": row.attrs.get("negotiated_linkrate") if row.negotiated_rate_gbps is not None else None,
        "counter_state": "measured" if all(value is not None for value in row.counters.values()) else "not_read",
        "counter_source": "linux_sas_phy",
        "not_read": list(row.not_read),
        **row.counters,
    }


def _port_phys(port: SasTransportRow, phys: dict[str, SasTransportRow]) -> list[SasTransportRow] | None:
    members = [phys[path] for name, path in port.links.items() if name.startswith("phy-") and path in phys]
    # Membership is explicit in both directions. Never pair rows by order.
    if (
        port.num_phys is None
        or not port.num_phys
        or len(members) != port.num_phys
        or any(
            row.owner_path != port.owner_path or row.links.get("port") != port.path or row.phy is None
            for row in members
        )
        or len({row.phy for row in members}) != len(members)
        or sum(name.startswith("phy-") for name in port.links) != len(members)
    ):
        return None
    return sorted(members, key=lambda row: row.phy if row.phy is not None else -1)


def _reported_lanes(
    port: SasTransportRow,
    remote: SasTransportRow,
    ports: list[SasTransportRow],
    phys: dict[str, SasTransportRow],
    source_address: str | None,
    target_address: str | None,
) -> SasFabricReportedLanes | None:
    backlinks = [item for item in ports if item.owner_path == remote.path and port.owner_path in item.links.values()]
    if len(backlinks) != 1:
        return None
    local, peer = _port_phys(port, phys), _port_phys(backlinks[0], phys)
    if not local or not peer or len(local) != len(peer):
        return None
    rates = [row.negotiated_rate_gbps for row in (*local, *peer)]
    rate = rates[0] if rates[0] is not None and len(set(rates)) == 1 else None
    # A wide port reports the sets at either end, not the lane-to-lane wiring.
    # Keep lanes empty rather than fabricating a sorted zip of PHY pairs.
    pairs = []
    if len(local) == 1:
        source_phy, target_phy = local[0].phy, peer[0].phy
        assert source_phy is not None and target_phy is not None
        pairs = [SasFabricReportedLane(source_phy=source_phy, target_phy=target_phy, negotiated_rate_gbps=rate)]
    return SasFabricReportedLanes(
        basis="sysfs_reciprocal_port_membership",
        lane_count=len(local),
        source_phys=[row.phy for row in local if row.phy is not None],
        target_phys=[row.phy for row in peer if row.phy is not None],
        lanes=pairs,
        negotiated_rate_gbps=rate,
        source_sas_address=source_address,
        attached_sas_address=target_address,
        evidence=list(EVIDENCE),
    )


def enrich_linux_sas_fabric(
    *,
    output: str,
    snapshot: Any,
    nodes: dict,
    links: dict,
    traces: dict,
    controllers: list,
    paths: list,
    warnings: list[str],
) -> list[dict[str, Any]] | None:
    data = parse_sas_transport(output)
    if not data.complete or not data.of_kind("sas_host"):
        if output:
            warnings.append("Linux SAS transport sysfs detail was not read as a complete survey.")
        return None
    projection = _Projection(data, nodes, links)
    projection.build_hardware()
    if not projection.controller_ids:
        return None
    # SES remains the bay/containment evidence, not a pretend HBA. Replace only
    # its synthetic controller/path chain, keeping pools, bays and backplanes.
    removed = {
        key for key, node in nodes.items() if node.kind in {"controller", "path"} and key not in projection.hardware_ids
    }
    for key in removed:
        del nodes[key]
    for key, link in list(links.items()):
        if link.source in removed or link.target in removed:
            del links[key]
    for key, trace in list(traces.items()):
        if trace.kind == "path":
            del traces[key]
            continue
        trace.node_ids = [item for item in trace.node_ids if item not in removed]
        trace.link_ids = [item for item in trace.link_ids if item in links]
    controllers[:] = [nodes[key].raw for key in sorted(projection.controller_ids)]
    paths.clear()
    for node in nodes.values():
        if node.kind == "ses-enclosure":
            node.controller_id = None
    projection.attach_bays(snapshot, traces, warnings)
    for controller_id in sorted(projection.controller_ids):
        controller = nodes[controller_id]
        if not controller.related_slots:
            continue
        path_id = "path:" + controller.label + ":sysfs"
        path = {
            "id": path_id,
            "controller": controller.label,
            "state": "unknown",
            "count": len(controller.related_slots),
            "slots": controller.related_slots,
            "source": "linux_sas_transport",
        }
        paths.append(path)
        nodes[path_id] = SasFabricNode(
            id=path_id,
            kind="path",
            label=controller.label + " observed",
            controller_id=controller_id,
            related_slots=controller.related_slots,
            metrics={"count": path["count"]},
            raw=path,
            evidence=list(EVIDENCE),
        )
        path_link = projection.link(controller_id, path_id, "controller-path", controller.related_slots)
        host_link = "host-controller:host->" + controller_id
        traces[path_id] = SasFabricTrace(
            id=path_id,
            kind="path",
            label=nodes[path_id].label,
            node_ids=["host", controller_id, path_id],
            link_ids=[host_link, path_link.id],
            slots=controller.related_slots,
            metrics=path,
            evidence=list(EVIDENCE),
        )
        for trace in traces.values():
            if trace.kind == "bay" and any(
                port.get("path_id") == path_id for port in trace.metrics.get("sas_transport_ports", [])
            ):
                trace.node_ids.append(path_id)
                trace.link_ids.append(path_link.id)
    return [nodes[key].raw for key in sorted(projection.expander_ids)]


class _Projection:
    def __init__(self, data: SasTransportData, nodes: dict, links: dict):
        self.data, self.nodes, self.links = data, nodes, links
        self.controller_ids: set[str] = set()
        self.expander_ids: set[str] = set()
        self.hardware_ids: set[str] = set()
        self.by_path: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self.parent_links: dict[str, str] = {}
        self.phys = {row.path: row for row in data.of_kind("sas_phy")}
        self.ports = data.of_kind("sas_port")
        self.devices = {row.path: row for row in data.of_kind("sas_device")}
        self.host_details = {row.name: row for row in data.of_kind("scsi_host")}
        self.phys_by_owner: dict[str, list[SasTransportRow]] = defaultdict(list)
        for row in sorted(self.phys.values(), key=lambda item: item.name):
            self.phys_by_owner[row.owner_path].append(row)

    def link(self, source: str, target: str, kind: str, slots: list[int] | None = None) -> SasFabricLink:
        key = f"{kind}:{source}->{target}"
        if key not in self.links:
            self.links[key] = SasFabricLink(
                id=key, source=source, target=target, kind=kind, related_slots=slots or [], evidence=list(EVIDENCE)
            )
        else:
            self.links[key].related_slots = sorted(set(self.links[key].related_slots + (slots or [])))
        return self.links[key]

    def build_hardware(self) -> None:
        for host in self.data.of_kind("sas_host"):
            key = "controller:" + host.name
            detail = self.host_details.get(host.name)
            if detail and detail.path != host.path:
                detail = None
            attrs = detail.attrs if detail else {}
            rows = [_phy(row) for row in self.phys_by_owner[host.path]]
            raw = {
                "id": key,
                "name": host.name,
                "board": attrs.get("board_name"),
                "firmware": attrs.get("version_fw"),
                "driver": attrs.get("proc_name"),
                "sas_address": detail.sas_address if detail else None,
                "phy_rows": rows,
                "source": "linux_sas_transport",
                "related_slots": [],
            }
            self.nodes[key] = SasFabricNode(
                id=key,
                kind="controller",
                label=host.name,
                raw_id=host.name,
                raw=raw,
                evidence=list(EVIDENCE),
                metrics={"firmware": raw["firmware"], "phy_count": len(rows)},
            )
            self.by_path[host.path] = key
            self.controller_ids.add(key)
            self.link("host", key, "host-controller")
        # Resolved nesting, not the driver's level field, defines cascade depth.
        for row in sorted(self.data.of_kind("sas_expander"), key=lambda item: (item.path.count("/"), item.path)):
            port_path = row.owner_path
            parent_path = port_path.rsplit("/", 1)[0]
            parent_id = self.by_path.get(parent_path)
            controller_id = (
                parent_id
                if parent_id in self.controller_ids
                else self.nodes[parent_id].controller_id
                if parent_id
                else None
            )
            device = self.devices.get(row.path)
            key = f"expander:{controller_id or 'unknown'}:{row.name}"
            phys = [_phy(item) for item in self.phys_by_owner[row.path]]
            parent_depth = self.nodes[parent_id].metrics.get("sas_level") if parent_id else None
            depth = (
                1 if parent_id in self.controller_ids else parent_depth + 1 if isinstance(parent_depth, int) else None
            )
            raw = {
                "id": key,
                "name": row.name,
                "controller_id": controller_id,
                "sas_address": device.sas_address if device else None,
                "vendor": row.attrs.get("vendor_id"),
                "product": row.attrs.get("product_id"),
                "revision": row.attrs.get("product_rev"),
                "reported_level": row.attrs.get("level"),
                "sas_level": depth if parent_id else None,
                "phys": phys,
                "num_phys": len(phys),
                "source": "linux_sas_transport",
                "parent_link_state": "unknown",
                "parent_link_reason": "port_not_read",
            }
            self.nodes[key] = SasFabricNode(
                id=key,
                kind="expander",
                label=raw["product"] or row.name,
                raw_id=raw["sas_address"] or row.name,
                controller_id=controller_id,
                evidence=list(EVIDENCE),
                raw=raw,
                metrics={
                    "num_phys": len(phys),
                    "linked_phys": len(phys)
                    if all(item["negotiated_rate_gbps"] is not None for item in phys)
                    else None,
                    "not_read": ["linked_phys"] if any(item["negotiated_rate_gbps"] is None for item in phys) else [],
                    "sas_level": raw["sas_level"],
                },
            )
            self.by_path[row.path] = key
            self.expander_ids.add(key)
            ports = [item for item in self.ports if item.path == port_path and item.links.get(row.name) == row.path]
            if parent_id and len(ports) == 1 and (parent_id in self.controller_ids or parent_id in self.parents):
                port = ports[0]
                link = self.link(
                    parent_id, key, "controller-expander" if parent_id in self.controller_ids else "expander-expander"
                )
                link.reported_lanes = _reported_lanes(
                    port, row, self.ports, self.phys, self.nodes[parent_id].raw.get("sas_address"), raw["sas_address"]
                )
                self.nodes[key].raw.update(
                    parent_id=parent_id,
                    parent_link_state="resolved",
                    parent_link_reason="sysfs_ancestry",
                    parent_port=port.name,
                    source_phys=[item.phy for item in (_port_phys(port, self.phys) or [])],
                    reported_num_phys=port.num_phys,
                )
                self.parents[key], self.parent_links[key] = parent_id, link.id
        self.hardware_ids = self.controller_ids | self.expander_ids

    def chain(self, owner_id: str) -> tuple[list[str], list[str]]:
        node_ids, link_ids = [owner_id], []
        while node_ids[-1] in self.parents:
            link_ids.append(self.parent_links[node_ids[-1]])
            node_ids.append(self.parents[node_ids[-1]])
        if node_ids[-1] not in self.controller_ids:
            return [], []
        link_ids.append(f"host-controller:host->{node_ids[-1]}")
        return ["host", *reversed(node_ids)], list(reversed(link_ids))

    def attach_bays(self, snapshot: Any, traces: dict, warnings: list[str]) -> None:
        by_block: dict[str, list[SasTransportRow]] = defaultdict(list)
        for row in self.data.of_kind("sas_end_device"):
            for block in row.links:
                by_block[block].append(row)
        owners = Counter(slot.device_name.removeprefix("/dev/") for slot in snapshot.slots if slot.device_name)
        for slot in snapshot.slots:
            key = f"bay:{slot.slot}"
            bay, trace = self.nodes.get(key), traces.get(key)
            if bay is None or trace is None:
                continue
            # Named block identity is independent of the bay ordinal. Never use
            # bay_identifier as an offset or guess a join by a SAS address alone.
            block = (slot.device_name or "").removeprefix("/dev/")
            matches = by_block.get(block, []) if owners[block] == 1 else []
            if len(matches) != 1:
                matches = []
            port_rows = []
            for row in matches:
                device = self.devices.get(row.path)
                port = next(
                    (
                        item
                        for item in self.ports
                        if item.path == row.owner_path and item.links.get(row.name) == row.path
                    ),
                    None,
                )
                owner_id = self.by_path.get(port.owner_path) if port else None
                if not owner_id or port is None:
                    continue
                chain_nodes, chain_links = self.chain(owner_id)
                if not chain_nodes:
                    continue
                phys = _port_phys(port, self.phys)
                controller_id = chain_nodes[1]
                metric = {
                    "controller": self.nodes[controller_id].label,
                    "state": "unknown",
                    "path_id": "path:" + self.nodes[controller_id].label + ":sysfs",
                    "member_device_name": block,
                    "sas_address": device.sas_address if device else None,
                    "attached_sas_address": self.nodes[owner_id].raw.get("sas_address"),
                    "enclosure_identifier": device.enclosure_identifier if device else None,
                    "bay_identifier": device.bay_identifier if device else None,
                    "phy_identifier": device.phy if device else None,
                    "port": port.name,
                    "expander_ids": [item for item in chain_nodes if item in self.expander_ids],
                    "expander_phys": [_phy(item) for item in (phys or [])],
                    "source": "linux_sas_transport",
                }
                port_rows.append(metric)
                ses_ids = [item for item in trace.node_ids if self.nodes[item].kind == "ses-enclosure"]
                destinations = ses_ids or [key]
                for destination in destinations:
                    edge = self.link(
                        owner_id, destination, "expander-ses-enclosure" if ses_ids else "transport-bay", [slot.slot]
                    )
                    chain_links.append(edge.id)
                trace.node_ids = sorted(set(trace.node_ids + chain_nodes))
                trace.link_ids = sorted(set(trace.link_ids + chain_links))
                for item in chain_nodes[1:]:
                    node = self.nodes[item]
                    node.related_slots = sorted(set(node.related_slots + [slot.slot]))
                    node.raw["related_slots"] = node.related_slots
                for item in chain_links:
                    self.links[item].related_slots = sorted(set(self.links[item].related_slots + [slot.slot]))
            bay.metrics["sas_transport_ports"] = port_rows
            bay.raw["sas_transport_ports"] = port_rows
            trace.metrics["sas_transport_ports"] = port_rows
            trace.metrics["path_states"] = port_rows
            if block and not port_rows:
                warnings.append(
                    f"Bay {slot.slot}: SAS transport end-device port was not read or could not be joined by block identity."
                )
