#!/usr/bin/env python3
"""Rebuild public stable source locally, qualify and compare; never publish an image."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import urljoin, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "gcs8/truenas-jbod-ui"
IMAGE = "ghcr.io/" + REPOSITORY
LOCAL_IMAGE = "stable-rebuild-scan:local"
SMOKE_IMAGE = "127.0.0.1:5000/stable-rebuild-scan:local"
REGISTRY_IMAGE = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
TRIVY_VERSION = "0.76.2"
ARCHIVE_SHA256 = "bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf"
MANIFEST_SHA256 = "802e4ac1418ed967cd3c4b901f4f71ca00b7363ede67778665a22d9f13f58c83"
RELEASE = f"https://github.com/aquasecurity/trivy/releases/download/v{TRIVY_VERSION}/"
STABLE = r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
DIGEST = r"sha256:[0-9a-f]{64}"
BASE = r"python:3\.(?:0|[1-9]\d*)-slim(?:-(?:bullseye|bookworm|trixie))?"
ADVISORY = r"(?:CVE-\d{4}-\d{4,}|GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4})"
MARKER = "<!-- stable-rebuild-scan:v1:" + hashlib.sha256((REPOSITORY + ":current-stable").encode()).hexdigest() + " -->"
TITLE = "Stable image rebuild comparison (scan-only)"


class ScanError(ValueError):
    """Incomplete or unsafe evidence. Exception text must never contain native data."""


def require(condition):
    if not condition:
        raise ScanError("stable rebuild qualification incomplete")


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=object_pairs)
    except (ValueError, TypeError, RecursionError):
        raise ScanError("invalid structured evidence") from None


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(req.method == "GET" and not req.has_header("Authorization"))
        require(urlsplit(newurl).scheme == "https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http(method, url, headers=None, payload=None, limit=32 * 1024 * 1024):
    # No automatic retry, including a POST whose result may be ambiguous.
    request = urllib.request.Request(url, headers=headers or {}, method=method,
                                     data=None if payload is None else json.dumps(payload).encode())
    try:
        with urllib.request.build_opener(Redirect).open(request, timeout=120) as response:
            raw = response.read(limit + 1)
            require(len(raw) <= limit)
            return raw, {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        raise ScanError("HTTP operation incomplete") from None


def pages(url, headers):
    origin, path = urlsplit(url)[:2], urlsplit(url).path
    seen = set()
    while url:
        require(url not in seen and len(seen) < 10000)
        require(urlsplit(url)[:2] == origin and urlsplit(url).path == path)
        seen.add(url)
        raw, metadata = http("GET", url, headers)
        yield parse_json(raw)
        link = metadata.get("link", "")
        links = re.findall(r'<([^>]+)>;\s*rel="(next|prev|last|first)"', link)
        require(not link or ",".join(f'<{u}>; rel="{r}"' for u, r in links).replace(" ", "") == link.replace(" ", ""))
        next_links = [u for u, rel in links if rel == "next"]
        require(len(next_links) <= 1)
        url = urljoin(url, next_links[0]) if next_links else None


class Registry:
    def __init__(self, host, repository, *, token=None):
        require((host, repository) in (("ghcr.io", REPOSITORY), ("registry-1.docker.io", "library/python")))
        self.base, self.repository = "https://" + host, repository
        if token is None:
            auth = (f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repository}:pull" if host == "ghcr.io" else
                    f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull")
            raw, _ = http("GET", auth)
            data = parse_json(raw)
            require(isinstance(data, dict) and isinstance(data.get("token"), str) and data["token"])
            token = data["token"]
        self.headers = {"Authorization": "Bearer " + token}

    def stable(self):
        require(self.repository == REPOSITORY)
        tags = set()
        for page in pages(self.base + "/v2/" + self.repository + "/tags/list?n=100", self.headers):
            require(isinstance(page, dict) and page.get("name") == self.repository and isinstance(page.get("tags"), list))
            require(all(matches(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) for tag in page["tags"]))
            tags.update(page["tags"])
        stable = [tag for tag in tags if matches(STABLE, tag)]
        require(stable)
        return max(stable, key=lambda tag: tuple(map(int, tag[1:].split("."))))

    def resolve(self, tag):
        require(matches(STABLE, tag) if self.repository == REPOSITORY else matches(BASE, "python:" + tag))
        headers = {**self.headers, "Accept": ", ".join(("application/vnd.oci.image.index.v1+json",
                   "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.manifest.v1+json",
                   "application/vnd.docker.distribution.manifest.v2+json"))}

        def manifest(ref):
            raw, metadata = http("GET", self.base + "/v2/" + self.repository + "/manifests/" + ref, headers)
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            declared = next((v for k, v in metadata.items() if k.lower() == "docker-content-digest"), None)
            require(declared == digest and (not ref.startswith("sha256:") or digest == ref))
            data = parse_json(raw)
            require(isinstance(data, dict) and data.get("schemaVersion") == 2)
            return digest, data

        digest, data = manifest(tag)
        if "manifests" in data:
            require(isinstance(data["manifests"], list))
            candidates = [m for m in data["manifests"] if isinstance(m, dict) and
                          m.get("platform", {}).get("os") == "linux" and
                          m.get("platform", {}).get("architecture") == "amd64" and
                          m.get("platform", {}).get("variant", "") in ("", "v1")]
            require(len(candidates) == 1 and matches(DIGEST, candidates[0].get("digest")))
            digest, data = manifest(candidates[0]["digest"])
        require(isinstance(data.get("layers"), list) and isinstance(data.get("config"), dict))
        return digest


def base_match(source):
    require(isinstance(source, bytes))
    lines = list(re.finditer(rb"(?im)^[ \t]*FROM\b[^\r\n]*", source))
    require(len(lines) == 1)
    match = re.fullmatch(rb"[ \t]*FROM[ \t]+(" + BASE.encode() + rb")(@sha256:[0-9a-f]{64})?[ \t]*", lines[0][0])
    require(match is not None)
    return lines[0], match


def base_target(source):
    return base_match(source)[1][1].decode()


def refresh_from(source, target, digest):
    line, match = base_match(source)
    require(target == match[1].decode() and matches(DIGEST, digest))
    start = line.start() + match.start(1)
    end = line.start() + (match.end(2) if match[2] else match.end(1))
    return source[:start] + (target + "@" + digest).encode() + source[end:]


def build_argv(context, commit):
    require(matches(r"[0-9a-f]{40}", commit))
    return ["docker", "buildx", "build", "--pull", "--no-cache", "--platform", "linux/amd64", "--load",
            "--build-arg", "SOURCE_COMMIT=" + commit, "--tag", LOCAL_IMAGE, str(context)]


def loopback_push_argv(image):
    require(image == SMOKE_IMAGE)
    return ["docker", "push", image]


def run(argv, *, cwd, env=None, timeout=1800):
    # Native build/smoke/scanner outputs may contain secrets. Never echo them.
    result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    require(result.returncode == 0)
    return result.stdout


def native_rows(report, image, kind):
    require(isinstance(report, dict) and report.get("SchemaVersion") == 2)
    require(report.get("Trivy", {}).get("Version") == TRIVY_VERSION)
    require(report.get("ArtifactType") == "container_image" and report.get("Metadata", {}).get("ImageID") == image)
    results = report.get("Results")
    require(isinstance(results, list) and (kind == "secret" or results))
    rows = []
    for result in results:
        require(isinstance(result, dict))
        secrets = result.get("Secrets", [])
        require(isinstance(secrets, list) and not secrets)
        vulns = result.get("Vulnerabilities", [])
        require(isinstance(vulns, list))
        if kind == "secret":
            require(not vulns)
            continue
        ecosystem = result.get("Type")
        require((result.get("Class"), ecosystem) in (("os-pkgs", "debian"), ("lang-pkgs", "python-pkg")))
        for vuln in vulns:
            require(isinstance(vuln, dict) and matches(ADVISORY, vuln.get("VulnerabilityID")))
            package, severity = vuln.get("PkgName"), vuln.get("Severity")
            require(matches(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,199}", package) and severity in ("HIGH", "CRITICAL"))
            require(isinstance(vuln.get("InstalledVersion"), str) and vuln["InstalledVersion"] and
                    isinstance(vuln.get("FixedVersion"), str) and vuln["FixedVersion"])
            if ecosystem == "python-pkg":
                package = re.sub(r"[-_.]+", "-", package).lower()
            aliases = vuln.get("VendorIDs", [])
            require(isinstance(aliases, list) and all(isinstance(a, str) for a in aliases))
            ids = {vuln["VulnerabilityID"], *(a for a in aliases if matches(ADVISORY, a))}
            rows.append((ecosystem, package, ids, severity))
    return rows


def validate_database(db):
    require(isinstance(db, dict) and set(db) == {"Version", "UpdatedAt", "sha256"})
    require(db["Version"] == 2 and matches(r"[0-9a-f]{64}", db["sha256"]))
    require(matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", db["UpdatedAt"]))
    try:
        datetime.fromisoformat(db["UpdatedAt"].replace("Z", "+00:00"))
    except ValueError:
        raise ScanError("invalid database metadata") from None


def compare(pair):
    require(isinstance(pair, dict) and set(pair) == {"version", "tag", "commit", "published_digest", "base_ref",
            "base_digest", "rebuilt_id", "production_smoke", "upgrade_smoke", "published", "rebuilt"})
    require(pair["version"] == 1 and matches(STABLE, pair["tag"]) and matches(r"[0-9a-f]{40}", pair["commit"]))
    require(matches(BASE, pair["base_ref"]) and all(matches(DIGEST, pair[k]) for k in ("base_digest", "published_digest", "rebuilt_id")))
    require(pair["production_smoke"] is True and pair["upgrade_smoke"] is True)
    rows = {}
    for side in ("published", "rebuilt"):
        entry = pair[side]
        require(isinstance(entry, dict) and set(entry) == {"image_id", "database", "vuln", "secret"})
        require(matches(DIGEST, entry["image_id"]))
        validate_database(entry["database"])
        native_rows(entry["secret"], entry["image_id"], "secret")
        rows[side] = native_rows(entry["vuln"], entry["image_id"], "vuln")
    require(pair["rebuilt"]["image_id"] == pair["rebuilt_id"])
    require(pair["published"]["database"] == pair["rebuilt"]["database"])
    # Merge advisory aliases across both scans, within ecosystem/package only.
    groups = {}
    for side in rows:
        for eco, pkg, ids, severity in rows[side]:
            key = (eco, pkg)
            overlapping = [g for g in groups.get(key, []) if g & ids]
            merged = set(ids).union(*overlapping)
            groups[key] = [g for g in groups.get(key, []) if g not in overlapping] + [merged]
    findings = {}
    for side, records in rows.items():
        findings[side] = {}
        for eco, pkg, ids, severity in records:
            group = next(g for g in groups[(eco, pkg)] if g & ids)
            advisory = min(group, key=lambda a: (not a.startswith("CVE-"), a))
            key = (advisory, eco, pkg)
            old = findings[side].get(key)
            findings[side][key] = "CRITICAL" if "CRITICAL" in (severity, old) else "HIGH"
    published, rebuilt = map(set, (findings["published"], findings["rebuilt"]))
    sets = {"published": published, "rebuilt": rebuilt, "cleared": published - rebuilt,
            "remaining": published & rebuilt, "new": rebuilt - published}
    return {"counts": {k: len(v) for k, v in sets.items()}, "sets": sets,
            "severity": {side: {s: list(records.values()).count(s) for s in ("HIGH", "CRITICAL")}
                         for side, records in findings.items()}}


def issue_body(pair):
    result = compare(pair)
    lines = [MARKER, "## Scan-only stable image comparison", "",
             "No image was published. Publishing and tag policy remain an owner decision.", "",
             f"Stable tag: `{pair['tag']}`", f"Source commit: `{pair['commit']}`",
             f"Published linux/amd64 digest: `{pair['published_digest']}`",
             f"Refreshed base: `{pair['base_ref']}@{pair['base_digest']}`", "",
             f"Scanner: Trivy {TRIVY_VERSION}. Both images use the same database snapshot and linux/amd64 platform.",
             "Counts are unique advisory/ecosystem/package scan matches, not exploitability claims.", "",
             "| Image | Fixable High | Fixable Critical | Total |", "| --- | ---: | ---: | ---: |"]
    for side in ("published", "rebuilt"):
        severity = result["severity"][side]
        lines.append(f"| {side} | {severity['HIGH']} | {severity['CRITICAL']} | {result['counts'][side]} |")
    lines += ["", f"Cleared by rebuild: {result['counts']['cleared']}. Remaining: {result['counts']['remaining']}. "
              f"New in rebuild: {result['counts']['new']}.", "",
              "Production container smoke and image-only upgrade/rollback smoke passed for the rebuilt candidate."]
    for category in ("cleared", "new"):
        lines += ["", f"### {category.capitalize()} scan matches"]
        entries = sorted(result["sets"][category])
        lines += [f"- `{advisory}` / `{eco}` / `{pkg}`" for advisory, eco, pkg in entries] or ["None."]
    body = "\n".join(lines) + "\n"
    require(len(body.encode()) <= 60000)
    return body


def issue_record(value):
    require(isinstance(value, dict) and type(value.get("number")) is int and value["number"] > 0)
    require(value.get("state") in ("open", "closed") and isinstance(value.get("title"), str))
    require(value.get("body") is None or isinstance(value.get("body"), str))
    require(isinstance(value.get("user"), dict) and isinstance(value["user"].get("login"), str))
    return value


def managed(value):
    issue_record(value)
    require("pull_request" not in value and value["user"]["login"] == "github-actions[bot]" and
            (value.get("body") or "").splitlines().count(MARKER) == 1)
    return value


def sync_issue(pair, *, token, api="https://api.github.com"):
    body = issue_body(pair)  # Validate the complete pair before ANY API operation.
    require(isinstance(token, str) and token)
    require(api == "https://api.github.com" or matches(r"http://127\.0\.0\.1:[1-9]\d*", api))
    endpoint = api + "/repos/" + REPOSITORY + "/issues"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
    candidates, numbers = [], set()
    for page in pages(endpoint + "?state=all&per_page=100", headers):
        require(isinstance(page, list))
        for value in page:
            issue_record(value)
            require(value["number"] not in numbers)
            numbers.add(value["number"])
            if MARKER in (value.get("body") or ""):
                candidates.append(managed(value))
    require(len(candidates) <= 1)
    desired = {"title": TITLE, "body": body, "state": "open"}
    if candidates:
        previous = candidates[0]
        target = endpoint + "/" + str(previous["number"])
        raw, _ = http("GET", target, headers)
        current = managed(parse_json(raw))
        require(all(current.get(k) == previous.get(k) for k in ("number", "title", "body", "state", "user")))
        if all(current.get(k) == v for k, v in desired.items()):
            return "unchanged"
        raw, _ = http("PATCH", target, headers, desired)
        require(managed(parse_json(raw))["number"] == previous["number"])
    else:
        raw, _ = http("POST", endpoint, headers, desired)
        created = managed(parse_json(raw))
        target = endpoint + "/" + str(created["number"])
    raw, _ = http("GET", target, headers)
    actual = managed(parse_json(raw))
    require(target == endpoint + "/" + str(actual["number"]) and all(actual.get(k) == v for k, v in desired.items()))
    return "synchronized"


def scanner_env(directory):
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(directory), "TMPDIR": str(directory), "LANG": "C.UTF-8"}


def acquire(directory):
    name = f"trivy_{TRIVY_VERSION}_Linux-64bit.tar.gz"
    manifest, _ = http("GET", RELEASE + f"trivy_{TRIVY_VERSION}_checksums.txt")
    require(hashlib.sha256(manifest).hexdigest() == MANIFEST_SHA256)
    require(f"{ARCHIVE_SHA256}  {name}" in manifest.decode().splitlines())
    archive, _ = http("GET", RELEASE + name, limit=80 * 1024 * 1024)
    require(hashlib.sha256(archive).hexdigest() == ARCHIVE_SHA256)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name == "trivy"]
        require(len(members) == 1 and members[0].isfile() and members[0].size < 300 * 1024 * 1024)
        stream = tar.extractfile(members[0])
        require(stream is not None)
        tool = directory / "trivy"
        tool.write_bytes(stream.read())
        tool.chmod(0o700)
    version = parse_json(run([str(tool), "--version", "--format", "json"], cwd=directory, env=scanner_env(directory)))
    require(version.get("Version") == TRIVY_VERSION)
    return tool


def scan_argv(tool, archive, output, private, kind):
    require(kind in ("vuln", "secret"))
    args = [str(tool), "image", "--quiet", "--config", str(private / "trivy.yaml"),
            "--cache-dir", str(private / "cache"), "--skip-db-update", "--skip-java-db-update", "--offline-scan",
            "--scanners", kind, "--severity", "HIGH,CRITICAL" if kind == "vuln" else "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL",
            "--ignorefile", str(private / "empty.ignore"), "--secret-config", str(private / "secret.yaml"),
            "--format", "json", "--output", str(output), "--exit-code", "0", "--platform", "linux/amd64",
            "--timeout", "15m", "--input", str(archive)]
    return args + (["--ignore-unfixed"] if kind == "vuln" else [])


def database(private):
    cache = private / "cache/db"
    info = parse_json((cache / "metadata.json").read_bytes())
    with (cache / "trivy.db").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    db = {"Version": info.get("Version"), "UpdatedAt": info.get("UpdatedAt"), "sha256": digest}
    validate_database(db)
    return db


def image_info(reference, private, *, digest=None, commit=None):
    info = parse_json(run(["docker", "image", "inspect", reference, "--format", "{{json .}}"], cwd=private))
    require(info.get("Os") == "linux" and info.get("Architecture") == "amd64" and matches(DIGEST, info.get("Id")))
    if digest:
        require(IMAGE + "@" + digest in info.get("RepoDigests", []))
    if commit:
        require(info.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision") == commit)
    return info["Id"]


def production_smoke(source, private):
    # Reuse the RELEASE'S complete production smoke, not today's incompatible
    # app contracts. Only substitute the already-built image and disable builds.
    workflow = yaml.safe_load((source / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["container-smoke"]["steps"]
    recipes = [s["run"] for s in steps if "run" in s]
    require(recipes and all(isinstance(r, str) and "${{" not in r for r in recipes))
    fixture = source / "tests/fixtures/ci-smoke.compose.yml"
    compose = yaml.safe_load(fixture.read_text())
    ui = compose["services"]["enclosure-ui"]
    require("build" in ui and "image" not in ui)
    del ui["build"]
    ui["image"] = LOCAL_IMAGE
    ui["ports"] = ["127.0.0.1:18080:8000"]
    fixture.write_text(yaml.safe_dump(compose, sort_keys=False))
    # Probe-only admin invocations do not need host Docker control. Never mount
    # the daemon into a container, including in the release's Compose recipe.
    compose_path = source / "docker-compose.yml"
    original = compose_path.read_bytes()
    compose = yaml.safe_load(original)
    for service in compose["services"].values():
        service["volumes"] = [v for v in service.get("volumes", []) if "docker.sock" not in str(v)]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))
    try:
        for recipe in recipes:
            require("up -d --build --wait" in recipe or "--build" not in recipe)
            recipe = recipe.replace("up -d --build --wait", "up -d --no-build --wait")
            env = {**scanner_env(private), "RUNNER_TEMP": str(private), "GITHUB_ENV": str(private / "smoke.env"),
                   "CI_SMOKE_IMAGE_ID": image_info(LOCAL_IMAGE, private), "JBOD_UI_IMAGE": LOCAL_IMAGE}
            run(["bash", "-e", "-o", "pipefail", "-c", recipe], cwd=source, env=env, timeout=900)
    finally:
        compose_path.write_bytes(original)


def upgrade_smoke(source, private, published, tag, commit):
    registry = "stable-rebuild-scan-registry"
    try:
        run(["docker", "run", "-d", "--name", registry, "-p", "127.0.0.1:5000:5000", REGISTRY_IMAGE], cwd=private)
        run(["docker", "tag", LOCAL_IMAGE, SMOKE_IMAGE], cwd=private)
        run(loopback_push_argv(SMOKE_IMAGE), cwd=private)
        run([sys.executable, str(ROOT / "scripts/run_image_upgrade_smoke.py"), "--scenario", "hardened",
             "--root", str(private / "upgrade"), "--compose-fixture", str(source / "docker-compose.yml"),
             "--nonroot-fixture", str(source / "docker-compose.nonroot.yml"),
             "--config-fixture", str(source / "tests/fixtures/ci-smoke-config.yaml"),
             "--previous-image", IMAGE + "@" + published, "--previous-version", tag[1:],
             "--candidate-image", SMOKE_IMAGE, "--candidate-version", tag[1:], "--candidate-revision", commit],
            cwd=private, env=scanner_env(private), timeout=1500)
    finally:
        run(["docker", "rm", "-f", registry], cwd=private)


def qualify():
    # Fresh context from a public Git tag, with no local files or useful HOME.
    with tempfile.TemporaryDirectory(prefix="stable-rebuild-") as raw:
        private = Path(raw)
        private.chmod(0o700)
        source = private / "source"
        source.mkdir()
        registry = Registry("ghcr.io", REPOSITORY)
        tag = registry.stable()
        published = registry.resolve(tag)
        run(["git", "init", "--quiet", str(source)], cwd=private)
        run(["git", "fetch", "--depth=1", "https://github.com/" + REPOSITORY + ".git", "refs/tags/" + tag], cwd=source,
            env=scanner_env(private))
        commit = run(["git", "rev-parse", "FETCH_HEAD^{commit}"], cwd=source).decode().strip()
        require(matches(r"[0-9a-f]{40}", commit))
        run(["git", "checkout", "--detach", commit], cwd=source)
        dockerfile = source / "Dockerfile"
        original = dockerfile.read_bytes()
        base = base_target(original)
        base_digest = Registry("registry-1.docker.io", "library/python").resolve(base.split(":", 1)[1])
        dockerfile.write_bytes(refresh_from(original, base, base_digest))
        run(build_argv(source, commit), cwd=private)
        rebuilt_id = image_info(LOCAL_IMAGE, private, commit=commit)
        run(["docker", "pull", "--platform", "linux/amd64", IMAGE + "@" + published], cwd=private)
        published_id = image_info(IMAGE + "@" + published, private, digest=published, commit=commit)
        production_smoke(source, private)
        upgrade_smoke(source, private, published, tag, commit)
        tool = acquire(private)
        (private / "trivy.yaml").write_text("{}\n")
        (private / "empty.ignore").write_text("")
        (private / "secret.yaml").write_text("{}\n")
        run([str(tool), "image", "--quiet", "--config", str(private / "trivy.yaml"), "--cache-dir", str(private / "cache"),
             "--download-db-only"], cwd=private, env=scanner_env(private))
        snapshot = database(private)
        pair = {"version": 1, "tag": tag, "commit": commit, "published_digest": published, "base_ref": base,
                "base_digest": base_digest, "rebuilt_id": rebuilt_id, "production_smoke": True, "upgrade_smoke": True}
        for side, reference, image_id in (("published", IMAGE + "@" + published, published_id), ("rebuilt", LOCAL_IMAGE, rebuilt_id)):
            archive = private / (side + ".tar")
            run(["docker", "image", "save", "--output", str(archive), reference], cwd=private)
            entry = {"image_id": image_id, "database": database(private)}
            require(entry["database"] == snapshot)
            for kind in ("vuln", "secret"):
                output = private / "report.json"
                output.touch(mode=0o600, exist_ok=False)
                try:
                    run(scan_argv(tool, archive, output, private, kind), cwd=private, env=scanner_env(private), timeout=1000)
                    require(0 < output.stat().st_size <= 64 * 1024 * 1024)
                    entry[kind] = parse_json(output.read_bytes())
                    native_rows(entry[kind], image_id, kind)
                finally:
                    output.unlink(missing_ok=True)
                require(database(private) == snapshot)
            archive.unlink()
            pair[side] = entry
        issue_body(pair)
        # Strip all native paths, package versions and other raw fields BEFORE
        # leaving the private context. Reconstitute only allowlisted evidence.
        for side in ("published", "rebuilt"):
            entry = pair[side]
            for kind in ("vuln", "secret"):
                rows = native_rows(entry[kind], entry["image_id"], kind)
                results = [{"Class": "os-pkgs" if eco == "debian" else "lang-pkgs", "Type": eco,
                            "Vulnerabilities": [{"VulnerabilityID": min(ids), "VendorIDs": sorted(ids), "PkgName": pkg,
                                                 "InstalledVersion": "redacted", "FixedVersion": "redacted", "Severity": severity}]}
                           for eco, pkg, ids, severity in rows]
                entry[kind] = {"SchemaVersion": 2, "Trivy": {"Version": TRIVY_VERSION}, "ArtifactType": "container_image",
                               "Metadata": {"ImageID": entry["image_id"]}, "Results": results or
                               ([{"Class": "os-pkgs", "Type": "debian"}] if kind == "vuln" else [])}
        issue_body(pair)
    return pair  # All native outputs and the temporary source are gone.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("qualify", "sync"))
    args = parser.parse_args(argv)
    try:
        receipt = Path(os.environ["RUNNER_TEMP"]) / "stable-rebuild-pair.json"
        if args.mode == "qualify":
            require(not receipt.exists())
            pair = qualify()
            body = issue_body(pair)
            with receipt.open("x") as stream:
                receipt.chmod(0o600)
                json.dump(pair, stream)
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(body + f"\nLocal rebuilt image ID (not a registry digest): `{pair['rebuilt_id']}`\n")
                summary.write(f"Database metadata: `{json.dumps(pair['published']['database'], sort_keys=True)}`\n")
                run_id = os.environ["GITHUB_RUN_ID"]
                require(matches(r"[1-9]\d*", run_id))
                summary.write(f"Run: https://github.com/{REPOSITORY}/actions/runs/{run_id}\n")
        else:
            require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and os.environ.get("GITHUB_REF") == "refs/heads/main")
            require(os.environ.get("GITHUB_EVENT_NAME") in ("schedule", "workflow_dispatch"))
            pair = parse_json(receipt.read_bytes())
            sync_issue(pair, token=os.environ["GH_TOKEN"])
        return 0
    except Exception:
        print("Stable rebuild qualification or synchronization incomplete; no clean comparison claimed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
