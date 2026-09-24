from __future__ import annotations

import hashlib
import json
import os
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
    build_public_demo_snapshot_bundle,
    load_public_demo_fixture,
)
from app.services.snapshot_export import EXPORT_HISTORY_CACHE, EXPORT_RENDER_CACHE, EXPORT_ZIP_CACHE


ROOT = Path(__file__).resolve().parents[1]


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
            "Frozen Sanitized Snapshot",
            f"Artifact app v{__version__}",
            PUBLIC_DEMO_GENERATED_AT.isoformat(),
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
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            artifact = self.copy_artifact(demo_dir)
            artifact.write_text(
                artifact.read_text(encoding="utf-8").replace(
                    f"Artifact app v{__version__}",
                    "Artifact app v0.0.0-stale",
                ),
                encoding="utf-8",
            )
            result = run_checker(demo_dir)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"does not match source {__version__}", result.stderr)

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
