#!/usr/bin/env python3
"""Run the repository's three dependency audits with expiring, package-scoped ignores."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEVERITIES = ("info", "low", "moderate", "high", "critical")
GHSA = r"GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}"
PYTHON_ADVISORY = rf"(?:{GHSA}|CVE-\d{{4}}-\d{{4,}}|PYSEC-\d{{4}}-\d+)"


class AuditError(ValueError):
    """An invalid policy or incomplete scanner result, never a clean audit."""


def require(condition, message):
    if not condition:
        raise AuditError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def package_name(value, ecosystem):
    pattern = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?" if ecosystem == "python" else (
        r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
    )
    require(isinstance(value, str) and re.fullmatch(pattern, value), "invalid package name")
    return re.sub(r"[-_.]+", "-", value).lower() if ecosystem == "python" else value


def advisory_id(value, ecosystem):
    pattern = PYTHON_ADVISORY if ecosystem == "python" else rf"(?:{GHSA}|npm:[1-9]\d*)"
    require(isinstance(value, str) and re.fullmatch(pattern, value), "invalid advisory identifier")
    return value


def json_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=json_object)
    except (ValueError, TypeError) as exc:
        raise AuditError("invalid JSON report or policy") from exc


def load_policy(path, *, today=None):
    policy = parse_json(path.read_text(encoding="utf-8"))
    require(isinstance(policy, dict) and set(policy) == {"version", "ignores"}, "invalid policy fields")
    require(type(policy["version"]) is int and policy["version"] == 1, "unsupported policy version")
    require(isinstance(policy["ignores"], list), "ignores must be a list")
    today = today or datetime.now(timezone.utc).date()
    seen = set()
    entries = []
    for entry in policy["ignores"]:
        require(isinstance(entry, dict) and set(entry) == {
            "ecosystem", "package", "advisory", "reason", "expires"
        }, "each ignore requires ecosystem, package, advisory, reason and expires only")
        require(entry["ecosystem"] in ("python", "npm"), "invalid ignore ecosystem")
        ecosystem = entry["ecosystem"]
        package = package_name(entry["package"], ecosystem)
        advisory = advisory_id(entry["advisory"], ecosystem)
        require(text(entry["reason"]), "ignore reason must be nonblank")
        expiry = entry["expires"]
        require(isinstance(expiry, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry),
                "ignore expires must be a canonical ISO date")
        try:
            expires = date.fromisoformat(expiry)
        except ValueError as exc:
            raise AuditError("invalid ignore expiry date") from exc
        require(expires > today, "ignore expires must be later than today in UTC")
        key = (ecosystem, package, advisory)
        require(key not in seen, "duplicate package/advisory ignore")
        seen.add(key)
        entries.append({**entry, "package": package})
    return entries


def python_findings(report):
    require(isinstance(report, dict) and set(report) == {"dependencies", "fixes"}, "unknown Python report")
    require(report["fixes"] == [], "unexpected Python fixes")
    dependencies = report["dependencies"]
    require(isinstance(dependencies, list) and dependencies, "missing Python dependencies")
    findings = []
    for dependency in dependencies:
        require(isinstance(dependency, dict) and set(dependency) == {"name", "version", "vulns"},
                "missing, skipped or malformed Python dependency")
        package = package_name(dependency["name"], "python")
        require(text(dependency["version"]), "missing Python package version")
        require(isinstance(dependency["vulns"], list), "missing Python vulnerabilities")
        for vuln in dependency["vulns"]:
            require(isinstance(vuln, dict) and set(vuln) == {"id", "aliases", "fix_versions"},
                    "malformed Python advisory")
            require(isinstance(vuln["aliases"], list), "missing Python aliases")
            require(isinstance(vuln["fix_versions"], list) and all(map(text, vuln["fix_versions"])),
                    "invalid Python fix versions")
            identifiers = frozenset(advisory_id(value, "python") for value in [vuln["id"], *vuln["aliases"]])
            findings.append((package, identifiers))
    return findings


def npm_findings(report):
    require(isinstance(report, dict) and set(report) == {"auditReportVersion", "vulnerabilities", "metadata"},
            "unknown npm report")
    require(type(report["auditReportVersion"]) is int and report["auditReportVersion"] == 2,
            "unsupported npm audit report version")
    records = report["vulnerabilities"]
    require(isinstance(records, dict), "missing npm vulnerabilities")
    metadata = report["metadata"]
    require(isinstance(metadata, dict) and isinstance(metadata.get("dependencies"), dict), "missing npm metadata")
    total = metadata["dependencies"].get("total")
    require(type(total) is int and total >= 0, "invalid npm dependency count")
    counts: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    roots = {}
    links = {}
    for package, record in records.items():
        package_name(package, "npm")
        require(isinstance(record, dict) and set(record) == {
            "name", "severity", "isDirect", "via", "effects", "range", "nodes", "fixAvailable"
        }, "malformed npm vulnerability")
        require(record["name"] == package and record["severity"] in SEVERITIES, "invalid npm package/severity")
        require(type(record["isDirect"]) is bool and text(record["range"]), "invalid npm vulnerability details")
        require(isinstance(record["nodes"], list) and record["nodes"] and all(map(text, record["nodes"])),
                "missing npm affected nodes")
        require(isinstance(record["effects"], list) and all(map(text, record["effects"])), "invalid npm effects")
        require(isinstance(record["fixAvailable"], (bool, dict)), "invalid npm fix availability")
        require(isinstance(record["via"], list) and record["via"], "missing npm advisory causes")
        counts[record["severity"]] += 1
        roots[package] = set()
        links[package] = []
        for via in record["via"]:
            if isinstance(via, str):
                require(via in records, "unknown npm dependency cause")
                links[package].append(via)
                continue
            require(isinstance(via, dict), "unknown npm advisory cause")
            require(via.get("name") == package and via.get("dependency") == package, "npm advisory package mismatch")
            require(via.get("severity") in SEVERITIES, "unknown npm advisory severity")
            source = via.get("source")
            require(type(source) is int and source > 0, "missing npm advisory source")
            url = via.get("url")
            require(isinstance(url, str), "missing npm advisory URL")
            match = re.fullmatch(rf"https://github\.com/advisories/({GHSA})", url)
            require(match is not None or url == f"https://www.npmjs.com/advisories/{source}",
                    "unknown npm advisory URL")
            identifiers = (f"npm:{source}",) + ((match[1],) if match else ())
            roots[package].add((package, identifiers, via["severity"]))
    counts["total"] = len(records)
    observed = metadata.get("vulnerabilities")
    require(isinstance(observed, dict) and set(observed) == set(counts) and all(
        type(observed[key]) is int and observed[key] == value for key, value in counts.items()
    ), "npm vulnerability counts disagree")

    # npm v10 emits advisory objects for roots and package-name strings for
    # propagated vulnerabilities. Resolve the whole graph, including cycles,
    # before accepting an ignore for the original advisory's package only.
    changed = True
    while changed:
        changed = False
        for package, dependencies in links.items():
            before = len(roots[package])
            for dependency in dependencies:
                roots[package].update(roots[dependency])
            changed |= before != len(roots[package])
    for package, causes in roots.items():
        require(bool(causes), "npm dependency causes have no advisory root")
        highest = max(SEVERITIES.index(cause[2]) for cause in causes)
        require(SEVERITIES.index(records[package]["severity"]) == highest, "npm propagated severity disagrees")
    return sorted({(package, frozenset(identifiers)) for causes in roots.values()
                   for package, identifiers, severity in causes if severity in ("high", "critical")})


def run_audits(root, ignores, *, runner=None):
    runner = runner or subprocess.run
    common = [sys.executable, "-m", "pip_audit", "--format", "json", "--aliases", "on",
              "--desc", "off", "--progress-spinner", "off", "--strict"]
    audits = [
        ("requirements.lock", "python", common + ["-r", "requirements.lock", "--no-deps", "--disable-pip"], python_findings),
        ("requirements-dev.txt", "python", common + ["-r", "requirements-dev.txt"], python_findings),
        ("npm", "npm", ["npm", "audit", "--package-lock-only", "--audit-level=high", "--json"], npm_findings),
    ]
    status = 0
    for label, ecosystem, command, parse in audits:
        try:
            result = runner(command, cwd=root, capture_output=True, text=True, check=False, timeout=300)
            require(result.returncode in (0, 1), "scanner failed")
            findings = parse(parse_json(result.stdout))
            require(result.returncode == int(bool(findings)), "scanner exit disagrees with report")
            remaining = [(package, ids) for package, ids in findings if not any(
                ignore["ecosystem"] == ecosystem and ignore["package"] == package and ignore["advisory"] in ids
                for ignore in ignores
            )]
            print(f"{label}: {len(remaining)} unaccepted findings, {len(findings) - len(remaining)} accepted")
            for package, identifiers in remaining:
                print(f"  {package}: {', '.join(sorted(identifiers))}")
            if remaining:
                status = max(status, 1)
        except (AuditError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
            # Raw scanner stderr may include credentials or private index URLs.
            message = str(exc) if isinstance(exc, AuditError) else "scanner execution failed"
            print(f"{label}: incomplete audit ({message})")
            status = 2
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true", help="validate ignores without running scanners")
    args = parser.parse_args(argv)
    try:
        ignores = load_policy(ROOT / ".github/dependency-audit-ignore.json")
    except (AuditError, OSError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, AuditError) else "cannot read ignore policy"
        print(f"Dependency audit policy rejected: {message}", file=sys.stderr)
        return 2
    if args.validate_only:
        return 0
    return run_audits(ROOT, ignores)


if __name__ == "__main__":
    raise SystemExit(main())
