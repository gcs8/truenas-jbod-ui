"""Offline contracts; registry and issue traffic stays on synthetic loopback."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import io
import os
from contextlib import redirect_stderr, redirect_stdout
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/stable_rebuild_scan.py"
D1, D2, D3 = ("sha256:" + c * 64 for c in "abc")
DB = {"Version": 2, "UpdatedAt": "2026-11-25T00:00:00Z", "sha256": "d" * 64}


def load():
    spec = importlib.util.spec_from_file_location("stable_rebuild_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def finding(advisory="CVE-2026-12345", package="libexample", severity="HIGH", **extra):
    return {"VulnerabilityID": advisory, "PkgName": package, "InstalledVersion": "1",
            "FixedVersion": "2", "Severity": severity, **extra}


def report(image, vulns=(), *, secret=False):
    return {"SchemaVersion": 2, "Trivy": {"Version": "0.76.2"},
            "ArtifactType": "container_image", "Metadata": {"ImageID": image},
            "Results": ([{"Class": "secret", "Secrets": [{"Match": "SYNTHETIC"}]}] if secret else
                        [{"Class": "os-pkgs", "Type": "debian", "Vulnerabilities": list(vulns)}])}


def pair():
    return {"version": 1, "tag": "v0.23.0", "commit": "e" * 40,
            "published_digest": D1, "base_ref": "python:3.12-slim", "base_digest": D3,
            "rebuilt_id": D2, "production_smoke": True, "upgrade_smoke": True,
            "published": {"image_id": D1, "database": DB, "vuln": report(D1), "secret": report(D1)},
            "rebuilt": {"image_id": D2, "database": DB, "vuln": report(D2), "secret": report(D2)}}


class NewFeatureTests(unittest.TestCase):
    def test_self_contained_helper_exists(self):
        self.assertTrue(SCRIPT.is_file(), "stable rebuild scan helper is missing")


@unittest.skipUnless(SCRIPT.is_file(), "helper not implemented yet")
class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_zero_clear_and_new_are_separate(self):
        p = pair()
        p["published"]["vuln"] = report(D1, [finding()])
        p["rebuilt"]["vuln"] = report(D2, [finding(), finding("CVE-2026-99999", severity="CRITICAL")])
        result = self.m.compare(p)
        self.assertEqual(result["counts"], {"published": 1, "rebuilt": 2, "cleared": 0, "remaining": 1, "new": 1})
        self.assertEqual(result["severity"]["rebuilt"], {"HIGH": 1, "CRITICAL": 1})
        self.assertIn("Scan-only", self.m.issue_body(p))

    def test_aliases_duplicates_and_installed_paths_do_not_change_identity(self):
        p = pair()
        p["published"]["vuln"] = report(D1, [finding(VendorIDs=["GHSA-2345-2345-2345"]), finding()])
        p["rebuilt"]["vuln"] = report(D2, [finding("GHSA-2345-2345-2345", InstalledVersion="3", PkgPath="different")])
        self.assertEqual(self.m.compare(p)["counts"], {"published": 1, "rebuilt": 1, "cleared": 0, "remaining": 1, "new": 0})

    def test_package_and_ecosystem_are_not_lost(self):
        p = pair()
        p["published"]["vuln"] = report(D1, [finding(), finding(package="other")])
        p["rebuilt"]["vuln"] = report(D2, [finding()])
        self.assertEqual(self.m.compare(p)["counts"]["cleared"], 1)
        p["rebuilt"]["vuln"]["Results"][0].update(Class="lang-pkgs", Type="python-pkg")
        self.assertEqual(self.m.compare(p)["counts"]["cleared"], 2)
        self.assertEqual(self.m.compare(p)["counts"]["new"], 1)

    def test_python_package_aliases_normalize(self):
        p = pair()
        for side, image, package in (("published", D1, "Example_Pkg"), ("rebuilt", D2, "example-pkg")):
            p[side]["vuln"] = report(image, [finding(package=package)])
            p[side]["vuln"]["Results"][0].update(Class="lang-pkgs", Type="python-pkg")
        self.assertEqual(self.m.compare(p)["counts"]["remaining"], 1)

    def test_admission_refuses_incomplete_secret_db_mismatch_and_target_mismatch(self):
        mutations = [lambda p: p.pop("rebuilt"),
                     lambda p: p.update(production_smoke=False),
                     lambda p: p.update(upgrade_smoke=False),
                     lambda p: p["rebuilt"].update(database={**DB, "sha256": "f" * 64}),
                     lambda p: [p[side].update(database={**DB, "UpdatedAt": "2026-99-99T00:00:00Z"})
                                for side in ("published", "rebuilt")],
                     lambda p: p["rebuilt"]["vuln"].update(Trivy={"Version": "0.1"}),
                     lambda p: p["rebuilt"]["vuln"].update(Results=None),
                     lambda p: p["rebuilt"]["vuln"]["Metadata"].update(ImageID=D1),
                     lambda p: p["rebuilt"].update(secret=report(D2, secret=True)),
                     lambda p: p.update(tag="v01.2.3"),
                     lambda p: p.update(base_ref="python:3.13-alpine")]
        for mutate in mutations:
            p = copy.deepcopy(pair())
            mutate(p)
            with self.subTest(mutate=mutate), self.assertRaises(self.m.ScanError):
                self.m.issue_body(p)

    def test_body_is_deterministic_and_excludes_volatile_native_data(self):
        p = pair()
        p["published"]["vuln"] = report(D1, [finding()])
        body = self.m.issue_body(p)
        p["rebuilt_id"] = D3
        p["rebuilt"]["image_id"] = D3
        for kind in ("vuln", "secret"):
            p["rebuilt"][kind]["Metadata"]["ImageID"] = D3
        p["published"]["database"]["UpdatedAt"] = "2026-11-26T00:00:00Z"
        p["rebuilt"]["database"]["UpdatedAt"] = "2026-11-26T00:00:00Z"
        self.assertEqual(body, self.m.issue_body(p))
        self.assertNotIn("ImageID", body)
        self.assertNotIn("UpdatedAt", body)


@unittest.skipUnless(SCRIPT.is_file(), "helper not implemented yet")
class BuildTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_refresh_changes_only_from_suffix_preserving_exact_tag(self):
        for source in (b"# test\r\nFROM python:3.12-slim\r\nRUN true\r\n",
                       b"FROM python:3.12-slim@sha256:" + b"a" * 64 + b"\nRUN true\n"):
            target = self.m.base_target(source)
            self.assertEqual(target, "python:3.12-slim")
            refreshed = self.m.refresh_from(source, target, D3)
            self.assertEqual(refreshed, source.replace(source.split(b"FROM ")[1].splitlines()[0],
                                                      (target + "@" + D3).encode(), 1))
        with self.assertRaises(self.m.ScanError):
            self.m.refresh_from(b"FROM python:3.12-slim\n", "python:3.13-slim", D3)

    def test_unsupported_ambiguous_or_multistage_bases_fail_closed(self):
        for source in (b"FROM python:3.12-alpine\n", b"FROM python:3.12\n", b"FROM ubuntu:24.04\n",
                       b"FROM ${BASE}\n", b"FROM python:3.12-slim AS one\n", b"FROM python:3.12-slim\nFROM scratch\n",
                       b"FROM --platform=linux/amd64 python:3.12-slim\n"):
            with self.subTest(source=source), self.assertRaises(self.m.ScanError):
                self.m.base_target(source)

    def test_exact_local_build_command_and_only_loopback_smoke_push(self):
        command = self.m.build_argv(Path("context"), "e" * 40)
        self.assertEqual(command, ["docker", "buildx", "build", "--pull", "--no-cache", "--platform", "linux/amd64",
                                   "--load", "--build-arg", "SOURCE_COMMIT=" + "e" * 40,
                                   "--tag", "stable-rebuild-scan:local", "context"])
        self.assertNotIn("--push", command)
        with self.assertRaises(self.m.ScanError):
            self.m.loopback_push_argv("ghcr.io/example/image:test")
        self.assertEqual(self.m.loopback_push_argv(self.m.SMOKE_IMAGE), ["docker", "push", self.m.SMOKE_IMAGE])

    def test_published_image_is_bound_to_the_resolved_source_commit(self):
        with patch.object(self.m, "Registry") as registry, patch.object(self.m, "run") as run, \
                patch.object(self.m, "image_info") as inspect:
            registry.return_value.stable.return_value = "v0.23.0"
            registry.return_value.resolve.return_value = D3
            def native(argv, *, cwd, **kwargs):
                if argv[:2] == ["git", "checkout"]:
                    (cwd / "Dockerfile").write_bytes(b"FROM python:3.12-slim\n")
                return ("e" * 40).encode() if argv[:2] == ["git", "rev-parse"] else b""
            run.side_effect = native
            inspect.side_effect = [D2, self.m.ScanError("stop before smokes")]
            with self.assertRaises(self.m.ScanError):
                self.m.qualify()
            self.assertEqual(inspect.call_args.kwargs.get("commit"), "e" * 40)

    def test_scanner_commands_freeze_database_and_scan_all_secret_severities(self):
        for kind in ("vuln", "secret"):
            args = self.m.scan_argv(Path("trivy"), Path("image.tar"), Path("out.json"), Path("private"), kind)
            self.assertIn("--skip-db-update", args)
            self.assertIn("--skip-java-db-update", args)
            self.assertEqual(args[args.index("--platform") + 1], "linux/amd64")
            self.assertEqual(args[args.index("--scanners") + 1], kind)
            if kind == "vuln":
                self.assertEqual(args[args.index("--severity") + 1], "HIGH,CRITICAL")
                self.assertIn("--ignore-unfixed", args)
            else:
                self.assertIn("LOW", args[args.index("--severity") + 1])

    def test_generated_secret_configuration_keeps_all_native_defaults(self):
        tree = ast.parse(SCRIPT.read_text())
        writes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "write_text"
                  and isinstance(node.func.value, ast.BinOp)
                  and isinstance(node.func.value.right, ast.Constant)
                  and node.func.value.right.value == "secret.yaml"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.m.yaml.safe_load(ast.literal_eval(writes[0].args[0])), {})


class NativeRecipeTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_production_recipe_keeps_assertions_but_cannot_rebuild_or_mount_daemon(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "source"
            source.mkdir()
            (source / ".github/workflows").mkdir(parents=True)
            (source / "tests/fixtures").mkdir(parents=True)
            recipe = "docker compose up -d --build --wait\nassertions-kept\n"
            workflow = {"jobs": {"container-smoke": {"steps": [{"run": recipe}]}}}
            (source / ".github/workflows/ci.yml").write_text(self.m.yaml.safe_dump(workflow))
            fixture = source / "tests/fixtures/ci-smoke.compose.yml"
            fixture.write_text("services:\n  enclosure-ui:\n    build: .\n    ports: ['18080:8000']\n")
            compose = source / "docker-compose.yml"
            original = b"services:\n  enclosure-admin:\n    volumes: ['/var/run/docker.sock:/var/run/docker.sock', './data:/app/data']\n"
            compose.write_bytes(original)
            def native(argv, **kwargs):
                self.assertEqual(argv[-1], recipe.replace("--build", "--no-build"))
                config = self.m.yaml.safe_load(fixture.read_text())["services"]["enclosure-ui"]
                self.assertNotIn("build", config)
                self.assertEqual(config["image"], self.m.LOCAL_IMAGE)
                self.assertEqual(config["ports"], ["127.0.0.1:18080:8000"])
                self.assertNotIn("docker.sock", compose.read_text())
                self.assertEqual(kwargs["env"]["JBOD_UI_IMAGE"], self.m.LOCAL_IMAGE)
                return b""
            with patch.object(self.m, "run", side_effect=native), patch.object(self.m, "image_info", return_value=D2):
                self.m.production_smoke(source, Path(raw))
            self.assertEqual(compose.read_bytes(), original)

    def test_upgrade_keeps_published_baseline_and_literal_helper_with_only_loopback_push(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(self.m, "run", return_value=b"") as run:
            root = Path(raw)
            self.m.upgrade_smoke(root / "source", root, D1, "v0.23.0", "e" * 40)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual([c for c in commands if c[:2] == ["docker", "push"]], [["docker", "push", self.m.SMOKE_IMAGE]])
            helper = next(c for c in commands if "--previous-image" in c)
            self.assertEqual(helper[helper.index("--previous-image") + 1], self.m.IMAGE + "@" + D1)
            self.assertEqual(helper[helper.index("--scenario") + 1], "hardened")
            self.assertIn("--nonroot-fixture", helper)
            self.assertEqual(commands[-1], ["docker", "rm", "-f", "stable-rebuild-scan-registry"])
            self.assertFalse(any("--push" in c or "login" in c for c in commands))

    def test_cli_refuses_nonmain_writes_and_does_not_print_native_errors(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"RUNNER_TEMP": raw,
                "GITHUB_REPOSITORY": self.m.REPOSITORY, "GITHUB_REF": "refs/pull/1/merge", "GITHUB_EVENT_NAME": "pull_request"}), \
                patch.object(self.m, "sync_issue") as sync, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.m.main(["sync"]), 1)
            sync.assert_not_called()
            with patch.object(self.m, "qualify", side_effect=RuntimeError("SYNTHETIC_PRIVATE_VALUE")):
                self.assertEqual(self.m.main(["qualify"]), 1)
            self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse((Path(raw) / "stable-rebuild-pair.json").exists())


class DiagnosticTests(unittest.TestCase):
    PRIVATE = "SYNTHETIC_PRIVATE_DIAGNOSTIC_6b5e70c8"

    def setUp(self):
        self.m = load()

    def invoke(self, operation):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {"RUNNER_TEMP": raw}), \
                patch.object(self.m, "qualify", side_effect=operation), \
                patch.object(self.m, "sync_issue") as sync, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = self.m.main(["qualify"])
            sync.assert_not_called()
            self.assertFalse((Path(raw) / "stable-rebuild-pair.json").exists())
        text = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(self.PRIVATE, text)
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("Traceback", text)
        for line in text.splitlines():
            self.assertRegex(line, r"^stable_rebuild phase=[a-z_]+ event=(?:begin|complete|failure|native|http) "
                                   r"kind=(?:phase|native|http|python|timeout|os|transport) status=-?[0-9]+$")
            self.assertIn(line.split()[1].split("=", 1)[1], self.m.PHASES)
        return result, text

    def test_real_native_failure_preserves_exit_and_redacts_both_streams_and_argv(self):
        def operation():
            code = "import sys; print(sys.argv[1]); print(sys.argv[1], file=sys.stderr); sys.exit(37)"
            return self.m.run([sys.executable, "-c", code, self.PRIVATE], cwd=Path.cwd())
        result, text = self.invoke(operation)
        self.assertEqual(result, 1)
        self.assertIn("phase=qualification event=failure kind=native status=37", text)
        self.assertNotIn("sys.exit", text)

    def test_actual_http_failure_reports_only_numeric_status_without_headers_or_url(self):
        with FakeAPI() as api:
            api.deny = True
            result, text = self.invoke(lambda: self.m.http("GET", api.url + "/" + self.PRIVATE,
                                                        {"Authorization": "Bearer " + self.PRIVATE}))
            self.assertEqual(api.calls, [("GET", "/" + self.PRIVATE)])
        self.assertEqual(result, 1)
        self.assertIn("phase=qualification event=failure kind=http status=401", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)

    def test_python_and_schema_failure_use_fixed_generic_fallback(self):
        for operation in (lambda: (_ for _ in ()).throw(RuntimeError(self.PRIVATE)),
                          lambda: self.m.parse_json(self.PRIVATE)):
            with self.subTest(operation=operation):
                result, text = self.invoke(operation)
                self.assertEqual(result, 1)
                self.assertIn("phase=qualification event=failure kind=python status=1", text)

    def test_timeout_launch_signal_and_transport_failures_are_value_safe(self):
        def timeout():
            return self.m.run([sys.executable, "-c", "import time; time.sleep(30)", self.PRIVATE], cwd=Path.cwd(), timeout=0.1)
        def launch():
            return self.m.run(["/missing/" + self.PRIVATE], cwd=Path.cwd())
        def signal():
            return self.m.run([sys.executable, "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)", self.PRIVATE], cwd=Path.cwd())
        for operation, kind, status in ((timeout, "timeout", 1), (launch, "os", 1), (signal, "native", -15)):
            with self.subTest(kind=kind):
                result, text = self.invoke(operation)
                self.assertEqual(result, 1)
                self.assertIn(f"event=failure kind={kind} status={status}", text)
        with patch.object(self.m.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = self.m.urllib.error.URLError(self.PRIVATE)
            result, text = self.invoke(lambda: self.m.http("GET", "https://example.test/" + self.PRIVATE))
        self.assertEqual(result, 1)
        self.assertIn("event=failure kind=transport status=1", text)

    def test_unknown_phases_numeric_fields_and_cli_argv_cannot_escape_allowlists(self):
        for operation in (lambda: self.m.diagnostic(self.PRIVATE, "failure", "native", 7),
                          lambda: self.m.diagnostic("build", "failure", "native", self.PRIVATE)):
            result, text = self.invoke(operation)
            self.assertEqual(result, 1)
            self.assertIn("event=failure kind=python status=1", text)
        for argv in ([self.PRIVATE], ["qualify", "--" + self.PRIVATE], []):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(self.m.main(argv), 1)
            self.assertNotIn(self.PRIVATE, stdout.getvalue() + stderr.getvalue())
            self.assertIn("phase=startup event=failure kind=python status=1", stderr.getvalue())

    def test_upgrade_failure_attribution_survives_successful_or_failed_cleanup(self):
        native_run = subprocess.run
        for cleanup_exit in (0, 13):
            commands = []
            def native(argv, **kwargs):
                commands.append(argv[:2])
                if "--candidate-image" in argv:
                    return native_run([sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(29)", self.PRIVATE], **kwargs)
                return subprocess.CompletedProcess(argv, cleanup_exit if argv[:2] == ["docker", "rm"] else 0,
                                                   stdout=self.PRIVATE.encode(), stderr=self.PRIVATE.encode())
            with self.subTest(cleanup_exit=cleanup_exit), patch.object(self.m.subprocess, "run", side_effect=native):
                result, text = self.invoke(lambda: self.m.upgrade_smoke(Path("source"), Path.cwd(), D1, "v0.23.0", "e" * 40))
            self.assertEqual(result, 1)
            self.assertEqual(commands[-1], ["docker", "rm"])
            self.assertIn("phase=upgrade_recipe event=failure kind=native status=29", text)
            self.assertIn(f"phase=registry_cleanup event=native kind=native status={cleanup_exit}", text)
            if cleanup_exit:
                self.assertIn("phase=registry_cleanup event=failure kind=native status=13", text)

    def test_actual_qualification_attributes_failure_before_any_docker_or_scanner(self):
        native_run = subprocess.run
        commands = []
        def failed_init(argv, **kwargs):
            commands.append(argv[:2])
            return native_run([sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(19)", self.PRIVATE], **kwargs)
        with patch.object(self.m, "Registry") as registry, \
                patch.object(self.m.subprocess, "run", side_effect=failed_init), \
                patch.object(self.m, "acquire") as acquire:
            registry.return_value.stable.return_value = "v0.23.0"
            registry.return_value.resolve.return_value = D1
            operation = self.m.qualify
            result, text = self.invoke(operation)
            acquire.assert_not_called()
        self.assertEqual(result, 1)
        self.assertEqual(commands, [["git", "init"]])
        self.assertIn("phase=source_init event=failure kind=native status=19", text)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.issues = []
        self.late_error = False
        self.readback_bad = False
        self.ambiguous = False
        self.corrupt_after_write = False
        self.deny = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value, status=200, **headers):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if outer.deny:
                    self.reply({}, 401)
                    return
                if "state=all" in self.path:
                    if outer.late_error:
                        self.reply(outer.issues, Link=f'<{outer.url}/repos/gcs8/truenas-jbod-ui/issues?page=2>; rel="next"')
                    else:
                        self.reply(outer.issues)
                elif "page=2" in self.path:
                    self.reply({}, 500)
                else:
                    issue = copy.deepcopy(outer.issues[0])
                    if outer.readback_bad:
                        issue["body"] = "wrong readback"
                    self.reply(issue)

            def do_POST(self):
                outer.calls.append(("POST", self.path))
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.issues.append({"number": 41, "state": "open", "user": {"login": "github-actions[bot]"}, **data})
                outer.readback_bad = outer.corrupt_after_write
                self.reply({} if outer.ambiguous else outer.issues[0], 201)

            def do_PATCH(self):
                outer.calls.append(("PATCH", self.path))
                outer.issues[0].update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply(outer.issues[0])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


@unittest.skipUnless(SCRIPT.is_file(), "helper not implemented yet")
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_first_create_then_unchanged_run_has_zero_mutations(self):
        with FakeAPI() as api:
            self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertEqual([c[0] for c in api.calls].count("POST"), 1)
            self.assertEqual(api.calls[-1], ("GET", "/repos/gcs8/truenas-jbod-ui/issues/41"))
            api.calls.clear()
            self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertTrue(api.calls)
            self.assertEqual({c[0] for c in api.calls}, {"GET"})

    def test_closed_issue_reopens_and_get_verifies(self):
        with FakeAPI() as api:
            self.m.sync_issue(pair(), token="synthetic", api=api.url)
            api.issues[0]["state"] = "closed"
            api.calls.clear()
            self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertEqual([c[0] for c in api.calls], ["GET", "GET", "PATCH", "GET"])
            self.assertEqual(api.issues[0]["state"], "open")

    def test_late_page_ownership_duplicate_and_readback_failures_are_closed(self):
        for case in ("late", "owner", "duplicate", "readback"):
            with self.subTest(case=case), FakeAPI() as api:
                self.m.sync_issue(pair(), token="synthetic", api=api.url)
                if case == "late":
                    api.late_error = True
                if case == "owner":
                    api.issues[0]["user"]["login"] = "someone-else"
                if case == "duplicate":
                    api.issues.append({**api.issues[0], "number": 42})
                if case == "readback":
                    api.issues[0]["state"] = "closed"
                    api.readback_bad = True
                api.calls.clear()
                with self.assertRaises(self.m.ScanError):
                    self.m.sync_issue(pair(), token="synthetic", api=api.url)
                self.assertEqual({c[0] for c in api.calls}, {"GET"})

    def test_secret_pair_stops_before_any_api_call_and_ambiguous_post_is_not_retried(self):
        with FakeAPI() as api:
            p = pair()
            p["rebuilt"]["secret"] = report(D2, secret=True)
            with self.assertRaises(self.m.ScanError):
                self.m.sync_issue(p, token="synthetic", api=api.url)
            self.assertEqual(api.calls, [])
            api.ambiguous = True
            with self.assertRaises(self.m.ScanError):
                self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertEqual([c[0] for c in api.calls].count("POST"), 1)

    def test_auth_failure_has_no_writes_and_post_requires_exact_readback(self):
        with FakeAPI() as api:
            api.deny = True
            with self.assertRaises(self.m.ScanError):
                self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertEqual({c[0] for c in api.calls}, {"GET"})
        with FakeAPI() as api:
            api.corrupt_after_write = True
            with self.assertRaises(self.m.ScanError):
                self.m.sync_issue(pair(), token="synthetic", api=api.url)
            self.assertEqual([c[0] for c in api.calls], ["GET", "POST", "GET"])

    def test_tag_selection_is_strict_semver_and_pages_are_authoritative(self):
        pages = [{"name": "gcs8/truenas-jbod-ui", "tags": ["v9.9.9", "v10.0.0-rc.1", "latest"]},
                 {"name": "gcs8/truenas-jbod-ui", "tags": ["v10.0.0", "v01.99.0"]}]
        with patch.object(self.m, "pages", return_value=pages):
            registry = self.m.Registry("ghcr.io", "gcs8/truenas-jbod-ui", token="synthetic")
            self.assertEqual(registry.stable(), "v10.0.0")
        def bad_pages(*args):
            yield pages[0]
            raise self.m.ScanError("incomplete")
        with patch.object(self.m, "pages", side_effect=bad_pages), self.assertRaises(self.m.ScanError):
            registry.stable()

    def test_registry_refuses_ambiguous_platforms_and_digest_mismatch(self):
        manifest = {"schemaVersion": 2, "manifests": [
            {"digest": D1, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": D2, "platform": {"os": "linux", "architecture": "amd64"}}]}
        raw = json.dumps(manifest).encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        registry = self.m.Registry("ghcr.io", "gcs8/truenas-jbod-ui", token="synthetic")
        with patch.object(self.m, "http", return_value=(raw, {"Docker-Content-Digest": digest})):
            with self.assertRaises(self.m.ScanError):
                registry.resolve("v0.23.0")
        with patch.object(self.m, "http", return_value=(raw, {"Docker-Content-Digest": D3})):
            with self.assertRaises(self.m.ScanError):
                registry.resolve("v0.23.0")


if __name__ == "__main__":
    unittest.main()
