#!/usr/bin/env python3
"""Storage Fabric Topology backend mutation run (#1516).

Each mutant is one textual change to a topology producer module. The run
copies the tracked tree to a temporary directory once per worker, plants one
mutant at a time there (never in the checkout), runs the producer test
modules, and restores the file. A mutant is killed when a test assertion
fails with it in place.

The run fails when the unmutated modules fail or run no test, when a mutant's
anchor is missing or not unique, when the mutated module does not compile,
when a mutant only breaks test loading (an import error is not a kill), when
a mutant survives that topology_equivalent_mutants.json does not list with a
reason, and when a listed equivalent is killed or no longer exists.

Usage: python tests/mutation/run_topology_backend_mutants.py [--jobs N] [NAME ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    "tests.test_core_fabric_direct_paths",
    "tests.test_sas_fabric",
    "tests.test_core_fabric_legacy_controller",
    "tests.test_linux_sas_transport",
    "tests.test_fabric_topology_fixture",
    "tests.test_core_adapter_summary_admission",
]
F = "app/services/sas_fabric.py"
C = "app/services/sas_fabric_core.py"
E = "app/services/sas_fabric_enclosures.py"
L = "app/services/sas_fabric_linux_sas.py"

# name -> (file, text replaced, replacement)
MUTANTS: dict[str, tuple[str, str, str]] = {

 # own evidence
 "O1 direct falls back to position": (C, "    if own_evidence:\n        # Direct paths, and a controller carrying several of this bay's paths,\n        # require a readable matching target or an exact name.\n        return None\n", ""),
 "O2 named row target conflict ignored": (C, "if member_target_rejected or (member_target is not None and row_target is not None and row_target != member_target):\n                return None", "if False:\n                return None"),
 "O4 cross-read CAM trusted": (F, "if path_evidence_read else None,", ","),
 "O6 controllers total exact": (F, "    if members_by_slot.not_read_reasons:\n        controller_read[\"counts\"][\"controllers\"] = \"partial\"", "    if False:\n        controller_read[\"counts\"][\"controllers\"] = \"partial\""),
 "O7 paths total exact": (F, "            or any(not member.controller_label for members in members_by_slot.values() for member in members)):", "            or False):"),
 "O8 no controller budget": (F, "return set(ordered[:MAX_FABRIC_CONTROLLERS]), set(ordered[MAX_FABRIC_CONTROLLERS:])", "return set(ordered), set()"),
 "O9 peer not API-owned": (F, "peers = set(bay_peer_devices(raw, {own.lower()}, owners, slot)).intersection(api_peers)", "peers = set(bay_peer_devices(raw, {own.lower()}, owners, slot))"),
 "O10 digest no alias collapse": (F, "    canonical = {canonicalize_ssh_command(command): output\n                 for command, output in ssh_outputs.items() if output is not None}", "    canonical = {command: output\n                 for command, output in ssh_outputs.items() if output is not None}"),
 # enclosure keying
 "E1 handle in key": (E, "        physical = board(identity)\n        replacements", "        physical = board(identity + str(node.raw.get('enc_handle')))\n        replacements"),
 "E2 observations deleted": (E, "    nodes.update(by_id)\n", "    nodes.update(by_id)\n    for key in replacements:\n        nodes.pop(key, None)\n"),
 "E3 counts summed": (E, "            if values and all(value == values[0] for value in values):\n                physical.metrics[key] = values[0]", "            if values and all(isinstance(v, int) for v in values):\n                physical.metrics[key] = sum(values)\n            elif values and all(value == values[0] for value in values):\n                physical.metrics[key] = values[0]"),
 "E4 label as identity": (E, "    return value if re.fullmatch(r\"[0-9a-f]{16}\", value) else None", "    return value or None"),
 "E5 contradictory SES picks first": (E, "        if len(identities) != 1:\n            continue", "        if not identities:\n            continue"),
 "E7 observation not marked": (E, "        node.raw[\"physical_enclosure_id\"] = physical.id\n        link_id", "        link_id"),
 # transport census
 "T1 far end not verified": (L, "            if remote is not None and (remote.name != attached[0][0] or remote.owner_path != port.path):\n                remote = None", "            pass"),
 "T2 basis always reported": (L, "\"target_basis\": \"reported\" if remote else \"not_read\",", "\"target_basis\": \"reported\","),
 "T3 census not charged": (L, "            facts += 18 + 3 * sum(name.startswith(\"phy-\") for name in row.links)", "            facts += 0"),
 "T4 unread port says read": (L, "\"state\": \"read\" if members else \"not_read\",", "\"state\": \"read\","),
 "T5 ambiguous far end picks first": (L, "remote = remotes.get(attached[0][1]) if len(attached) == 1 else None", "remote = remotes.get(attached[0][1]) if attached else None"),
 "T6 lane basis dropped": (L, "        target_basis=\"not_read\",\n", ""),
 # identity before scope (#1504)
 "I1 adapter handle ambiguity unmarked": (C, "        if len(found) > 1 or found[0][\"conflict\"]:\n            states[phy] = \"ambiguous\"", "        if False:\n            states[phy] = \"ambiguous\""),
 "I2 adapter handle device kind unmarked": (C, "        elif kind != \"expander\":\n            states[phy] =", "        elif False:\n            states[phy] ="),
 "I3 adapter handle states dropped": (C, "            if states:\n                controller[\"device_handle_states\"] = states", "            if False:\n                controller[\"device_handle_states\"] = states"),
 "I4 transport address ambiguity unmarked": (L, "len(slots_by_address.get(address, ())) > 1:\n                record[\"remote_address_state\"] = \"ambiguous\"", "len(slots_by_address.get(address, ())) > 1:\n                pass"),
 "I5 transport address per slot ignored": (L, "            if record[\"remote_kind\"] == \"end_device\" and len(slots_by_address.get(address, ())) > 1:", "            if record[\"remote_kind\"] == \"end_device\" and len(slots_by_address.get(address, ())) > 2:"),

 "O3 expander parent filter off": (C, "    parent = _mpr_handle(device_context.get(\"parent\"))\n    expander_ids = [node_id for node_id in expander_ids\n                    if node_id in nodes and parent is not None\n                    and _mpr_handle(nodes[node_id].raw.get(\"dev_handle\")) == parent]\n    if len(expander_ids) != 1:\n        expander_ids = []\n", ""),
 "O5 position join for every member": (F, "own_evidence=direct_members or not position_allowed or snapshot_controllers[controller_name] != 1,", "own_evidence=False,"),
 "M1 multipath sibling position join": (F, "own_evidence=direct_members or not position_allowed or snapshot_controllers[controller_name] != 1,", "own_evidence=direct_members or not position_allowed,"),
 "C1 unread owner ignored": (F, "bool(snapshot_members) and None not in snapshot_controllers", "bool(snapshot_members)"),
 "C2 admission limit ignored": (F, "            and slot.slot not in members_by_slot.path_limited\n", ""),
 "C3 retained members counted": (F, "snapshot_members = list(slot.multipath.members) if slot.multipath and slot.multipath.members else []", "snapshot_members = list(slot_members)"),
 "M2 multipath later-read detail trusted": (F, "port_detail_read = direct_members or path_evidence_read or", "port_detail_read = True or"),
 "M3 multipath unread row not marked": (F, "        if evidence_read or not member.controller_label:\n            return limited\n", "        return limited\n"),
 "M4 enclosure disagreement not listed": (E, "                unread.add(key)\n", ""),
 # one device per identity, whichever reply names it (#1504)
 "I6 enclosure processors not read": (C, "            add(row.get(\"sep_handle\"), \"sep\", None, None)", "            pass"),
 "I7 expander-typed device rows dropped": (C, "        for row in payload.get(\"devices\") or []:\n            add(", "        for row in [item for item in payload.get(\"devices\") or [] if _mpr_device_kind(item.get(\"device\")) != \"expander\"]:\n            add("),
 "I8 parent or type contradiction unmarked": (C, "        if (parent and own and parent != own) or not agrees:", "        if False:"),
 "I9 enclosure processor said a disk": (C, "{\"disk\": \"end_device\", \"sep\": \"sep\"}", "{\"disk\": \"end_device\", \"sep\": \"end_device\"}"),
 "I10 lanes kept for an ambiguous handle": (C, "                or {child_handle, parent_handle} & self._ambiguous):", "                or False):"),
 "I11 inventory identities not marked": (L, "    projection.mark_inventory_identities(snapshot)\n", ""),
 "I12 enclosure contradiction ignored": (L, "            if len(found) > 1 or (enclosure and any(item[0] and item[0] != enclosure for item in found)):", "            if len(found) > 1:"),
 "I13 one address with two parents is one device": (C, "                held[\"conflict\"] = True", "                pass"),
 "I14 an unaddressed record is always a device of its own": (C, "            if not fits:\n                loose.append(dict(record))", "            loose.append(dict(record))"),
}


def _environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _copy_tree(into: Path) -> None:
    files = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
    for name in filter(None, files):
        source = ROOT / os.fsdecode(name)
        if source.is_file():
            target = into / os.fsdecode(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _run_modules(tree: Path) -> tuple[int, str]:
    result = subprocess.run([sys.executable, "-m", "unittest", *MODULES], cwd=tree, env=_environment(),
                            capture_output=True, text=True, timeout=1800)
    return result.returncode, result.stderr


def _ran(stderr: str) -> int:
    found = re.search(r"^Ran (\d+) tests?", stderr, re.M)
    return int(found[1]) if found else 0


def _judge(tree: Path, name: str) -> dict[str, object]:
    path, old, new = MUTANTS[name]
    file = tree / path
    source = file.read_text(encoding="utf-8")
    if source.count(old) != 1:
        return {"name": name, "verdict": "error", "detail": f"anchor found {source.count(old)} times in {path}"}
    mutated = source.replace(old, new)
    try:
        compile(mutated, str(file), "exec")
    except SyntaxError as error:
        return {"name": name, "verdict": "error", "detail": f"the mutated module does not compile: {error}"}
    file.write_text(mutated, encoding="utf-8")
    try:
        code, stderr = _run_modules(tree)
    finally:
        file.write_text(source, encoding="utf-8")
    failed = re.findall(r"^(FAIL|ERROR): (\S+) \((\S+)\)", stderr, re.M)
    loading = [item for item in failed if "_FailedTest" in item[2] or "loader" in item[2]]
    if not _ran(stderr):
        return {"name": name, "verdict": "error", "detail": "no test count"}
    if loading:
        return {"name": name, "verdict": "error", "detail": f"a test module did not load: {loading[0][1]}"}
    if code and failed:
        kinds = sorted({kind for kind, _test, _where in failed})
        return {"name": name, "verdict": "killed", "detail": f"{len(failed)} {'/'.join(kinds)} of {_ran(stderr)}: {failed[0][1]}"}
    if code:
        return {"name": name, "verdict": "error", "detail": f"exit {code} with no failing test"}
    return {"name": name, "verdict": "survived", "detail": f"{_ran(stderr)} tests passed"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--report")
    options = parser.parse_args()
    equivalent = json.loads((Path(__file__).with_name("topology_equivalent_mutants.json")).read_text(encoding="utf-8"))["backend"]
    problems = [f"{name} is listed as equivalent but is not a mutant" for name in equivalent if name not in MUTANTS]
    problems += [f"{name} is listed as equivalent without a reason" for name, reason in equivalent.items() if not str(reason).strip()]
    unknown = [name for name in options.names if name not in MUTANTS]
    if unknown:
        print(f"FAIL unknown mutant {', '.join(unknown)}")
        return 1
    names = options.names or sorted(MUTANTS)
    with tempfile.TemporaryDirectory(prefix="topology-backend-mutants-") as scratch:
        trees = [Path(scratch) / f"tree-{index}" for index in range(min(options.jobs, len(names)))]
        for tree in trees:
            _copy_tree(tree)
        code, stderr = _run_modules(trees[0])
        print(f"python {sys.version.split()[0]}; {len(names)} of {len(MUTANTS)} mutants; {len(trees)} at a time")
        print(f"stock: {_ran(stderr)} tests, exit {code}")
        if code or not _ran(stderr):
            print(stderr[-3000:])
            print("FAIL the unmutated modules do not pass")
            return 1
        queue = list(names)
        results: list[dict[str, object]] = []

        def worker(tree: Path) -> None:
            while True:
                try:
                    name = queue.pop(0)
                except IndexError:
                    return
                result = _judge(tree, name)
                results.append(result)
                print(f"{name:58} {result['verdict']:9} {result['detail']}", flush=True)

        with ThreadPoolExecutor(max_workers=len(trees)) as pool:
            list(pool.map(worker, trees))
    by = {verdict: sorted(str(item["name"]) for item in results if item["verdict"] == verdict) for verdict in ("killed", "survived", "error")}
    problems += [f"{name} could not be run as planted" for name in by["error"]]
    problems += [f"{name} survived and is not listed as equivalent" for name in by["survived"] if name not in equivalent]
    problems += [f"{name} is listed as equivalent but was killed: remove it from the list" for name in by["killed"] if name in equivalent]
    print(f"\n{len(by['killed'])} killed, {len(by['survived'])} survived, {len(by['error'])} errors, of {len(names)}")
    if options.report:
        Path(options.report).write_text(json.dumps({"results": sorted(results, key=lambda item: str(item["name"])), "problems": problems}, indent=1) + "\n", encoding="utf-8")
    for problem in problems:
        print(f"FAIL {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
