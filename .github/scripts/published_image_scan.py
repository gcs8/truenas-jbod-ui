#!/usr/bin/env python3
"""Scan published images privately, then reconcile only this workflow's issues."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "gcs8/truenas-jbod-ui"
REPOSITORY_ID = 1208650488
ISSUE_COLLECTION = f"/repositories/{REPOSITORY_ID}/issues"
IMAGE = "ghcr.io/" + REPOSITORY
TRIVY_VERSION = "0.76.2"
ARCHIVE_SHA256 = "bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf"
MANIFEST_SHA256 = "802e4ac1418ed967cd3c4b901f4f71ca00b7363ede67778665a22d9f13f58c83"
RELEASE = "https://github.com/aquasecurity/trivy/releases/download/v" + TRIVY_VERSION + "/"
STABLE = r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
DIGEST = r"sha256:[0-9a-f]{64}"
MARKER = r"<!-- published-image-scan:v1:([0-9a-f]{64}) -->"
ADVISORY = r"(?:CVE-\d{4}-\d{4,}|GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4})"
PACKAGE = r"(?:@[a-zA-Z0-9._-]+/)?[a-zA-Z0-9][a-zA-Z0-9._+/@-]{0,199}"
VERSION = r"[a-zA-Z0-9][a-zA-Z0-9._+~:!, <>*=-]{0,299}"


class ScanError(ValueError):
    """A fixed, value-safe failure, never a clean result."""


class PrivateTriage(ScanError):
    """An unignored secret match blocks every public mutation."""


def require(condition, message="invalid scan data"):
    if not condition:
        raise ScanError(message)


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate field")
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=object_pairs)
    except (ValueError, TypeError, RecursionError):
        raise ScanError("invalid JSON") from None


class PolicyLoader(yaml.BaseLoader):
    """Keep dates as strings and reject duplicate keys, aliases and merges."""

    def compose_node(self, parent, index):
        require(not self.check_event(yaml.AliasEvent), "policy aliases forbidden")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        return object_pairs((self.construct_object(k, deep=deep), self.construct_object(v, deep=deep))
                            for k, v in node.value)


def validate_policy(raw, *, today=None):
    try:
        policy = yaml.load(raw, Loader=PolicyLoader)
    except (yaml.YAMLError, TypeError, RecursionError):
        raise ScanError("invalid ignore policy") from None
    require(isinstance(policy, dict) and set(policy) == {"vulnerabilities", "secrets"}, "invalid policy fields")
    require(policy["secrets"] == [], "secret exceptions are not accepted")
    require(isinstance(policy["vulnerabilities"], list), "invalid policy vulnerabilities")
    today = today or datetime.now(timezone.utc).date()
    seen = set()
    for entry in policy["vulnerabilities"]:
        require(isinstance(entry, dict) and set(entry) == {"id", "statement", "expired_at"}, "invalid ignore fields")
        require(matches(ADVISORY, entry["id"]), "invalid ignore identifier")
        require(isinstance(entry["statement"], str) and entry["statement"].strip(), "blank ignore statement")
        expiry = entry["expired_at"]
        require(matches(r"\d{4}-\d{2}-\d{2}", expiry), "noncanonical ignore expiry")
        try:
            expires = date.fromisoformat(expiry)
        except ValueError:
            raise ScanError("invalid ignore expiry") from None
        require(expires > today, "expired ignore policy")
        require(entry["id"] not in seen, "duplicate ignore")
        seen.add(entry["id"])
    return policy


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(not req.has_header("Authorization") and req.method == "GET", "authenticated redirect refused")
        require(urlsplit(newurl).scheme == "https", "unsafe download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http(method, url, headers=None, payload=None, limit=32 * 1024 * 1024):
    # Deliberately no retries, especially POST after an ambiguous network result.
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                     headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(SafeRedirect).open(request, timeout=120) as response:
            raw = response.read(limit + 1)
            require(len(raw) <= limit, "response too large")
            return raw, dict(response.headers)
    except Exception:
        raise ScanError("HTTP operation incomplete") from None


def pages(url, headers, prefix, *, link_alias=None, query_validator=None):
    seen = set()
    origin = urlsplit(url)[:2]
    while url:
        parsed = urlsplit(url)
        require(parsed[:2] == origin and parsed.path in (prefix, link_alias) and not parsed.fragment,
                "unsafe pagination")
        if query_validator is not None:
            query_validator(parsed, len(seen) + 1)
        # A numeric Link identifies this collection; it never supplies the request destination.
        url = parsed._replace(path=prefix).geturl()
        require(url not in seen and len(seen) < 10000, "invalid pagination")
        seen.add(url)
        raw, response_headers = http("GET", url, headers)
        yield parse_json(raw)
        link = next((value for key, value in response_headers.items() if key.lower() == "link"), "")
        next_links = re.findall(r'<([^>]+)>;\s*rel="next"', link)
        require(len(next_links) <= 1 and (not link or next_links or 'rel="last"' in link or 'rel="prev"' in link or 'rel="first"' in link), "malformed pagination")
        url = urljoin(url, next_links[0]) if next_links else None


def validate_issue_page(parsed, page_number):
    require(len(parsed.query) <= 4096, "unsafe issue pagination")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        raise ScanError("unsafe issue pagination") from None
    query = {}
    for key, value in pairs:
        require(key not in query, "unsafe issue pagination")
        query[key] = value
    require(set(query) <= {"state", "per_page", "after", "page"} and
            query.get("state") == "all" and query.get("per_page") == "100", "unsafe issue pagination")
    if page_number == 1:
        require(set(query) == {"state", "per_page"}, "unsafe issue pagination")
        return
    require("after" in query or "page" in query, "unsafe issue pagination")
    if "after" in query:
        cursor = query["after"]
        require(0 < len(cursor) <= 2048 and all(0x21 <= ord(char) <= 0x7e for char in cursor),
                "unsafe issue pagination")
    if "page" in query:
        require(matches(r"[1-9]\d{0,4}", query["page"]) and int(query["page"]) == page_number,
                "unsafe issue pagination")


class Registry:
    def __init__(self, *, base="https://ghcr.io", token=None):
        self.base = base
        if token is None:
            raw, _ = http("GET", base + "/token?service=ghcr.io&scope=repository:" + REPOSITORY + ":pull")
            auth = parse_json(raw)
            require(isinstance(auth, dict) and isinstance(auth.get("token"), str) and auth["token"], "registry authentication failed")
            token = auth["token"]
        self.headers = {"Authorization": "Bearer " + token}

    def targets(self):
        path = "/v2/" + REPOSITORY + "/tags/list"
        tags = set()
        for page in pages(self.base + path + "?n=100", self.headers, path):
            require(isinstance(page, dict) and page.get("name") == REPOSITORY and isinstance(page.get("tags"), list), "invalid tag page")
            require(all(isinstance(tag, str) and matches(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) for tag in page["tags"]), "invalid tags")
            tags.update(page["tags"])
        require({"latest", "dev"} <= tags, "missing required image tag")
        stable = [tag for tag in tags if matches(STABLE, tag)]
        require(stable, "no published stable image")
        newest = max(stable, key=lambda tag: tuple(map(int, tag[1:].split("."))))
        return ["latest", "dev", newest]

    def resolve(self, tag):
        require(tag in ("latest", "dev") or matches(STABLE, tag), "invalid target tag")
        headers = {**self.headers, "Accept": ", ".join(("application/vnd.oci.image.index.v1+json",
                   "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                   "application/vnd.docker.distribution.manifest.v2+json"))}
        raw, metadata = http("GET", self.base + "/v2/" + REPOSITORY + "/manifests/" + tag, headers)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        header_digest = next((value for key, value in metadata.items() if key.lower() == "docker-content-digest"), None)
        require(digest == header_digest, "registry digest mismatch")
        manifest = parse_json(raw)
        require(isinstance(manifest, dict) and manifest.get("schemaVersion") == 2 and
                (isinstance(manifest.get("manifests"), list) or isinstance(manifest.get("layers"), list)), "invalid image manifest")
        return digest


def scan_argv(tool, reference, policy, output, cache):
    return [str(tool), "image", "--quiet", "--cache-dir", str(cache), "--scanners", "vuln,secret",
            "--severity", "HIGH,CRITICAL", "--ignore-unfixed", "--ignorefile", str(policy),
            "--format", "json", "--output", str(output), "--exit-code", "0", "--image-src", "remote",
            "--platform", "linux/amd64", "--timeout", "15m", reference]


def scanner_env(directory):
    # No GitHub/registry credentials or inherited Trivy suppression settings.
    return {"PATH": os.defpath, "HOME": str(directory), "TMPDIR": str(directory), "LANG": "C.UTF-8"}


def acquire(directory):
    name = f"trivy_{TRIVY_VERSION}_Linux-64bit.tar.gz"
    manifest, _ = http("GET", RELEASE + f"trivy_{TRIVY_VERSION}_checksums.txt")
    require(hashlib.sha256(manifest).hexdigest() == MANIFEST_SHA256, "checksum manifest mismatch")
    require(f"{ARCHIVE_SHA256}  {name}" in manifest.decode().splitlines(), "archive absent from manifest")
    archive, _ = http("GET", RELEASE + name, limit=80 * 1024 * 1024)
    require(hashlib.sha256(archive).hexdigest() == ARCHIVE_SHA256, "scanner archive mismatch")
    # Extract only the verified executable, never archive-supplied paths.
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = [member for member in tar.getmembers() if member.name == "trivy"]
        require(len(members) == 1 and members[0].isfile() and members[0].size < 300 * 1024 * 1024, "invalid scanner member")
        stream = tar.extractfile(members[0])
        require(stream is not None, "missing scanner executable")
        tool = directory / "trivy"
        with tool.open("xb") as output:
            output.write(stream.read())
        tool.chmod(0o700)
    version = subprocess.run([str(tool), "--version", "--format", "json"], cwd=directory,
                             env=scanner_env(directory), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=60, check=False)
    require(version.returncode == 0 and parse_json(version.stdout).get("Version") == TRIVY_VERSION, "scanner version mismatch")
    return tool


def scan_image(tool, digest, policy, directory):
    output = directory / "report.json"
    try:
        # Precreate restrictive native JSON, including on a permissive caller umask.
        with output.open("xb"):
            output.chmod(0o600)
        result = subprocess.run(scan_argv(tool, IMAGE + "@" + digest, policy, output, directory / "cache"),
                                cwd=directory, env=scanner_env(directory), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=1000, check=False)
        require(result.returncode == 0, f"native scanner incomplete: exit={result.returncode}")
        require(0 < output.stat().st_size <= 64 * 1024 * 1024, "invalid native report size")
        return parse_json(output.read_bytes())
    finally:
        output.unlink(missing_ok=True)


def validate_targets(targets):
    require(isinstance(targets, dict) and len(targets) == 3 and {"latest", "dev"} <= targets.keys(), "incomplete target coverage")
    stable = set(targets) - {"latest", "dev"}
    require(len(stable) == 1 and matches(STABLE, next(iter(stable))), "invalid stable target")
    require(all(matches(DIGEST, digest) for digest in targets.values()), "invalid target digest")


def identity(advisory, ecosystem, package):
    require(matches(ADVISORY, advisory) and matches(r"[a-z][a-z0-9-]{0,39}", ecosystem) and matches(PACKAGE, package), "invalid public finding identity")
    return {"advisory": advisory, "ecosystem": ecosystem, "package": package}


def fingerprint(finding):
    key = identity(finding["advisory"], finding["ecosystem"], finding["package"])
    return hashlib.sha256(json.dumps({"version": 1, **key}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def database_metadata(metadata):
    require(isinstance(metadata, dict) and type(metadata.get("Version")) is int and metadata["Version"] == 2, "invalid database version")
    updated = metadata.get("UpdatedAt")
    require(matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", updated), "invalid database date")
    try:
        datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        raise ScanError("invalid database date") from None
    return {"Version": 2, "UpdatedAt": updated}


def native_findings(digest, report):
    require(isinstance(report, dict) and report.get("SchemaVersion") == 2, "unknown native schema")
    require(set(report) <= {"SchemaVersion", "Trivy", "ReportID", "CreatedAt", "ArtifactID", "ArtifactName", "ArtifactType", "Metadata", "Results"}, "unknown report fields")
    require(isinstance(report.get("Trivy"), dict) and report["Trivy"].get("Version") == TRIVY_VERSION, "unknown native version")
    require(report.get("ArtifactName") == IMAGE + "@" + digest and report.get("ArtifactType") == "container_image", "native target mismatch")
    metadata = report.get("Metadata")
    require(isinstance(metadata, dict) and isinstance(metadata.get("RepoDigests"), list) and
            IMAGE + "@" + digest in metadata["RepoDigests"] and matches(DIGEST, metadata.get("ImageID")) and
            isinstance(metadata.get("OS"), dict) and metadata["OS"].get("Family") and metadata["OS"].get("Name"), "missing image metadata")
    results = report.get("Results")
    require(isinstance(results, list) and results and any(isinstance(r, dict) and r.get("Class") == "os-pkgs" for r in results), "missing package results")
    findings = []
    for result in results:
        require(isinstance(result, dict) and set(result) <= {"Target", "Class", "Type", "Packages", "Vulnerabilities", "Secrets"}, "unknown result fields")
        require(isinstance(result.get("Target"), str) and result["Target"].strip(), "missing native target")
        kind = result.get("Class")
        require(kind in ("os-pkgs", "lang-pkgs", "secret"), "unsupported result class")
        if kind != "secret":
            require(matches(r"[a-z][a-z0-9-]{0,39}", result.get("Type")), "missing package ecosystem")
        if kind == "os-pkgs":
            require(result["Type"] == metadata["OS"]["Family"], "package ecosystem mismatch")
        secrets = result.get("Secrets", [])
        require(isinstance(secrets, list), "invalid secret results")
        if secrets:
            raise PrivateTriage(f"private triage required: secret_matches={len(secrets)}")
        require(kind != "secret", "empty secret result")
        vulns = result.get("Vulnerabilities", [])
        require(isinstance(vulns, list), "invalid vulnerability results")
        for vuln in vulns:
            require(isinstance(vuln, dict), "invalid vulnerability")
            key = identity(vuln.get("VulnerabilityID"), result.get("Type"), vuln.get("PkgName"))
            require(matches(VERSION, vuln.get("InstalledVersion")) and matches(VERSION, vuln.get("FixedVersion")) and
                    vuln.get("Severity") in ("HIGH", "CRITICAL"), "invalid public vulnerability fields")
            findings.append({**key, "installed": vuln["InstalledVersion"], "fixed": vuln["FixedVersion"], "severity": vuln["Severity"]})
    return findings


def make_bundle(targets, reports, metadata):
    validate_targets(targets)
    require(isinstance(reports, dict) and set(reports) == set(targets.values()), "missing target report")
    findings = {}
    for digest, report in sorted(reports.items()):
        for record in native_findings(digest, report):
            key = identity(record["advisory"], record["ecosystem"], record["package"])
            finding = findings.setdefault(fingerprint(key), {**key, "occurrences": []})
            for tag in sorted(tag for tag, value in targets.items() if value == digest):
                occurrence = {"tag": tag, **{key: record[key] for key in ("installed", "fixed", "severity")}}
                if occurrence not in finding["occurrences"]:
                    finding["occurrences"].append(occurrence)
    for finding in findings.values():
        finding["occurrences"].sort(key=lambda row: tuple(row[key] for key in ("tag", "installed", "fixed", "severity")))
    return {"version": 1, "scanner": TRIVY_VERSION, "targets": dict(targets), "database": database_metadata(metadata), "findings": findings}


def validate_bundle(bundle):
    require(isinstance(bundle, dict) and set(bundle) == {"version", "scanner", "targets", "database", "findings"} and
            bundle["version"] == 1 and bundle["scanner"] == TRIVY_VERSION, "invalid scan bundle")
    validate_targets(bundle["targets"])
    database_metadata(bundle["database"])
    require(isinstance(bundle["findings"], dict), "invalid bundle findings")
    for fid, finding in bundle["findings"].items():
        require(isinstance(finding, dict) and set(finding) == {"advisory", "ecosystem", "package", "occurrences"} and fingerprint(finding) == fid, "invalid finding fingerprint")
        rows = finding["occurrences"]
        require(isinstance(rows, list) and rows, "missing occurrences")
        for row in rows:
            require(isinstance(row, dict) and set(row) == {"tag", "installed", "fixed", "severity"}, "invalid occurrence")
            require(row["tag"] in bundle["targets"] and matches(VERSION, row["installed"]) and
                    matches(VERSION, row["fixed"]) and row["severity"] in ("HIGH", "CRITICAL"), "invalid public occurrence")


def issue_content(fid, finding):
    advisory = finding["advisory"]
    link = "https://nvd.nist.gov/vuln/detail/" + advisory if advisory.startswith("CVE-") else "https://github.com/advisories/" + advisory
    title = f"Image scanner finding: {advisory} in {finding['package']} ({finding['ecosystem']})"
    lines = [f"<!-- published-image-scan:v1:{fid} -->", "", "## Published image scanner finding", "",
             "This is a package advisory match, not confirmation of exploitable application behavior.", "",
             f"Advisory: [{advisory}]({link})", f"Package: `{finding['package']}`", f"Ecosystem: `{finding['ecosystem']}`", "",
             "| Tag alias | Installed | Fixed | Severity |", "| --- | --- | --- | --- |"]
    for row in sorted(finding["occurrences"], key=lambda row: tuple(row[key] for key in ("tag", "installed", "fixed", "severity"))):
        lines.append(f"| `{row['tag']}` | `{row['installed']}` | `{row['fixed']}` | {row['severity']} |")
    lines += ["", "Managed by the published image scan. Closure requires a complete successful scan of all configured targets.", ""]
    return title, "\n".join(lines)


class GitHub:
    def __init__(self, token, *, base="https://api.github.com"):
        require(isinstance(token, str) and token, "missing GitHub token")
        self.base = base
        self.path = "/repos/" + REPOSITORY + "/issues"
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}

    def issues(self):
        issues = []
        for page in pages(self.base + self.path + "?state=all&per_page=100", self.headers, self.path,
                          link_alias=ISSUE_COLLECTION, query_validator=validate_issue_page):
            require(isinstance(page, list), "invalid issue page")
            issues.extend(page)
        return issues

    def request(self, method, suffix="", payload=None):
        raw, _ = http(method, self.base + self.path + suffix, self.headers, payload)
        return parse_json(raw)


def managed(issues):
    found = {}
    numbers = set()
    for issue in issues:
        require(isinstance(issue, dict), "invalid issue data")
        if "pull_request" in issue or issue.get("user", {}).get("login") != "github-actions[bot]":
            continue
        body = issue.get("body") or ""
        require(isinstance(body, str), "invalid issue body")
        marker = re.match(MARKER + r"\n", body)
        if not marker:
            continue
        require(len(re.findall(MARKER, body)) == 1, "ambiguous managed marker")
        fid = marker[1]
        number = issue.get("number")
        require(type(number) is int and number > 0 and number not in numbers and fid not in found and
                issue.get("state") in ("open", "closed") and isinstance(issue.get("title"), str), "ambiguous managed issue")
        numbers.add(number)
        found[fid] = issue
    return found


def reconcile(api, bundle):
    validate_bundle(bundle)
    existing = managed(api.issues())
    plan = []
    for fid, finding in sorted(bundle["findings"].items()):
        title, body = issue_content(fid, finding)
        require(len(title) <= 256 and len(body) <= 60000, "issue content too large")
        old = existing.get(fid)
        if old is None:
            plan.append(("POST", "", {"title": title, "body": body, "labels": [], "assignees": [], "milestone": None}, {"title": title, "body": body, "state": "open"}))
        elif old["title"] != title or old["body"] != body or old["state"] != "open":
            payload = {"title": title, "body": body, "state": "open"}
            if old["state"] == "closed":
                payload["state_reason"] = "reopened"
            plan.append(("PATCH", "/" + str(old["number"]), payload, {"title": title, "body": body, "state": "open"}))
    for fid, old in sorted(existing.items()):
        if fid not in bundle["findings"] and old["state"] == "open":
            plan.append(("PATCH", "/" + str(old["number"]), {"state": "closed", "state_reason": "completed"}, {"state": "closed"}))
    # All scan data, API pages, identities and payloads have passed before any write.
    for method, suffix, payload, expected in plan:
        result = api.request(method, suffix, payload)
        require(isinstance(result, dict) and type(result.get("number")) is int, "invalid mutation response")
        number = result["number"]
        require(number > 0 and (not suffix or suffix == "/" + str(number)), "mutation target mismatch")
        verified = api.request("GET", "/" + str(number))
        require(isinstance(verified, dict) and verified.get("number") == number and
                all(verified.get(key) == value for key, value in expected.items()), "issue readback mismatch")
    return len(plan)


def scan(output):
    raw_policy = (ROOT / ".trivyignore.yaml").read_text(encoding="utf-8")
    validate_policy(raw_policy)  # Must precede acquisition, auth and tag listing.
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="published-image-scan-") as temporary:
        directory = Path(temporary)
        require(not directory.is_relative_to(ROOT), "scanner scratch must be outside source")
        policy = directory / "ignore.yaml"
        policy.write_text(raw_policy, encoding="utf-8")
        policy.chmod(0o600)
        tool = acquire(directory)
        registry = Registry()
        targets = {tag: registry.resolve(tag) for tag in registry.targets()}
        reports = {}
        for digest in sorted(set(targets.values())):
            reports[digest] = scan_image(tool, digest, policy, directory)
            # Stop immediately on an unignored secret or invalid native schema.
            native_findings(digest, reports[digest])
        metadata = parse_json((directory / "cache/db/metadata.json").read_bytes())
        bundle = make_bundle(targets, reports, metadata)
    # No native raw report/cache survives this point. Only allowlisted fields remain.
    validate_bundle(bundle)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        output.chmod(0o600)
        json.dump(bundle, stream, sort_keys=True)
    summary = ["## Published image scan", f"Trivy: {TRIVY_VERSION}",
               f"Vulnerability database version: {bundle['database']['Version']}",
               f"Vulnerability database updated: {bundle['database']['UpdatedAt']}",
               "Platform: linux/amd64", "", "| Tag | Resolved immutable digest |", "| --- | --- |"]
    summary += [f"| {tag} | `{digest}` |" for tag, digest in sorted(targets.items())]
    summary += ["", f"Package advisory scanner findings: {len(bundle['findings'])}", "Secret matches: 0", ""]
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(summary))
    print(f"scan complete: targets={len(targets)} findings={len(bundle['findings'])} secret_matches=0")


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ScanError("invalid arguments")


def main(argv=None):
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("command", choices=("validate", "scan", "sync"))
        parser.add_argument("--bundle", type=Path)
        args = parser.parse_args(argv)
        if args.command == "validate":
            validate_policy((ROOT / ".trivyignore.yaml").read_text(encoding="utf-8"))
        else:
            require(args.bundle is not None, "bundle path required")
            require(not args.bundle.resolve().is_relative_to(ROOT), "bundle must be outside source")
            if args.command == "scan":
                scan(args.bundle)
            else:
                # Recheck policy and complete bundle before even reading GitHub state.
                validate_policy((ROOT / ".trivyignore.yaml").read_text(encoding="utf-8"))
                try:
                    bundle = parse_json(args.bundle.read_bytes())
                    writes = reconcile(GitHub(os.environ.get("GITHUB_TOKEN")), bundle)
                    print(f"issue reconciliation complete: writes={writes}")
                finally:
                    args.bundle.unlink(missing_ok=True)
        return 0
    except ScanError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Never emit scanner/API errors, paths, URLs, candidate bytes or tracebacks.
        print("published image scan incomplete; no clean result", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
