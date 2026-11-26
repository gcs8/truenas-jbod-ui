#!/usr/bin/env python3
"""Storage Fabric Topology backend mutation run (#1516).

Each mutant is one textual change to a topology producer module. The run
copies the tracked tree to a temporary directory once per worker, plants one
mutant at a time there (never in the checkout), runs the producer test
modules, and restores the file. A mutant is killed when a test assertion
fails with it in place.

A mutant counts as killed only when a collected test fails on the mutated
behaviour: an assertion failure (a FAIL), or an error whose exception was
thrown from product code (app/, or the mutated file). The run fails when the unmutated modules fail or run no
test, when a mutant's anchor is missing or not unique, when the mutated
module does not compile, when a test module does not load, when a missing
module or an import error appears anywhere in the run (even inside a test
that ran), when a mutant only makes tests error from test code or a product exception, when a mutant survives that topology_equivalent_mutants.json does
not list with a reason, and when a listed equivalent is killed or no longer
exists.

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
 "I1 adapter handle ambiguity unmarked": (C, "        if found and (len(found) > 1 or found[0][\"conflict\"]):\n            states[phy] = \"ambiguous\"", "        if False:\n            states[phy] = \"ambiguous\""),
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
 # an unread reply never resolves a handle (#1504)
 "I15 an unread list lets the rest name one device": (C, "        if unread and handle is not None and handle != \"0000\":", "        if False:"),
 "I16 an expander only the device list names is absent": (C, "        elif kind == \"expander\" and not found[0][\"listed\"]:", "        elif False:"),
 "I17 ambiguous unit handles not delivered": (C, "                    controller[\"ambiguous_device_handles\"] = ambiguous", "                    pass"),
 "I18 list states not delivered with the HBA": (C, "                controller[\"mpr_lists\"] = dict(payload[\"lists\"])", "                pass"),
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


def _run_modules(tree: Path, modules: list[str] | None = None, verbose: bool = False) -> tuple[int, str]:
    result = subprocess.run([sys.executable, "-m", "unittest", *(["-v"] if verbose else []), *(modules or MODULES)],
                            cwd=tree, env=_environment(), capture_output=True, text=True, timeout=1800)
    return result.returncode, result.stderr


def stock_problems(code: int, stderr: str, modules: list[str]) -> list[str]:
    """Why the unmutated run cannot stand as the baseline, if it cannot."""
    problems = [] if code == 0 else [f"exit {code}"]
    if not _ran(stderr):
        problems.append("no test count")
    if _IMPORT.search(stderr):
        problems.append("a missing module or an import error")
    # Every selected module ran at least one test of its own (-v names each).
    problems += [f"{module} ran no test" for module in modules if not re.search(rf"\({re.escape(module)}\.", stderr)]
    return problems


def _ran(stderr: str) -> int:
    found = re.search(r"^Ran (\d+) tests?", stderr, re.M)
    return int(found[1]) if found else 0


# One block per failed or errored test: its heading and its traceback.
# A subtest's heading carries its parameters after the test's own name.
_BLOCK = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]*)\)[^\n]*\n-+\n(.*?)(?=^=+$|^-+\nRan )", re.M | re.S)
_IMPORT = re.compile(r"\b(ModuleNotFoundError|ImportError)\b")


_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+', re.M)


def _thrown_from_product(trace: str, tree: Path | None, product: set[str]) -> bool:
    """Whether the innermost frame of a traceback is product code."""
    frames = _FRAME.findall(trace)
    if not frames:
        return False
    innermost = Path(frames[-1])
    try:
        relative = innermost.resolve().relative_to(tree.resolve()).as_posix() if tree else innermost.as_posix()
    except ValueError:
        return False
    return relative.startswith("app/") or relative in product


def classify(code: int, stderr: str, tree: Path | None = None, product: set[str] | None = None) -> tuple[str, str]:
    """The verdict on one mutated run: killed, survived, or error (never a kill)."""
    ran = _ran(stderr)
    blocks = _BLOCK.findall(stderr)
    if not ran:
        return "error", "no test count"
    loading = [test for _kind, test, where, _trace in blocks if "_FailedTest" in where or "loader" in where]
    if loading:
        return "error", f"a test module did not load: {loading[0]}"
    if _IMPORT.search(stderr):
        return "error", "a missing module or an import error in the run is not a kill"
    asserted = [test for kind, test, _where, trace in blocks if kind == "FAIL" and "AssertionError" in trace]
    thrown = [test for kind, test, _where, trace in blocks if kind == "ERROR" and _thrown_from_product(trace, tree, product or set())]
    if code and (asserted or thrown):
        return "killed", f"{len(asserted)} assertion failures and {len(thrown)} product exceptions of {ran}: {(asserted or thrown)[0]}"
    if code and blocks:
        return "error", f"{len(blocks)} tests errored from test code, with no assertion failed and no product exception: {blocks[0][1]}"
    if code:
        return "error", f"exit {code} with no failing test"
    return "survived", f"{ran} tests passed"


def _judge(tree: Path, name: str, mutants: dict[str, tuple[str, str, str]] | None = None,
           modules: list[str] | None = None) -> dict[str, object]:
    path, old, new = (mutants or MUTANTS)[name]
    file = tree / path
    if not file.is_file():
        return {"name": name, "verdict": "error", "detail": f"the mutated file {path} is missing"}
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
        code, stderr = _run_modules(tree, modules)
    finally:
        file.write_text(source, encoding="utf-8")
    verdict, detail = classify(code, stderr, tree, {path})
    return {"name": name, "verdict": verdict, "detail": detail}


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
        code, stderr = _run_modules(trees[0], verbose=True)
        print(f"python {sys.version.split()[0]}; {len(names)} of {len(MUTANTS)} mutants; {len(trees)} at a time")
        print(f"stock: {_ran(stderr)} tests, exit {code}")
        if stock_problems(code, stderr, MODULES):
            print("; ".join(stock_problems(code, stderr, MODULES)))
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
