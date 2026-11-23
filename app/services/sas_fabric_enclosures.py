"""Physical enclosure identity, independent of the controller-local route."""
from __future__ import annotations

import re
from collections import defaultdict

from app.models.domain import InventorySnapshot, SasFabricLink, SasFabricNode, SasFabricTrace


def logical_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.lower().removeprefix("0x")
    return value if re.fullmatch(r"[0-9a-f]{16}", value) else None


def project_enclosures(
    nodes: dict[str, SasFabricNode], links: dict[str, SasFabricLink],
    traces: dict[str, SasFabricTrace], snapshot: InventorySnapshot,
) -> None:
    """Join only explicit logical IDs. A label or a bay range is never identity.

    SES device nodes remain observations attached to their physical enclosure.
    They do not become a second enclosure object. Provider handles stay in the
    observation list, not in the enclosure key or its controller_id.
    """
    by_id: dict[str, SasFabricNode] = {}
    replacements: dict[str, str] = {}
    ses_ids: dict[str, set[str]] = defaultdict(set)

    def board(identity: str) -> SasFabricNode:
        node_id = f"enclosure:{identity}"
        if node_id not in by_id:
            by_id[node_id] = SasFabricNode(
                id=node_id, kind="enclosure", label=identity, raw_id=identity,
                evidence=["SES enclosure logical id"],
                raw={"logical_id": identity, "observations": [], "legacy_alias_ids": []},
            )
        return by_id[node_id]

    for node in list(nodes.values()):
        if node.kind != "mpr-enclosure":
            continue
        identity = logical_id(node.raw.get("logical_id"))
        if identity is None:
            continue
        physical = board(identity)
        replacements[node.id] = physical.id
        physical.raw["observations"].append({"id": node.id, **node.raw, "metrics": dict(node.metrics)})
        physical.raw["legacy_alias_ids"].append(node.id)
        physical.evidence = list(dict.fromkeys([*physical.evidence, *node.evidence]))
        if node.alias and physical.alias is None:
            physical.alias = node.alias
            physical.display_label = node.display_label
        nodes.pop(node.id)

    for slot in snapshot.slots:
        if not slot.physical_location_known:
            continue
        raw = slot.raw_status or {}
        # A contradictory SES location cannot choose which board owns this bay.
        identities = {identity for value in (slot.enclosure_id, raw.get("enclosure_id"))
                      if (identity := logical_id(value)) is not None}
        if len(identities) != 1:
            continue
        identity = next(iter(identities))
        physical = board(identity)
        physical.related_slots = sorted(set(physical.related_slots) | {slot.slot})
        for target in slot.ssh_ses_targets:
            if logical_id(target.get("enclosure_id") or identity) == identity and target.get("ses_device"):
                ses_ids[f"ses:{target['ses_device']}"].add(physical.id)
        if slot.ssh_ses_device:
            ses_ids[f"ses:{slot.ssh_ses_device}"].add(physical.id)
        bay_id = f"bay:{slot.slot}"
        link_id = f"enclosure-bay:{physical.id}->{bay_id}:{slot.slot}"
        links[link_id] = SasFabricLink(
            id=link_id, source=physical.id, target=bay_id, kind="enclosure-bay",
            slot=slot.slot, related_slots=[slot.slot], evidence=["SES enclosure logical id"],
        )
        trace = traces.get(bay_id)
        if trace:
            trace.node_ids = list(dict.fromkeys([*trace.node_ids, physical.id]))
            trace.link_ids.append(link_id)

    for ses_id, owners in ses_ids.items():
        node = nodes.get(ses_id) or nodes.get(ses_id.replace("ses:/dev/", "ses:"))
        if node is None or len(owners) != 1:
            continue
        ses_id = node.id
        owner = next(iter(owners))
        node.kind = "ses-device"
        node.raw["enclosure_id"] = owner
        link_id = f"enclosure-ses:{owner}->{ses_id}"
        links[link_id] = SasFabricLink(id=link_id, source=owner, target=ses_id, kind="enclosure-ses",
                                     related_slots=node.related_slots, evidence=["SES enclosure logical id"])

    # Retain only agreement across observations; never sum the same disks
    # once per HBA. Individual route measurements remain in observations.
    for physical in by_id.values():
        observed = physical.raw["observations"]
        for key in {key for row in observed for key in row["metrics"]} - {"not_read"}:
            values = [row["metrics"].get(key) for row in observed]
            if values and all(value == values[0] for value in values):
                physical.metrics[key] = values[0]
            else:
                physical.metrics[key] = None
        unread = {key for row in observed for key in row["metrics"].get("not_read", [])}
        if unread:
            physical.metrics["not_read"] = sorted(unread)
        if "device_count" in physical.metrics:
            physical.raw["device_count"] = physical.metrics["device_count"]
    nodes.update(by_id)
    link_replacements: dict[str, str] = {}
    for old_id, link in list(links.items()):
        source, target = replacements.get(link.source, link.source), replacements.get(link.target, link.target)
        if (source, target) == (link.source, link.target):
            continue
        links.pop(old_id)
        link.source, link.target = source, target
        suffix = f":{link.slot}" if link.slot is not None else ""
        link.id = f"{link.kind}:{source}->{target}{suffix}"
        link_replacements[old_id] = link.id
        if link.id in links:
            links[link.id].related_slots = sorted(set(links[link.id].related_slots) | set(link.related_slots))
        else:
            links[link.id] = link
    for trace in traces.values():
        trace.node_ids = list(dict.fromkeys(replacements.get(value, value) for value in trace.node_ids))
        trace.link_ids = list(dict.fromkeys(link_replacements.get(value, value) for value in trace.link_ids))
        for row in trace.metrics.get("mpr_devices", []):
            row["enclosure_id"] = replacements.get(row.get("enclosure_id"), row.get("enclosure_id"))
