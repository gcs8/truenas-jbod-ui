"""Inactive, pure composition of existing SasFabricSnapshot JSON payloads.

This draft is NOT an application API or a topology builder. It never admits a
parent, creates an edge, picks a newer observation, or infers a physical cable.
Callers supply authoritative system/enclosure IDs and explicit preview budgets.
There is no graph-size policy in the current snapshot model to reuse here.

Only host, controller and SAS-addressed expander nodes can be shared. Everything
else, including bay ordinals and traces, stays enclosure-local. Original payloads
remain under observations/snapshot, with their original local IDs and evidence.
No metric, status, slot list or diagnostic count is summed across observations.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any

from app.models.domain import SasFabricSnapshot


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _bounded_copy(value: Any, max_input_bytes: int, max_depth: int) -> Any:
    """Check JSON shape iteratively before recursive serializers/validators run.

    The running byte count is the exact compact ensure_ascii JSON size. Pending
    iterators, unlike a flattened traversal stack, do not allocate per child.
    Depth also refuses cycles without recursive traversal or partial output.
    """
    for limit in (max_input_bytes, max_depth):
        if type(limit) is not int or limit <= 0:
            raise ValueError("preview budgets must be positive integers")
    size = 0
    stack = [(iter((value,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        if depth > max_depth:
            raise ValueError("composition depth budget exceeded")
        if type(item) is dict:
            # Keys must be strings; process them without allocating an item list.
            if len(item) > max_input_bytes - size:
                raise ValueError("composition byte budget exceeded")
            if any(type(key) is not str for key in item):
                raise ValueError("composition requires JSON string keys")
            size += 2 + max(0, len(item) - 1) + len(item)
            stack.append((iter(part for pair in item.items() for part in pair), depth + 1))
        elif type(item) is list:
            size += 2 + max(0, len(item) - 1)
            stack.append((iter(item), depth + 1))
        elif item is None or type(item) in (str, int, bool, float):
            if type(item) is float and not math.isfinite(item):
                raise ValueError("composition requires finite JSON numbers")
            # Refuse huge strings before making an escaped copy.
            if type(item) is str and len(item) > max_input_bytes - size:
                raise ValueError("composition byte budget exceeded")
            try:
                size += len(_json(item))
            except (ValueError, UnicodeError) as exc:
                raise ValueError("invalid JSON scalar") from exc
        else:
            raise ValueError("composition requires JSON payloads")
        if size > max_input_bytes:
            raise ValueError("composition byte budget exceeded")
    try:
        return json.loads(_json(value))
    except (ValueError, RecursionError) as exc:
        raise ValueError("JSON nesting cannot be serialized") from exc


def _id(kind: str, *parts: str) -> str:
    # Tuple encoding preserves delimiters, case and authoritative IDs verbatim.
    return kind + ":" + _json(list(parts))


def _index(rows: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key.strip() or key in result:
            raise ValueError(f"missing or duplicate {kind} ID")
        result[key] = row
    return result


def _validate_expander_raw(raw: dict[str, Any]) -> None:
    # The domain intentionally leaves raw extensible. Validate only fields this
    # composer interprets, before using them in hashing or PHY comparisons.
    for field in ("controller_id", "sas_address", "dev_handle"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise ValueError("malformed expander identity evidence")
    phys = raw.get("phys")
    if phys is None:
        return
    if not isinstance(phys, list):
        raise ValueError("malformed expander PHY evidence")
    for phy in phys:
        if not isinstance(phy, dict):
            raise ValueError("malformed expander PHY row")
        for field in ("dev_handle", "device"):
            if phy.get(field) is not None and not isinstance(phy[field], str):
                raise ValueError("malformed expander PHY text")
        for field in ("phy", "remote_phy"):
            if phy.get(field) is not None and type(phy[field]) not in (str, int):
                raise ValueError("malformed expander PHY number")


def _prepare(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    scopes = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            raise ValueError("each graph must be a snapshot object")
        system, enclosure = payload.get("system_id"), payload.get("selected_enclosure_id")
        if any(not isinstance(scope, str) or not scope.strip() for scope in (system, enclosure)):
            raise ValueError("authoritative system/enclosure scope is required")
        if (system, enclosure) in scopes:
            raise ValueError("duplicate enclosure snapshot in system")
        scopes.add((system, enclosure))
        try:
            # JSON-mode strict validation admits timestamp strings, not coerced
            # IDs/numbers. Keep original fields, including future evidence.
            SasFabricSnapshot.model_validate_json(_json(payload), strict=True)
        except (ValueError, RecursionError) as exc:
            raise ValueError("invalid SasFabricSnapshot payload") from exc
        nodes = _index(payload.get("nodes", []), "node")
        links = _index(payload.get("links", []), "link")
        traces = _index(payload.get("traces", []), "trace")
        for node in nodes.values():
            if node["kind"] == "expander":
                _validate_expander_raw(node.get("raw", {}))
            controller = node.get("controller_id")
            if controller is not None and (controller not in nodes or nodes[controller]["kind"] != "controller"):
                raise ValueError("dangling controller reference")
            if node["kind"] == "host" and node.get("raw_id") not in (None, system):
                raise ValueError("host contradicts authoritative system scope")
            # PHY order is not evidence. Canonicalize only this known row set;
            # arbitrary evidence lists and trace order are not reordered.
            phys = node.get("raw", {}).get("phys")
            if isinstance(phys, list):
                phys.sort(key=_json)
        for link in links.values():
            if link["source"] not in nodes or link["target"] not in nodes:
                raise ValueError("dangling link endpoint")
            left, right = nodes[link["source"]], nodes[link["target"]]
            controllers = {n.get("controller_id") or (n["id"] if n["kind"] == "controller" else None) for n in (left, right)}
            controllers.discard(None)
            if len(controllers) > 1:
                raise ValueError("link crosses controller scopes")
        for trace in traces.values():
            if any(key not in nodes for key in trace.get("node_ids", [])) or any(key not in links for key in trace.get("link_ids", [])):
                raise ValueError("dangling trace reference")
        prepared.append({"system": system, "enclosure": enclosure, "payload": payload,
                         "nodes": nodes, "links": links, "traces": traces})
    return sorted(prepared, key=lambda row: (row["system"], row["enclosure"]))


def _controller_conflicts(rows: list[dict[str, Any]]) -> set[str]:
    evidence: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        for node in row["nodes"].values():
            if node["kind"] != "controller":
                continue
            raw, metrics = node.get("raw", {}), node.get("metrics", {})
            for field, value in [
                ("raw_id", node.get("raw_id")),
                *((field, raw.get(field)) for field in ("name", "unit", "device", "pci_address")),
                ("pci_address", metrics.get("pci_address")),
            ]:
                if value is not None and value != "":
                    evidence[node["id"], field].add(_json(value))
    return {node_id for (node_id, _), values in evidence.items() if len(values) > 1}


def _node_key(row: dict[str, Any], node: dict[str, Any], conflicts: set[str]) -> tuple[str, str]:
    system, enclosure = row["system"], row["enclosure"]
    kind, original = node["kind"], node["id"]
    if kind == "host":
        return _id("node", system, "host", original), "supplied"
    if kind == "controller":
        if original in conflicts:
            return _id("node", system, enclosure, "controller", original), "conflicting"
        return _id("node", system, "controller", original), "supplied"
    if kind == "expander":
        controller = node.get("controller_id")
        address = node.get("raw", {}).get("sas_address")
        raw_id = node.get("raw_id")
        if node.get("raw", {}).get("controller_id") not in (None, controller):
            return _id("node", system, enclosure, kind, original), "conflicting"
        if controller and controller not in conflicts and isinstance(address, str) and re.fullmatch(r"[0-9a-fA-F]{16}", address) and raw_id in (None, address):
            return _id("node", system, "expander", controller, address), "supplied"
    return _id("node", system, enclosure, kind, original), "enclosure_local"


def _unknown(reason: str) -> dict[str, Any]:
    return {"state": "unknown", "lane_count": None, "source_phys": None,
            "target_phys": None, "phy_pairs": None, "reason": reason}


def _phy_number(value: Any) -> int | None:
    # Parser outputs are decimal strings. Reject signs, booleans and enormous
    # tokens before conversion; the current parser's numeric bound is not a PHY
    # range contract, so no new hardware range or truncation policy is invented.
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _handle_identity(value: Any) -> str | None:
    # Canonicalize only interpreted identity; keep supplied observations intact.
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{4}", value):
        return value.lower()
    return None


def _reported_pairs(source: dict[str, Any], target: dict[str, Any]) -> list[tuple[int, int]] | None:
    raw, peer = source.get("raw", {}), target.get("raw", {})
    handle = _handle_identity(peer.get("dev_handle"))
    own_handle = _handle_identity(raw.get("dev_handle"))
    if handle is None or own_handle is None or handle == own_handle:
        return None
    phys = raw.get("phys")
    if not isinstance(phys, list):
        return None
    pairs = []
    seen_phys = set()
    for phy in phys:
        if not isinstance(phy, dict):
            return None
        local = _phy_number(phy.get("phy"))
        if local is None or local in seen_phys:
            return None
        seen_phys.add(local)
        if _handle_identity(phy.get("dev_handle")) != handle:
            continue
        remote = _phy_number(phy.get("remote_phy"))
        if local is None or remote is None or phy.get("device") not in {"Expander", "Edge Expander", "Fanout Expander", "SMP Target"}:
            return None
        pairs.append((local, remote))
    if not pairs or len({a for a, _ in pairs}) != len(pairs) or len({b for _, b in pairs}) != len(pairs):
        return None
    return sorted(pairs)


def _wide_port(link: dict[str, Any], nodes: dict[str, dict[str, Any]], ambiguous_handles: set[tuple[str, str, str]]) -> dict[str, Any]:
    source, target = nodes[link["source"]], nodes[link["target"]]
    if source["kind"] != "expander" or target["kind"] != "expander":
        return _unknown("endpoint_phy_rows_not_in_snapshot")
    # Every observation of both shared endpoints must agree. A complete report
    # must not erase another shelf's missing or contradictory evidence.
    results = []
    for endpoint in (source, target):
        if endpoint["identity_state"] != "supplied":
            return _unknown("endpoint_identity_not_shared")
    left = {o["enclosure_id"]: o["payload"] for o in source["observations"]}
    right = {o["enclosure_id"]: o["payload"] for o in target["observations"]}
    if left.keys() != right.keys():
        return _unknown("endpoint_reports_not_coincident")
    for enclosure in sorted(left):
        for node in (left[enclosure], right[enclosure]):
            if (enclosure, node.get("controller_id"), _handle_identity(node.get("raw", {}).get("dev_handle"))) in ambiguous_handles:
                return _unknown("ambiguous_endpoint_handle")
        forward, reverse = _reported_pairs(left[enclosure], right[enclosure]), _reported_pairs(right[enclosure], left[enclosure])
        if forward is None or reverse is None or forward != sorted((b, a) for a, b in reverse):
            return _unknown("missing_or_conflicting_reciprocal_phys")
        results.append(forward)
    if not results or any(pairs != results[0] for pairs in results[1:]):
        return _unknown("conflicting_endpoint_reports")
    pairs = results[0]
    return {"state": "observed", "lane_count": len(pairs), "source_phys": sorted(a for a, _ in pairs),
            "target_phys": sorted(b for _, b in pairs), "phy_pairs": [list(pair) for pair in pairs],
            "basis": "reciprocal_reported_phy_rows"}


def _compose_system(system: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    conflicts = _controller_conflicts(rows)
    nodes: dict[str, dict[str, Any]] = {}
    links: dict[str, dict[str, Any]] = {}
    enclosures = []
    handle_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        for node in row["nodes"].values():
            handle = _handle_identity(node.get("raw", {}).get("dev_handle"))
            if node["kind"] == "expander" and handle is not None and node.get("controller_id"):
                handle_counts[row["enclosure"], node["controller_id"], handle] += 1
    ambiguous_handles = {key for key, count in handle_counts.items() if count > 1}
    for row in rows:
        node_map, link_map = {}, {}
        used_keys = set()
        enclosure = row["enclosure"]
        for original, node in sorted(row["nodes"].items()):
            key, state = _node_key(row, node, conflicts)
            if key in used_keys:
                raise ValueError("ambiguous shared node identity in enclosure")
            used_keys.add(key)
            node_map[original] = key
            entry = nodes.setdefault(key, {"id": key, "kind": node["kind"], "identity_state": state, "observations": []})
            entry["observations"].append({"enclosure_id": enclosure, "payload": node})
        for original, link in sorted(row["links"].items()):
            source, target = node_map[link["source"]], node_map[link["target"]]
            # Endpoint equality does not prove that two distinct supplied
            # edges/ports are one edge. Share only the same supplied link ID.
            key = _id("link", system, source, target, link["kind"], original)
            link_map[original] = key
            entry = links.setdefault(key, {"id": key, "source": source, "target": target, "kind": link["kind"], "observations": []})
            entry["observations"].append({"enclosure_id": enclosure, "payload": link})
        traces = [{"id": _id("trace", system, enclosure, original),
                   "node_ids": [node_map[key] for key in trace.get("node_ids", [])],
                   "link_ids": [link_map[key] for key in trace.get("link_ids", [])],
                   "observation": trace} for original, trace in sorted(row["traces"].items())]
        enclosures.append({"enclosure_id": enclosure, "snapshot": {key: value for key, value in row["payload"].items() if key not in {"nodes", "links", "traces"}},
                           "node_ids": sorted(set(node_map.values())), "link_ids": sorted(set(link_map.values())), "traces": traces})
    for link in links.values():
        link["wide_port"] = _wide_port(link, nodes, ambiguous_handles)
    return {"system_id": system, "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
            "links": sorted(links.values(), key=lambda item: item["id"]), "enclosures": enclosures,
            "warnings": ["Conflicting controller identity retained enclosure-local: " + key for key in sorted(conflicts)]}


def compose_system_sas_fabrics(
    payloads: list[dict[str, Any]], *, max_input_bytes: int, max_depth: int,
) -> dict[str, Any]:
    """Compose model_dump(mode='json') snapshots, without I/O or mutation.

    Both budgets are mandatory caller choices, not shipping admission defaults.
    Any malformed/over-budget input refuses the whole composition with ValueError.
    Lane count describes only matching reciprocal *reported* PHY rows, not cable
    width, active lanes, topology completeness, or a new verification of an edge.
    CORE controller PHY rows are currently dropped by the producer, so those
    links remain unknown. Original per-enclosure graphs are never replaced.
    """
    payloads = _bounded_copy(payloads, max_input_bytes, max_depth)
    if not isinstance(payloads, list):
        raise ValueError("composition requires a list of snapshot payloads")
    prepared = _prepare(payloads)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in prepared:
        groups[row["system"]].append(row)
    return {"schema": "system-sas-fabric-draft-v1", "inactive": True,
            "topology_policy": "supplied_edges_only", "lane_count_scope": "reciprocal_reported_rows_only",
            "systems": [_compose_system(system, rows) for system, rows in sorted(groups.items())]}
