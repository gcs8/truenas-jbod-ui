#!/usr/bin/env python3
"""Pinned native workflow scanners and an exact-source zizmor baseline.

Only Python's standard library is required. See .github/WORKFLOW_LINT.md.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = {
    "actionlint": {
        "version": "1.7.12",
        "url": "https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz",
        "sha256": "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        "member": "actionlint",
        "version_args": ["-version"],
        "version_line": "1.7.12",
    },
    "shellcheck": {
        "version": "0.11.0",
        "url": "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.xz",
        "sha256": "8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198",
        "member": "shellcheck-v0.11.0/shellcheck",
        "version_args": ["--version"],
        "version_line": "version: 0.11.0",
    },
    "zizmor": {
        "version": "1.33.1",
        "url": "https://github.com/zizmorcore/zizmor/releases/download/v1.33.1/zizmor-x86_64-unknown-linux-gnu.tar.gz",
        "sha256": "3e6a301c7a820a2501915f5a9de8c0486da82bdec7965f068477538a98743d43",
        "member": "zizmor",
        "version_args": ["--version"],
        "version_line": "zizmor 1.33.1",
    },
}
SEVERITY_EXITS = {"Informational": 11, "Low": 12, "Medium": 13, "High": 14}
CONFIDENCES = {"Low", "Medium", "High"}
ENTRY_KEYS = {"rule", "path", "line", "column", "severity", "confidence", "source_sha256", "reason"}


class LintError(Exception):
    """A scanner error, unexpected finding, or invalid baseline."""


def require(condition, message):
    if not condition:
        raise LintError(message)


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected), "unexpected JSON object schema")


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise LintError("missing or malformed scanner/baseline JSON") from exc


def workflow_paths(root):
    paths = sorted(path.relative_to(root).as_posix()
                   for path in (root / ".github/workflows").rglob("*")
                   if path.suffix in {".yaml", ".yml"} and path.is_file())
    require(bool(paths), "no workflow inputs found")
    for name in paths:
        require((root / name).resolve().is_relative_to(root.resolve()), "workflow escapes repository")
    return paths


def source_sha(root, path):
    return hashlib.sha256((root / path).read_bytes()).hexdigest()


def identity(entry):
    return tuple(entry[key] for key in sorted(ENTRY_KEYS - {"reason"}))


def load_baseline(root):
    data = strict_json((root / ".github/workflow-lint-baseline.json").read_text(encoding="utf-8"))
    keys(data, {"version", "scanner", "format", "findings"})
    require(type(data["version"]) is int and data["version"] == 1, "unsupported baseline version")
    require(data["scanner"] == "zizmor 1.33.1" and data["format"] == "json-v1", "unsupported scanner contract")
    require(isinstance(data["findings"], list), "baseline findings must be an array")
    paths = workflow_paths(root)
    seen = set()
    for entry in data["findings"]:
        keys(entry, ENTRY_KEYS)
        require(all(isinstance(entry[key], str) and entry[key].strip()
                    for key in ENTRY_KEYS - {"line", "column"}), "baseline strings require nonempty values")
        require(re.fullmatch(r"[a-z][a-z0-9-]*", entry["rule"]) is not None, "invalid baseline rule")
        require(entry["path"] in paths, "baseline path is not a selected workflow")
        require(all(type(entry[key]) is int and entry[key] > 0 for key in ("line", "column")), "invalid baseline position")
        require(entry["severity"] in SEVERITY_EXITS and entry["confidence"] in CONFIDENCES, "invalid baseline determination")
        require(re.fullmatch(r"[0-9a-f]{64}", entry["source_sha256"]) is not None, "invalid baseline source hash")
        require(source_sha(root, entry["path"]) == entry["source_sha256"], "baseline source changed: " + entry["path"])
        item = identity(entry)
        require(item not in seen, "duplicate baseline entry")
        seen.add(item)
    return data["findings"]


def check_actionlint(result):
    findings = strict_json(result.stdout)
    require(isinstance(findings, list), "actionlint report is not an array")
    require(result.returncode == 0 and not result.stderr.strip(), "actionlint failed; inspect the native report")
    require(not findings, "actionlint emitted findings despite exit zero")
    return 0


def zizmor_identity(root, finding, paths):
    # v1.33.1's explicit json-v1 serializer. Refuse a future incompatible shape.
    keys(finding, {"ident", "desc", "url", "determinations", "locations", "ignored", "fixes"})
    require(finding["ignored"] is False, "native ignores must not bypass the baseline")
    require(all(isinstance(finding[key], str) and finding[key] for key in ("ident", "desc", "url")), "invalid finding metadata")
    determinations = finding["determinations"]
    keys(determinations, {"severity", "confidence", "persona"})
    require(determinations["severity"] in SEVERITY_EXITS and determinations["confidence"] in CONFIDENCES
            and determinations["persona"] == "Regular", "unsupported zizmor determination")
    require(isinstance(finding["fixes"], list), "invalid zizmor fixes")
    require(isinstance(finding["locations"], list) and finding["locations"], "finding locations are missing")
    primary = []
    for location in finding["locations"]:
        keys(location, {"symbolic", "concrete"})
        symbolic, concrete = location["symbolic"], location["concrete"]
        keys(symbolic, {"key", "annotation", "route", "feature_kind", "kind"})
        keys(symbolic["key"], {"Local"})
        keys(symbolic["key"]["Local"], {"verbatim_path"})
        path = symbolic["key"]["Local"]["verbatim_path"]
        require(path in paths, "finding refers to an unselected workflow")
        require(symbolic["kind"] in {"Primary", "Related", "Hidden"}, "unknown location kind")
        keys(concrete, {"location", "feature", "comments"})
        require(isinstance(concrete["feature"], str) and isinstance(concrete["comments"], list), "invalid concrete location")
        span = concrete["location"]
        keys(span, {"start_point", "end_point", "offset_span"})
        for point in (span["start_point"], span["end_point"]):
            keys(point, {"row", "column"})
            require(all(type(value) is int and value >= 0 for value in point.values()), "invalid native position")
        keys(span["offset_span"], {"start", "end"})
        start, end = span["offset_span"]["start"], span["offset_span"]["end"]
        require(type(start) is int and type(end) is int and 0 <= start <= end <= (root / path).stat().st_size,
                "invalid native offset span")
        if symbolic["kind"] == "Primary":
            primary.append((path, span["start_point"]))
    require(len(primary) == 1, "expected exactly one primary finding location")
    path, point = primary[0]
    return {
        "rule": finding["ident"], "path": path, "line": point["row"] + 1, "column": point["column"] + 1,
        "severity": determinations["severity"], "confidence": determinations["confidence"],
        "source_sha256": source_sha(root, path),
    }


def check_zizmor(root, result, baseline, paths):
    findings = strict_json(result.stdout)
    require(isinstance(findings, list), "zizmor report is not an array")
    entries = [zizmor_identity(root, finding, paths) for finding in findings]
    expected_exit = max((SEVERITY_EXITS[entry["severity"]] for entry in entries), default=0)
    require(result.returncode == expected_exit, "zizmor error or exit/report severity mismatch")
    actual = [identity(entry) for entry in entries]
    require(len(set(actual)) == len(actual), "duplicate native finding identity")
    for entry in baseline:
        require(source_sha(root, entry["path"]) == entry["source_sha256"], "baseline source changed: " + entry["path"])
    accepted = {identity(entry) for entry in baseline}
    require(set(actual) == accepted, "new or stale zizmor findings; review native report and exact source")
    return len(entries)


def scanner_commands(tools_dir, paths):
    return {
        "actionlint": [str(tools_dir / "actionlint"), "-format", "{{json .}}",
                       "-shellcheck", str(tools_dir / "shellcheck"), *paths],
        "zizmor": [str(tools_dir / "zizmor"), "--offline", "--no-config", "--no-ignores",
                   "--strict-collection", "--format=json-v1", "--persona=regular", "--no-progress", *paths],
    }


def emit_safe_diagnostics(root, name, result, paths):
    """Expose locations, never native messages, snippets, URLs or stderr."""
    print(f"{name}: native exit {result.returncode}")
    try:
        findings = strict_json(result.stdout)
        require(isinstance(findings, list), "diagnostic array missing")
        records = []
        for finding in findings:
            if name == "actionlint":
                require(isinstance(finding, dict), "invalid diagnostic")
                rule = finding.get("kind")
                file, line, column = (finding.get(key) for key in ("filepath", "line", "column"))
                require(isinstance(rule, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", rule), "invalid rule")
                message = finding.get("message", "")
                code = re.search(r"\bSC\d{4}\b", message) if isinstance(message, str) else None
                if code:
                    rule += "/" + code.group()
            else:
                entry = zizmor_identity(root, finding, paths)
                rule, file, line, column = (entry[key] for key in ("rule", "path", "line", "column"))
                require(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", rule), "invalid rule")
            require(file in paths and all(type(value) is int and value > 0 for value in (line, column)),
                    "invalid diagnostic location")
            records.append({"scanner": name, "rule": rule, "file": file, "line": line, "column": column})
    except (LintError, OSError, ValueError, KeyError, TypeError, RecursionError):
        print(f"{name}: diagnostic metadata unavailable")
        return
    for record in records[:100]:
        # JSON escaping prevents filenames from adding log lines or workflow commands.
        print(json.dumps(record, sort_keys=True))
    if len(records) > 100:
        print(f"{name}: {len(records) - 100} additional diagnostic locations omitted")


def check_versions(tools_dir):
    for name, tool in TOOLS.items():
        try:
            result = subprocess.run([str(tools_dir / name), *tool["version_args"]],
                                    capture_output=True, text=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LintError("cannot execute pinned tool: " + name) from exc
        require(result.returncode == 0 and tool["version_line"] in result.stdout.splitlines(), "wrong tool version: " + name)


def install_tools(destination):
    # Digests come from official GitHub release asset metadata, not a downloaded
    # mutable installer. Read only the selected regular-file member after hashing.
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, tool in TOOLS.items():
        with urllib.request.urlopen(tool["url"], timeout=60) as response:
            archive = response.read()
        require(hashlib.sha256(archive).hexdigest() == tool["sha256"], "archive checksum mismatch: " + name)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.getmember(tool["member"])
            require(member.isfile(), "tool archive member is not a regular file")
            with tar.extractfile(member) as source:
                (destination / name).write_bytes(source.read())
        (destination / name).chmod(0o755)
    check_versions(destination)


def scan(root, tools_dir, report_dir):
    paths = workflow_paths(root)
    baseline = load_baseline(root)
    check_versions(tools_dir)
    report_dir.mkdir(parents=True, exist_ok=False)
    receipt = {"workflows": paths, "scanners": {}}
    failures = []
    for name, command in scanner_commands(tools_dir, paths).items():
        try:
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(name + " execution failed")
            receipt["scanners"][name] = {"error": type(exc).__name__}
            continue
        (report_dir / (name + ".json")).write_text(result.stdout, encoding="utf-8")
        (report_dir / (name + ".stderr")).write_text(result.stderr, encoding="utf-8")
        status = {"native_exit": result.returncode, "gate": "fail"}
        receipt["scanners"][name] = status
        try:
            count = check_actionlint(result) if name == "actionlint" else check_zizmor(root, result, baseline, paths)
        except LintError as exc:
            failures.append(name + ": " + str(exc))
            emit_safe_diagnostics(root, name, result, paths)
        else:
            status.update(gate="pass", findings=count)
            print(f"{name}: native exit {result.returncode}, {count} findings, gate pass")
    (report_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    require(not failures, "; ".join(failures))
    print(f"Workflow lint passed for all {len(paths)} workflow files.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-tools", type=Path)
    parser.add_argument("--tools-dir", type=Path)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.install_tools is not None:
            require(args.tools_dir is None and args.report_dir is None, "installation and scanning are separate invocations")
            install_tools(args.install_tools.resolve())
        else:
            require(args.tools_dir is not None and args.report_dir is not None, "scanning requires --tools-dir and --report-dir")
            scan(ROOT, args.tools_dir.resolve(), args.report_dir.resolve())
    except (LintError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        # Native reports stay in the report directory. Do not echo source snippets.
        print(f"Workflow lint failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
