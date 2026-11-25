"""Offline synthetic qualification. No registry or GitHub service is contacted."""
from __future__ import annotations

import copy
import importlib.util
import json
import socket
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/published_image_scan.py"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
IMAGE = "ghcr.io/gcs8/truenas-jbod-ui"
STABLE = "v1.10.0"
TARGETS = {"latest": DIGEST, "dev": OTHER, STABLE: DIGEST}


def vulnerability(**changes):
    # These are synthetic scanner records, not claims about a real image.
    return {"VulnerabilityID": "CVE-2000-0001", "PkgName": "example-package",
            "InstalledVersion": "1.0", "FixedVersion": "1.1", "Severity": "HIGH", **changes}


def report(digest=DIGEST, vulns=None, secrets=None):
    return {"SchemaVersion": 2, "Trivy": {"Version": "0.76.2"},
            "ArtifactType": "container_image", "ArtifactName": IMAGE + "@" + digest,
            "Metadata": {"RepoDigests": [IMAGE + "@" + digest], "ImageID": digest,
                         "OS": {"Family": "debian", "Name": "13"}},
            "Results": [{"Target": "synthetic image", "Class": "os-pkgs", "Type": "debian",
                         "Vulnerabilities": [] if vulns is None else vulns,
                         "Secrets": [] if secrets is None else secrets}]}


class Server:
    def __init__(self):
        self.issues = []
        self.calls = []
        self.fail: tuple[str, int] | None = None
        self.fail_method: str | None = None
        self.ambiguous = False
        self.bad_readback = False
        self.redirect = False
        self.tags = ["latest", "dev", "v1.9.9", "v9.0.0-rc.1", STABLE, "v01.99.0"]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                self.handle_request("GET")

            def do_POST(self):
                self.handle_request("POST")

            def do_PATCH(self):
                self.handle_request("PATCH")

            def handle_request(self, method):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
                outer.calls.append((method, self.path, payload))
                status = 200
                headers = {}
                if outer.redirect:
                    status, data = 302, {}
                    headers["Location"] = outer.url + "/redirected"
                elif outer.fail and (outer.fail[0] in self.path) and outer.fail_method in (None, method):
                    status, data = outer.fail[1], {"message": "synthetic failure"}
                elif "/tags/list" in self.path:
                    second = "page=2" in self.path
                    data = {"name": "gcs8/truenas-jbod-ui", "tags": outer.tags[3:] if second else outer.tags[:3]}
                    if not second:
                        headers["link"] = '</v2/gcs8/truenas-jbod-ui/tags/list?page=2>; rel="next"'
                elif method == "GET" and "/issues?" in self.path:
                    second = "page=2" in self.path
                    data = outer.issues[1:] if second else outer.issues[:1]
                    if not second:
                        headers["link"] = '</repos/gcs8/truenas-jbod-ui/issues?state=all&per_page=100&page=2>; rel="next"'
                elif method == "POST":
                    data = {"number": len(outer.issues) + 1, "state": "open", "user": {"login": "github-actions[bot]"}, **payload}
                    outer.issues.append(data)
                    status = 201
                    if outer.ambiguous:
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                else:
                    number = int(self.path.rsplit("/", 1)[-1])
                    data = next(item for item in outer.issues if item["number"] == number)
                    if method == "PATCH":
                        data.update(payload)
                    if outer.bad_readback and method == "GET":
                        data = {**data, "state": "closed"}
                raw = json.dumps(data).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class PublishedImageScanTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "published image scanner implementation is missing")
        spec = importlib.util.spec_from_file_location("published_image_scan", SCRIPT)
        assert spec and spec.loader
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def bundle(self, vulns=None):
        reports = {DIGEST: report(vulns=vulns), OTHER: report(OTHER, vulns=vulns)}
        return self.mod.make_bundle(TARGETS, reports, {"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"})

    def api(self, server):
        return self.mod.GitHub("synthetic-token", base=server.url)

    def test_native_argv_is_explicit_remote_digest_and_no_extra_suppression(self):
        args = self.mod.scan_argv("trivy", IMAGE + "@" + DIGEST, "policy.yaml", "report.json", "cache")
        for key, value in {"--scanners": "vuln,secret", "--severity": "HIGH,CRITICAL",
                           "--ignorefile": "policy.yaml", "--format": "json", "--output": "report.json",
                           "--image-src": "remote", "--platform": "linux/amd64", "--exit-code": "0"}.items():
            self.assertEqual(args[args.index(key) + 1], value)
        self.assertIn("--ignore-unfixed", args)
        self.assertEqual(args[-1], IMAGE + "@" + DIGEST)
        for forbidden in ("--skip-files", "--skip-dirs", "--ignore-policy", "--ignore-status", "--skip-db-update"):
            self.assertNotIn(forbidden, args)

    def test_policy_rejects_expiry_unknowns_duplicates_and_secret_exceptions(self):
        good = 'vulnerabilities:\n  - id: CVE-2000-0001\n    statement: Synthetic review\n    expired_at: 2099-01-01\nsecrets: []\n'
        self.mod.validate_policy(good, today=date(2026, 1, 1))
        for bad in (good.replace("2099-01-01", "2026-01-01"), good.replace("2099-01-01", "2026-1-2"),
                    good.replace("2099-01-01", "2099-02-30"), good.replace("Synthetic review", "' '"),
                    good + "other: []\n", good + "secrets: []\n", good.replace("id:", "id: X\n    id:"),
                    good.replace("statement:", "paths: ['**']\n    statement:"),
                    good.replace("secrets: []", good.split("vulnerabilities:\n", 1)[1]),
                    good.replace("2099-01-01", "&expiry 2099-01-01").replace("secrets: []", "secrets: [*expiry]"),
                    good.replace("statement:", "<<: {other: value}\n    statement:"),
                    good.replace("secrets: []", "secrets: [{id: generic-api-key, statement: review, expired_at: 2099-01-01}]")):
            with self.subTest(case=bad), self.assertRaises(self.mod.ScanError):
                self.mod.validate_policy(bad, today=date(2026, 1, 1))

    def test_tag_pagination_semantic_stable_not_lexical_or_prerelease(self):
        server = Server()
        self.addCleanup(server.close)
        registry = self.mod.Registry(base=server.url, token="synthetic-token")
        self.assertEqual(registry.targets(), ["latest", "dev", STABLE])
        self.assertEqual(len(server.calls), 2)
        server.tags.remove("dev")
        with self.assertRaises(self.mod.ScanError):
            registry.targets()

    def test_stable_fingerprint_excludes_versions_severity_digest_and_metadata(self):
        first = self.bundle([vulnerability()])
        second = self.bundle([vulnerability(InstalledVersion="1.0.1", FixedVersion="1.2", Severity="CRITICAL")])
        self.assertEqual(set(first["findings"]), set(second["findings"]))
        self.assertEqual(len(first["findings"]), 1)
        finding = next(iter(first["findings"].values()))
        self.assertEqual({row["tag"] for row in finding["occurrences"]}, set(TARGETS))
        changed = self.bundle([vulnerability(PkgName="other-package")])
        self.assertNotEqual(set(first["findings"]), set(changed["findings"]))

    def test_malformed_missing_partial_error_output_fails(self):
        valid = report()
        bads = [None, {}, {**valid, "SchemaVersion": 3}, {**valid, "Results": None},
                {**valid, "Results": []}, {**valid, "Error": "unsafe diagnostic"},
                {**valid, "ArtifactName": IMAGE + "@" + OTHER}, {**valid, "Metadata": {}},
                {**valid, "Trivy": {"Version": "0.0.0"}},
                {**valid, "Results": [{"Class": "os-pkgs"}]},
                {**valid, "Results": [{"Target": "synthetic", "Type": "debian", "Class": "secret"}]},
                {**valid, "Results": [{"Target": "synthetic", "Type": "alpine", "Class": "os-pkgs"}]}]
        for bad in bads:
            with self.subTest(case=bad), self.assertRaises(self.mod.ScanError):
                self.mod.make_bundle(TARGETS, {DIGEST: bad, OTHER: report(OTHER)},
                                     {"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"})
        with self.assertRaises(self.mod.ScanError):
            self.mod.parse_json('{"Results": [], "Results": []}')
        for bad in (vulnerability(PkgName="unsafe\ntext"), vulnerability(FixedVersion=""),
                    vulnerability(Severity="LOW"), vulnerability(VulnerabilityID="unsafe identifier")):
            with self.assertRaises(self.mod.ScanError):
                self.bundle([bad])

    def test_changed_gone_reappear_and_unchanged_second_run_zero_writes(self):
        server = Server()
        self.addCleanup(server.close)
        api = self.api(server)
        bundle = self.bundle([vulnerability()])
        self.mod.reconcile(api, bundle)
        self.assertEqual([call[0] for call in server.writes()], ["POST"])
        self.assertEqual(len(server.issues), 1)
        body = server.issues[0]["body"]
        self.assertNotIn(DIGEST, body)
        for forbidden in ("CreatedAt", "UpdatedAt", "Description", "PrimaryURL", "Match", "Code"):
            self.assertNotIn(forbidden, body)
        server.calls.clear()
        second = copy.deepcopy(bundle)
        second["database"]["UpdatedAt"] = "2026-01-02T00:00:00Z"
        second["targets"] = {tag: OTHER for tag in TARGETS}
        self.mod.reconcile(api, second)
        self.assertEqual(server.writes(), [])
        changed = self.bundle([vulnerability(FixedVersion="1.2", Severity="CRITICAL")])
        self.mod.reconcile(api, changed)
        self.assertEqual(len(server.writes()), 1)
        self.assertEqual(server.writes()[0][0], "PATCH")
        server.calls.clear()
        self.mod.reconcile(api, self.bundle())
        self.assertEqual(server.issues[0]["state"], "closed")
        self.assertEqual(len(server.writes()), 1)
        server.calls.clear()
        self.mod.reconcile(api, changed)
        self.assertEqual(server.issues[0]["state"], "open")
        self.assertEqual(len(server.issues), 1)
        self.assertEqual(len(server.writes()), 1)
        server.calls.clear()
        self.mod.reconcile(api, changed)
        self.assertEqual(server.writes(), [])

    def test_finding_on_only_one_successful_target_stays_open(self):
        server = Server()
        self.addCleanup(server.close)
        api = self.api(server)
        self.mod.reconcile(api, self.bundle([vulnerability()]))
        reports = {DIGEST: report(), OTHER: report(OTHER, vulns=[vulnerability()])}
        bundle = self.mod.make_bundle(TARGETS, reports, {"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"})
        server.calls.clear()
        self.mod.reconcile(api, bundle)
        self.assertEqual(server.issues[0]["state"], "open")
        self.assertIn("`dev`", server.issues[0]["body"])
        self.assertNotIn("`latest`", server.issues[0]["body"])
        self.assertEqual([call[0] for call in server.writes()], ["PATCH"])

    def test_missing_one_target_must_not_close_or_write(self):
        server = Server()
        self.addCleanup(server.close)
        api = self.api(server)
        self.mod.reconcile(api, self.bundle([vulnerability()]))
        server.calls.clear()
        bundle = self.bundle()
        del bundle["targets"]["dev"]
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(api, bundle)
        self.assertEqual(server.writes(), [])
        with self.assertRaises(self.mod.ScanError):
            self.mod.make_bundle(TARGETS, {DIGEST: report()}, {})

    def test_secret_stops_before_any_public_write_and_report_fields_never_escape(self):
        server = Server()
        self.addCleanup(server.close)
        secret_report = report(vulns=[vulnerability()], secrets=[{"RuleID": "synthetic", "Match": "never-publish", "Code": {"Lines": []}}])
        with self.assertRaises(self.mod.PrivateTriage) as caught:
            bundle = self.mod.make_bundle(TARGETS, {DIGEST: secret_report, OTHER: report(OTHER)},
                                          {"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"})
            self.mod.reconcile(self.api(server), bundle)
        self.assertNotIn("never-publish", str(caught.exception))
        self.assertEqual(server.writes(), [])
        bundle = self.bundle([vulnerability(Description="never-publish", PrimaryURL="https://example.invalid/private")])
        self.mod.reconcile(self.api(server), bundle)
        self.assertNotIn("never-publish", json.dumps(server.writes()))
        self.assertNotIn("example.invalid", json.dumps(server.writes()))

    def test_foreign_markers_prs_and_human_copies_ignored_with_full_issue_pagination(self):
        server = Server()
        self.addCleanup(server.close)
        self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        original = copy.deepcopy(server.issues[0])
        server.issues = [{**original, "number": 1, "body": "<!-- another-workflow:v1 -->"},
                         {**original, "number": 2, "user": {"login": "human"}},
                         {**original, "number": 3, "pull_request": {}},
                         {**original, "number": 4}]
        server.calls.clear()
        self.mod.reconcile(self.api(server), self.bundle())
        self.assertEqual([call[1] for call in server.writes()], ["/repos/gcs8/truenas-jbod-ui/issues/4"])

    def test_auth_and_later_page_errors_fail_before_writes(self):
        for failure in (("/issues?", 401), ("page=2", 503)):
            server = Server()
            try:
                server.fail = failure
                with self.assertRaises(self.mod.ScanError):
                    self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
                self.assertEqual(server.writes(), [])
            finally:
                server.close()

    def test_ambiguous_post_not_retried_then_next_run_reconciles_authoritatively(self):
        server = Server()
        self.addCleanup(server.close)
        server.ambiguous = True
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        self.assertEqual(len(server.writes()), 1)
        self.assertEqual(len(server.issues), 1)
        server.ambiguous = False
        server.calls.clear()
        self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        self.assertEqual(server.writes(), [])

    def test_issue_write_failure_and_readback_mismatch_fail_job(self):
        server = Server()
        self.addCleanup(server.close)
        server.fail = ("/issues", 403)
        server.fail_method = "POST"
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        self.assertEqual(len(server.writes()), 1)
        self.assertEqual(server.issues, [])
        server.fail = None
        server.bad_readback = True
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))

    def test_bad_native_metadata_is_a_controlled_failure(self):
        for key, value in (("Trivy", []), ("Metadata", {"RepoDigests": None}), ("Results", [{"Class": "os-pkgs", "Vulnerabilities": None}])):
            native = report()
            native[key] = value
            with self.subTest(field=key), self.assertRaises(self.mod.ScanError):
                self.mod.make_bundle(TARGETS, {DIGEST: native, OTHER: report(OTHER)},
                                     {"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"})

    def test_policy_expiry_stops_before_acquisition_or_network(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / ".trivyignore.yaml").write_text('vulnerabilities: [{id: CVE-2000-0001, statement: review, expired_at: 2000-01-01}]\nsecrets: []\n')
            with patch.object(self.mod, "ROOT", root), patch.object(self.mod, "acquire") as acquire:
                with self.assertRaises(self.mod.ScanError):
                    self.mod.scan(Path(temporary) / "bundle.json")
                acquire.assert_not_called()

    def test_failed_second_digest_cannot_publish_complete_bundle(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / ".trivyignore.yaml").write_text('vulnerabilities: []\nsecrets: []\n')
            output = Path(temporary) / "bundle.json"
            output.write_text("stale bundle")
            class Registry:
                def targets(self):
                    return list(TARGETS)
                def resolve(self, tag):
                    return TARGETS[tag]
            count = 0
            def scan_image(tool, digest, policy, directory):
                nonlocal count
                count += 1
                if count == 2:
                    raise self.mod.ScanError("synthetic native failure")
                metadata = directory / "cache/db/metadata.json"
                metadata.parent.mkdir(parents=True)
                metadata.write_text('{"Version": 2, "UpdatedAt": "2026-01-01T00:00:00Z"}')
                return report(digest)
            with patch.object(self.mod, "ROOT", root), patch.object(self.mod, "Registry", Registry), patch.object(self.mod, "acquire", return_value="trivy"), patch.object(self.mod, "scan_image", scan_image):
                with self.assertRaises(self.mod.ScanError):
                    self.mod.scan(output)
            self.assertEqual(count, 2)
            self.assertFalse(output.exists())

    def test_duplicate_managed_identity_prevents_the_entire_plan(self):
        server = Server()
        self.addCleanup(server.close)
        self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        server.issues.append({**server.issues[0], "number": 2})
        server.calls.clear()
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle())
        self.assertEqual(server.writes(), [])

    def test_closed_absent_second_run_does_not_write(self):
        server = Server()
        self.addCleanup(server.close)
        self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        self.mod.reconcile(self.api(server), self.bundle())
        server.calls.clear()
        self.mod.reconcile(self.api(server), self.bundle())
        self.assertEqual(server.writes(), [])

    def test_acquisition_rejects_unverified_bytes_before_execution(self):
        with TemporaryDirectory() as temporary:
            with patch.object(self.mod, "http", return_value=(b"untrusted manifest", {})), patch.object(self.mod.subprocess, "run") as runner:
                with self.assertRaises(self.mod.ScanError):
                    self.mod.acquire(Path(temporary))
                runner.assert_not_called()
            with patch.object(self.mod, "http", return_value=(b"untrusted manifest", {})), patch.object(self.mod, "MANIFEST_SHA256", self.mod.hashlib.sha256(b"untrusted manifest").hexdigest()):
                with self.assertRaises(self.mod.ScanError):
                    self.mod.acquire(Path(temporary))

    def test_digest_mismatch_and_missing_auth_are_not_clean(self):
        registry = self.mod.Registry(token="synthetic-token")
        with patch.object(self.mod, "http", return_value=(b'{"schemaVersion":2,"layers":[]}', {"Docker-Content-Digest": DIGEST})):
            with self.assertRaises(self.mod.ScanError):
                registry.resolve("latest")
        with patch.object(self.mod, "http", return_value=(b'{}', {})):
            with self.assertRaises(self.mod.ScanError):
                self.mod.Registry()

    def test_authenticated_redirect_never_forwards_token_or_mutates(self):
        server = Server()
        self.addCleanup(server.close)
        server.redirect = True
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(server.writes(), [])

    def test_patch_error_aborts_without_swallowing_or_retry(self):
        server = Server()
        self.addCleanup(server.close)
        self.mod.reconcile(self.api(server), self.bundle([vulnerability()]))
        server.calls.clear()
        server.fail = ("/issues/1", 503)
        server.fail_method = "PATCH"
        with self.assertRaises(self.mod.ScanError):
            self.mod.reconcile(self.api(server), self.bundle())
        self.assertEqual(len(server.writes()), 1)
        self.assertEqual(server.issues[0]["state"], "open")

    def test_scanner_exit_failure_deletes_raw_and_never_returns_bundle(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            def runner(argv, **kwargs):
                self.assertEqual(kwargs["stdout"], self.mod.subprocess.DEVNULL)
                self.assertEqual(kwargs["stderr"], self.mod.subprocess.DEVNULL)
                self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "TMPDIR", "LANG"})
                self.assertEqual((path / "report.json").stat().st_mode & 0o777, 0o600)
                (path / "report.json").write_text('sensitive raw output')
                return type("Exit", (), {"returncode": 2})()
            with patch.object(self.mod.subprocess, "run", runner), self.assertRaises(self.mod.ScanError):
                self.mod.scan_image("trivy", DIGEST, path / "policy.yaml", path)
            self.assertFalse((path / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
