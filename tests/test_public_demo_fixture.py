from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from pydantic import ValidationError

from app import __version__
from app.services.public_demo_fixture import (
    PUBLIC_DEMO_ENCLOSURE_ID,
    PUBLIC_DEMO_FIXTURE_PATH,
    PUBLIC_DEMO_GENERATED_AT,
    PUBLIC_DEMO_SYSTEM_ID,
    PublicDemoFixture,
    build_public_demo_html,
    build_public_demo_sas_fabric,
    build_public_demo_snapshot_bundle,
    load_public_demo_fixture,
)
from app.services.snapshot_export import EXPORT_HISTORY_CACHE, EXPORT_RENDER_CACHE, EXPORT_ZIP_CACHE
from scripts.public_demo_source_parity import recorded_source_integrity


ROOT = Path(__file__).resolve().parents[1]


def demo_app_version() -> str:
    """The app version the checked-in demo was built from.

    A prerelease bumps ``__version__`` but leaves the demo for the next stable
    release, so the demo may trail the source. Mirrors the checker's PR mode.
    """
    html = (ROOT / "public-demo/index.html").read_text(encoding="utf-8")
    recorded = recorded_source_integrity(html, source_root=ROOT)
    return recorded[1] if recorded and recorded[1] else __version__


def clear_export_caches() -> None:
    EXPORT_HISTORY_CACHE.clear()
    EXPORT_RENDER_CACHE.clear()
    EXPORT_ZIP_CACHE.clear()


def run_checker(demo_dir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir), *extra],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class PublicDemoArtifactTests(unittest.TestCase):
    def copy_artifact(self, target: Path) -> Path:
        target.mkdir(parents=True)
        shutil.copy2(ROOT / "public-demo/index.html", target / "index.html")
        shutil.copy2(ROOT / "public-demo/.nojekyll", target / ".nojekyll")
        return target / "index.html"

    def test_checked_in_artifact_is_publishable_and_reports_sizes(self) -> None:
        result = run_checker(ROOT / "public-demo")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public demo artifact is publishable", result.stdout)
        self.assertIn("raw=", result.stdout)
        self.assertIn("gzip=", result.stdout)

    def test_checked_in_artifact_has_synthetic_operator_markers(self) -> None:
        html = (ROOT / "public-demo/index.html").read_text(encoding="utf-8")

        for marker in (
            "Demo data",
            f'id="snapshot-app-version">v{demo_app_version()}<',
            PUBLIC_DEMO_GENERATED_AT.isoformat(),
            "A 60-bay JBOD with made-up disks.",
            "About this demo",
            "Everything on this page is made-up demo data.",
            "Synthetic IDs",
            "Demo Storage Host",
            "Demo 60-Bay Top Loader",
            "Demo Boot Modules",
            "Demo 4x NVMe Carrier",
            "mirror-8",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        self.assertNotIn("Live-derived", html)
        self.assertNotIn("history/history.db", html)
        self.assertNotIn('id="sas-fabric-view-link"', html)

    def test_artifact_version_mismatch_is_rejected(self) -> None:
        version = demo_app_version()
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            artifact = self.copy_artifact(demo_dir)
            artifact.write_text(
                artifact.read_text(encoding="utf-8").replace(
                    f'id="snapshot-app-version">v{version}<',
                    'id="snapshot-app-version">v0.0.0-stale<',
                ),
                encoding="utf-8",
            )
            result = run_checker(demo_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"does not match source {version}", result.stderr)

    def test_missing_source_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            artifact = self.copy_artifact(demo_dir)
            artifact.write_text(artifact.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
            result = run_checker(demo_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing public demo source parity manifest", result.stderr)

    def test_forbidden_route_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            artifact = self.copy_artifact(demo_dir)
            with artifact.open("a", encoding="utf-8") as handle:
                handle.write('\n<a id="sas-fabric-view-link">Storage Fabric</a>\n')
            result = run_checker(demo_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("snapshot Storage Fabric route action", result.stderr)

    def test_external_or_missing_local_resource_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            artifact = self.copy_artifact(demo_dir)
            with artifact.open("a", encoding="utf-8") as handle:
                handle.write('\n<script src="missing-local-asset.js"></script>\n')
            result = run_checker(demo_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external or local resource reference", result.stderr)

    def test_raw_and_gzip_budgets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self.copy_artifact(demo_dir)
            raw = run_checker(demo_dir, "--max-raw-bytes", "64")
            gzip = run_checker(demo_dir, "--max-gzip-bytes", "64")

        self.assertNotEqual(raw.returncode, 0)
        self.assertIn("raw size", raw.stderr)
        self.assertNotEqual(gzip.returncode, 0)
        self.assertIn("gzip size", gzip.stderr)


class CurrentSourceDemoArtifactTests(unittest.TestCase):
    # These are all exported object/cache locations, not the inlined app source.
    DATA_LOCATIONS = (
        "snapshot", "storageViewsRuntime", "snapshotExportMeta",
        "preloadedHistoryBySlot", "preloadedSmartSummariesBySlot",
        "preloadedSnapshotsByEnclosure", "preloadedSnapshotSmartSummaries",
        "preloadedStorageViewSmartSummaries", "preloadedHistorySummary",
        "preloadedSasFabric",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="current-demo-checker-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.demo_dir = Path(cls.temp.name)
        cls.artifact = cls.demo_dir / "index.html"
        command = [sys.executable, "scripts/build_public_demo.py", "--output", str(cls.artifact)]
        for extra in ([], ["--check"]):
            result = subprocess.run(command + extra, cwd=ROOT, text=True, capture_output=True, check=False)
            if result.returncode:
                raise AssertionError(result.stderr)
        (cls.demo_dir / ".nojekyll").touch()
        cls.clean_html = cls.artifact.read_text(encoding="utf-8")

    def setUp(self) -> None:
        self.artifact.write_text(self.clean_html, encoding="utf-8")

    def reseal(self, html: str) -> None:
        # Test-only mutation: keep provenance checks active and valid so a
        # negative control cannot pass merely because its bytes changed.
        from scripts.public_demo_source_parity import (
            SOURCE_PARITY_PREFIX, SOURCE_PARITY_SUFFIX, parse_manifest,
            sha256_text, source_output_digest,
        )

        manifest, body, errors = parse_manifest(html)
        self.assertEqual(errors, [])
        manifest["artifact_sha256"] = sha256_text(body)
        manifest["source_output_sha256"] = source_output_digest(manifest["sources"], body)
        self.artifact.write_text(
            SOURCE_PARITY_PREFIX + json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            + SOURCE_PARITY_SUFFIX + body,
            encoding="utf-8",
        )

    def inject_data(self, location: str, payload: object) -> None:
        match = re.search(rf"^    {location}:\s*", self.clean_html, re.MULTILINE)
        self.assertIsNotNone(match, location)
        assert match is not None
        start = match.end()
        value, end = json.JSONDecoder().raw_decode(self.clean_html[start:])
        self.assertIsInstance(value, dict, location)
        value["synthetic_checker_control"] = payload
        self.reseal(self.clean_html[:start] + json.dumps(value) + self.clean_html[start + end:])

    def test_fresh_build_is_publishable_in_integrity_and_current_modes(self) -> None:
        self.assertIn('serial: "Serial"', self.clean_html)
        for flags in ((), ("--require-current",)):
            with self.subTest(flags=flags):
                result = run_checker(self.demo_dir, *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Public demo artifact is publishable", result.stdout)

    def test_non_demo_serial_in_every_exported_data_location_is_rejected(self) -> None:
        for location in self.DATA_LOCATIONS:
            for flags in ((), ("--require-current",)):
                with self.subTest(location=location, flags=flags):
                    self.inject_data(location, [{"serial": "SANITIZED-CHECKER-CONTROL"}])
                    result = run_checker(self.demo_dir, *flags)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(
                        result.stderr.strip(),
                        f"found non-demo serial at APP_BOOTSTRAP.{location}.synthetic_checker_control[0].serial",
                    )

    def test_serial_label_is_not_exempt_when_it_is_exported_data(self) -> None:
        self.inject_data("snapshot", {"serial_number": "Serial"})
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr.strip(),
            "found non-demo serial at APP_BOOTSTRAP.snapshot.synthetic_checker_control.serial_number",
        )

    def test_nested_serialized_history_details_are_checked(self) -> None:
        self.inject_data("preloadedHistoryBySlot", {"details_json": json.dumps({"serial": "SANITIZED-HISTORY"})})
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertIn("found non-demo serial at APP_BOOTSTRAP.preloadedHistoryBySlot", result.stderr)
        self.assertNotIn("fingerprint", result.stderr)

    def test_nested_duplicate_json_properties_fail_closed_in_both_modes(self) -> None:
        cases = (
            r'{"\u0073erial":"SANITIZED-HISTORY","\u0073erial":"DEMO-CONTROL"}',
            r'{"serial":"DEMO-CONTROL","\u0073erial":"SANITIZED-HISTORY"}',
            '{"serial_number":"SANITIZED-HISTORY","serial_number":"DEMO-CONTROL"}',
            r'[{"items":[{"\u0073erial_number":"SANITIZED-HISTORY","serial_number":""}]}]',
            r'{"\u0073erial":"DEMO-FIRST","serial":"DEMO-SECOND"}',
            r'{"value":null,"\u0076alue":null}',
        )
        for details in cases:
            for flags in ((), ("--require-current",)):
                with self.subTest(details=details, flags=flags):
                    self.inject_data("preloadedHistoryBySlot", {"details_json": details})
                    result = run_checker(self.demo_dir, *flags)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stderr.strip(), "invalid public demo APP_BOOTSTRAP data")

    def test_nested_json_depth_failure_is_not_a_raw_report_in_both_modes(self) -> None:
        details = "[" * 1100 + r'{"\u0073erial":"SANITIZED-HISTORY"}' + "]" * 1100
        self.inject_data("preloadedHistoryBySlot", {"details_json": details})
        for flags in ((), ("--require-current",)):
            with self.subTest(flags=flags):
                result = run_checker(self.demo_dir, *flags)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stderr.strip(), "invalid public demo APP_BOOTSTRAP data")

    def test_nested_json_lists_decode_serials_in_both_modes(self) -> None:
        self.inject_data("preloadedHistoryBySlot", {
            "details_json": r'[{"items":[{"\u0073erial_number":"SANITIZED-HISTORY"}]}]',
        })
        for flags in ((), ("--require-current",)):
            with self.subTest(flags=flags):
                result = run_checker(self.demo_dir, *flags)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stderr.strip(),
                                 "found non-demo serial at APP_BOOTSTRAP.preloadedHistoryBySlot."
                                 "synthetic_checker_control.details_json[0].items[0].serial_number")

    def test_nested_json_demo_and_absent_serials_pass_in_both_modes(self) -> None:
        self.inject_data("preloadedHistoryBySlot", {
            "details_json": json.dumps([{"items": [{"serial": value, "serial_number": value}
                                                   for value in ("DEMO-CONTROL", "", None, "null")]}]),
        })
        for flags in ((), ("--require-current",)):
            with self.subTest(flags=flags):
                result = run_checker(self.demo_dir, *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("Public demo artifact is publishable", result.stdout)

    def test_non_json_raw_reports_preserve_serial_checks_in_both_modes(self) -> None:
        cases = (
            ("serial_number = 'SANITIZED-REPORT'", True),
            ("[report] serial: 'SANITIZED-REPORT'", True),
            ("serial_number = 'DEMO-CONTROL'", False),
            ("serial: ''", False),
            ("serial: 'null'", False),
            ("[report] no serial available", False),
        )
        for report, rejected in cases:
            for flags in ((), ("--require-current",)):
                with self.subTest(report=report, flags=flags):
                    self.inject_data("preloadedSmartSummariesBySlot", {"report": report})
                    result = run_checker(self.demo_dir, *flags)
                    self.assertEqual(result.returncode, int(rejected), result.stdout + result.stderr)
                    if rejected:
                        self.assertEqual(result.stderr.strip(),
                                         "found non-demo serial at APP_BOOTSTRAP.preloadedSmartSummariesBySlot."
                                         "synthetic_checker_control.report")
                    else:
                        self.assertEqual(result.stderr, "")
                        self.assertIn("Public demo artifact is publishable", result.stdout)

    def test_demo_and_absent_serials_remain_supported(self) -> None:
        self.inject_data("snapshot", [{"serial": value} for value in ("DEMO-TEST", "", None, "null")])
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsealed_data_mutation_still_fails_fingerprints(self) -> None:
        self.artifact.write_text(self.clean_html + "\n<!-- synthetic mutation -->\n", encoding="utf-8")
        for flags in ((), ("--require-current",)):
            result = run_checker(self.demo_dir, *flags)
            self.assertEqual(result.returncode, 1)
            self.assertIn("public demo embedded output fingerprint mismatch", result.stderr)
            self.assertIn("source/output parity fingerprint mismatch", result.stderr)

    def test_resealed_external_resource_still_fails_resource_detector(self) -> None:
        self.reseal(self.clean_html + '\n<script src="missing-local-asset.js"></script>\n')
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "found external or local resource reference")

    def test_escaped_serial_keys_and_values_are_decoded(self) -> None:
        self.inject_data("snapshot", {"serial": "SANITIZED-ESCAPED"})
        html = self.artifact.read_text(encoding="utf-8")
        self.reseal(html.replace(
            '"serial": "SANITIZED-ESCAPED"',
            r'"\u0073erial": "\u0053ANITIZED-ESCAPED"',
            1,
        ))
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr.strip(),
            "found non-demo serial at APP_BOOTSTRAP.snapshot.synthetic_checker_control.serial",
        )

    def test_raw_report_serials_remain_checked(self) -> None:
        self.inject_data("preloadedSmartSummariesBySlot", {"report": "serial_number = 'SANITIZED-REPORT'"})
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr.strip(),
            "found non-demo serial at APP_BOOTSTRAP.preloadedSmartSummariesBySlot.synthetic_checker_control.report",
        )

    def test_bootstrap_missing_duplicate_or_duplicate_keys_fail_closed(self) -> None:
        cases = (
            (self.clean_html.replace("window.APP_BOOTSTRAP =", "window.UNUSED_BOOTSTRAP =", 1),
             "missing or ambiguous public demo APP_BOOTSTRAP data"),
            (self.clean_html + "\n<script>window.APP_BOOTSTRAP = {};</script>",
             "missing or ambiguous public demo APP_BOOTSTRAP data"),
            (self.clean_html.replace("window.APP_BOOTSTRAP = {", 'window.APP_BOOTSTRAP = { snapshot: {},', 1),
             "invalid public demo APP_BOOTSTRAP data"),
        )
        for html, error in cases:
            with self.subTest(error=error):
                self.reseal(html)
                result = run_checker(self.demo_dir, "--require-current")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.strip(), error)

    def test_resealed_source_change_still_fails_embedded_source_check(self) -> None:
        self.reseal(self.clean_html.replace('serial: "Serial"', 'serial: "Changed label"', 1))
        for flags in ((), ("--require-current",)):
            result = run_checker(self.demo_dir, *flags)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr.strip(), "embedded source mismatch: app/static/app.js")

    def test_identity_and_other_sensitive_detectors_still_scan_whole_html(self) -> None:
        for injection, expected in (
            ('<a id="sas-fabric-view-link">Storage Fabric</a>', "found forbidden snapshot Storage Fabric route action"),
            ("Live-derived", "found forbidden live-derived provenance claim"),
            ("history/history.db", "found forbidden local history dependency"),
            ('{"password":"synthetic-control"}', "found non-empty credential value"),
            ("-----BEGIN " + "PRIVATE KEY-----", "found private key block"),
            ("OPENSSH " + "PRIVATE KEY", "found OpenSSH key material"),
            (".".join(("10", "20", "30", "40")), "found private IPv4 address"),
        ):
            with self.subTest(expected=expected):
                self.reseal(self.clean_html + "\n" + injection)
                result = run_checker(self.demo_dir, "--require-current")
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("fingerprint", result.stderr)

    def test_malformed_bootstrap_is_rejected_even_with_valid_fingerprints(self) -> None:
        self.reseal(self.clean_html.replace("window.APP_BOOTSTRAP = {", "window.APP_BOOTSTRAP = { invalid,", 1))
        result = run_checker(self.demo_dir, "--require-current")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "invalid public demo APP_BOOTSTRAP data")


class PublicDemoBuildScriptTests(unittest.TestCase):
    def test_current_source_fixture_imports_under_synthetic_win32_without_fcntl(self) -> None:
        probe = """
import asyncio
import builtins
import runpy
import sys

script = sys.argv[1]
real_import = builtins.__import__

def portable_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("synthetic Windows has no fcntl")
    return real_import(name, *args, **kwargs)

builtins.__import__ = portable_import
runpy.run_path(script, run_name="synthetic_windows_import_probe")
print("synthetic-win32-import: PASS")
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                str(ROOT / "scripts/build_current_source_browser_fixture.py"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("synthetic-win32-import: PASS", result.stdout)

    def test_current_source_browser_fixture_requires_explicit_output(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/build_current_source_browser_fixture.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--output", result.stderr)

    def test_current_source_browser_fixture_ignores_malformed_operator_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            malformed_config = Path(temp_dir) / "malformed.yaml"
            malformed_config.write_text("systems: [\n", encoding="utf-8")
            malformed_output = Path(temp_dir) / "malformed-config.html"
            clean_output = Path(temp_dir) / "clean-env.html"
            malformed_env = os.environ.copy()
            malformed_env["APP_CONFIG_PATH"] = str(malformed_config)
            clean_env = os.environ.copy()
            clean_env.pop("APP_CONFIG_PATH", None)

            malformed_result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_current_source_browser_fixture.py",
                    "--output",
                    str(malformed_output),
                ],
                cwd=ROOT,
                env=malformed_env,
                text=True,
                capture_output=True,
                check=False,
            )
            clean_result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_current_source_browser_fixture.py",
                    "--output",
                    str(clean_output),
                ],
                cwd=ROOT,
                env=clean_env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(malformed_result.returncode, 0, malformed_result.stderr)
            self.assertNotIn("Traceback", malformed_result.stderr)
            self.assertEqual(clean_result.returncode, 0, clean_result.stderr)
            self.assertEqual(malformed_output.read_bytes(), clean_output.read_bytes())
            self.assertIn(
                "current-source-browser-fixture",
                malformed_output.read_text(encoding="utf-8"),
            )

    def test_current_source_browser_fixture_is_deterministic_and_inlines_candidate_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = Path(temp_dir) / "first.html"
            second_path = Path(temp_dir) / "second.html"
            results = [
                subprocess.run(
                    [
                        sys.executable,
                        "scripts/build_current_source_browser_fixture.py",
                        "--output",
                        str(output_path),
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                for output_path in (first_path, second_path)
            ]

            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built current-source browser fixture", result.stdout)

            first_html = first_path.read_text(encoding="utf-8")
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            for asset_name in ("app.js", "style.css"):
                asset_bytes = (ROOT / "app" / "static" / asset_name).read_bytes()
                self.assertIn(
                    f"{asset_name}_sha256={hashlib.sha256(asset_bytes).hexdigest()}",
                    first_html,
                )
            self.assertIn("SYNTHETIC-SLOT-0001", first_html)
            self.assertIn('"identify_active": true', first_html)
            self.assertNotIn('src="/static/app.js"', first_html)
            self.assertNotIn('href="/static/style.css"', first_html)
            self.assertIn('const trustedBase = new URL("/", window.location.origin);', first_html)
            self.assertIn("new URLSearchParams(target.search)", first_html)
            self.assertIn("buildScopedUrl(url, queryParams)", first_html)
            self.assertNotIn("`${url}?${params.toString()}`", first_html)
            self.assertNotIn("history/history.db", first_html)


class PublicDemoFixtureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_export_caches()

    def test_checked_in_fixture_is_complete_synthetic_schema(self) -> None:
        fixture = load_public_demo_fixture(PUBLIC_DEMO_FIXTURE_PATH)

        self.assertEqual(fixture.schema_version, 1)
        self.assertEqual(fixture.provenance, "synthetic")
        self.assertEqual(fixture.history_window_hours, 168)
        self.assertEqual(len(fixture.slots), 60)
        self.assertEqual([slot.slot for slot in fixture.slots], list(range(60)))
        self.assertEqual({view.id for view in fixture.storage_views}, {"boot-doms", "nvme-carrier-x4"})

    def test_checked_in_fixture_declares_a_synthetic_storage_fabric(self) -> None:
        fixture = load_public_demo_fixture(PUBLIC_DEMO_FIXTURE_PATH)
        fabric = fixture.storage_fabric

        self.assertEqual(len(fabric.controllers), 2)
        self.assertEqual(
            [controller.id for controller in fabric.controllers],
            ["demo-hba-0", "demo-hba-1"],
        )
        states = [path.state for controller in fabric.controllers for path in controller.paths]
        self.assertIn("fail", states, "a non-active path must exist so impacted chips are visible")
        covered = [
            slot_number
            for controller in fabric.controllers
            for path in controller.paths
            for slot_number in path.slot_numbers
        ]
        self.assertEqual(sorted(covered), list(range(60)))
        self.assertEqual(len(covered), len(set(covered)), "paths must not claim the same bay twice")

    def test_fixture_rejects_overlapping_fabric_paths(self) -> None:
        payload = json.loads(PUBLIC_DEMO_FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["storage_fabric"]["controllers"][1]["paths"][0]["slot_range"] = [0, 59]

        with self.assertRaisesRegex(
            ValidationError,
            r"storage fabric paths must cover bays 0-59 exactly once",
        ):
            PublicDemoFixture.model_validate(payload)

    def test_storage_fabric_payload_is_shaped_for_the_bay_grid(self) -> None:
        fabric = build_public_demo_sas_fabric()
        payload = fabric.model_dump(mode="json")

        self.assertTrue(payload["available"])
        self.assertEqual(payload["platform"], "core")
        self.assertEqual(payload["selected_enclosure_id"], PUBLIC_DEMO_ENCLOSURE_ID)
        self.assertEqual(len(payload["controllers"]), 2)
        self.assertEqual(len(payload["paths"]), 3)

        lane_one = [
            slot_number
            for path in payload["paths"]
            if path["controller"] == "demo-hba-0"
            for slot_number in path["slots"]
        ]
        lane_two = [
            slot_number
            for path in payload["paths"]
            if path["controller"] == "demo-hba-1"
            for slot_number in path["slots"]
        ]
        self.assertEqual(sorted(lane_one), list(range(45)))
        self.assertEqual(sorted(lane_two), list(range(45, 60)))

        kinds = {node["kind"] for node in payload["nodes"]}
        self.assertEqual(kinds, {"host", "controller", "expander", "ses-enclosure"})
        self.assertEqual(
            sorted(trace["kind"] for trace in payload["traces"] if trace["kind"] != "bay"),
            ["path", "path", "path"],
        )
        self.assertIn("bay:57", {trace["id"] for trace in payload["traces"]})
        self.assertEqual(
            [path["state"] for path in payload["paths"]],
            ["active", "fail", "active"],
        )

    def test_public_demo_html_embeds_the_frozen_storage_fabric(self) -> None:
        bundle = build_public_demo_snapshot_bundle()

        self.assertIsNotNone(bundle.sas_fabric)
        self.assertTrue(bundle.sas_fabric.available)

    def test_fixture_rejects_a_storage_view_missing_a_template_slot(self) -> None:
        payload = json.loads(PUBLIC_DEMO_FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["storage_views"][0]["slots"].pop()

        with self.assertRaisesRegex(
            ValidationError,
            r"storage view boot-doms must declare exactly template slots 0, 1",
        ):
            PublicDemoFixture.model_validate(payload)

    def test_snapshot_bundle_maps_synthetic_fixture(self) -> None:
        bundle = build_public_demo_snapshot_bundle()
        snapshot = bundle.primary_snapshot
        slots = {slot.slot: slot for slot in snapshot.slots}

        self.assertEqual(snapshot.selected_system_id, PUBLIC_DEMO_SYSTEM_ID)
        self.assertEqual(snapshot.selected_enclosure_id, PUBLIC_DEMO_ENCLOSURE_ID)
        self.assertEqual(snapshot.selected_profile.face_style, "top-loader")
        self.assertEqual(snapshot.layout_slot_count, 60)
        self.assertEqual(slots[12].state.value, "empty")
        self.assertEqual(slots[57].model, "Demo Flash SSD 4TB")
        self.assertEqual(slots[57].serial, "DEMO-SN-CORE-0057")
        self.assertEqual(slots[57].vdev_name, "mirror-8")
        self.assertEqual(bundle.smart_summary_cache["57"]["temperature_c"], 30)

        boot = next(view for view in bundle.storage_view_runtime.views if view.id == "boot-doms")
        nvme = next(view for view in bundle.storage_view_runtime.views if view.id == "nvme-carrier-x4")
        self.assertEqual([slot.slot_label for slot in boot.slots], ["DOM-A", "DOM-B"])
        self.assertEqual(nvme.slot_layout, [[3], [2], [1], [0]])
        self.assertEqual([slot.slot_label for slot in nvme.slots], ["M2-1", "M2-2", "M2-3", "M2-4"])
        self.assertEqual(nvme.slots[0].model, "Demo NVMe Flash 2TB")

    async def test_public_demo_embeds_all_declared_images_for_strict_source_parity(self) -> None:
        from scripts.public_demo_source_parity import inline_source_errors

        html = await build_public_demo_html()

        self.assertEqual(inline_source_errors(html, ROOT), [])

    async def test_public_demo_html_is_deterministic_and_self_contained(self) -> None:
        first = await build_public_demo_html()
        clear_export_caches()
        second = await build_public_demo_html()

        self.assertEqual(first, second)
        self.assertEqual(hashlib.sha256(first.encode()).hexdigest(), hashlib.sha256(second.encode()).hexdigest())
        self.assertIn("Synthetic IDs", first)
        self.assertIn('"history_window_hours": 168', first)
        self.assertIn("Demo 4x NVMe Carrier", first)
        self.assertNotIn('src="/static/app.js"', first)
        self.assertNotIn('href="/static/style.css"', first)
        self.assertIn("data:image/png;base64", first)
        self.assertNotIn("history/history.db", first)

    def test_build_script_writes_and_checks_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "index.html"
            build = subprocess.run(
                [sys.executable, "scripts/build_public_demo.py", "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            check = subprocess.run(
                [sys.executable, "scripts/build_public_demo.py", "--output", str(output), "--check"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertIn("deterministic synthetic", build.stdout)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("artifact is current", check.stdout)

    def test_build_script_help_documents_clean_source_input(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/build_public_demo.py", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("checked-in synthetic fixture", result.stdout)
        self.assertIn("uses no config, history database", result.stdout)


if __name__ == "__main__":
    unittest.main()
