from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from app import __version__
from app.services.public_demo_fixture import (
    PUBLIC_DEMO_GENERATED_AT,
    PUBLIC_DEMO_HISTORY_WINDOW_HOURS,
    build_public_demo_html,
    build_public_demo_snapshot_bundle,
)
from app.services.snapshot_export import (
    EXPORT_HISTORY_CACHE,
    EXPORT_RENDER_CACHE,
    EXPORT_ZIP_CACHE,
)
from scripts.public_demo_source_parity import (
    SOURCE_INPUT_PATHS,
    SOURCE_PARITY_PREFIX,
    check_source_parity_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
LOCAL_HISTORY_ENV = "PUBLIC_DEMO_LOCAL_HISTORY"
LOCAL_HISTORY_DB = ROOT / "history" / "history.db"
LOCAL_HISTORY_SKIP_REASON = (
    f"requires {LOCAL_HISTORY_ENV}=1 and local ignored history/history.db release input"
)


def clear_export_caches() -> None:
    EXPORT_HISTORY_CACHE.clear()
    EXPORT_RENDER_CACHE.clear()
    EXPORT_ZIP_CACHE.clear()


class PublicDemoArtifactTests(unittest.TestCase):
    def _copy_checked_demo_artifact(self, demo_dir: Path, *, padding: str = "") -> None:
        demo_dir.mkdir(parents=True, exist_ok=True)
        artifact_html = (ROOT / "public-demo" / "index.html").read_text(encoding="utf-8")
        (demo_dir / "index.html").write_text(artifact_html + padding, encoding="utf-8")
        (demo_dir / ".nojekyll").write_text("", encoding="utf-8")

    def _assert_source_change_rejects_checked_artifact(self, source_path: Path) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source_root = temp_root / "source"
            demo_dir = temp_root / "public-demo"
            demo_dir.mkdir()
            shutil.copy2(ROOT / "public-demo" / "index.html", demo_dir / "index.html")
            (demo_dir / ".nojekyll").write_text("", encoding="utf-8")
            source_paths = tuple(dict.fromkeys((*SOURCE_INPUT_PATHS, source_path)))
            for relative_path in source_paths:
                target = source_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative_path, target)

            changed_source = source_root / source_path
            changed_source.write_bytes(changed_source.read_bytes() + b"\nsource-parity-test-change\n")

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_public_demo_artifact.py",
                    str(demo_dir),
                    "--source-root",
                    str(source_root),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            artifact_html = (demo_dir / "index.html").read_text(encoding="utf-8")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"source fingerprint mismatch: {source_path.as_posix()}", result.stderr)
        self.assertIn(f"Artifact app v{__version__}", artifact_html)

    def test_checked_in_public_demo_artifact_is_publishable(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_public_demo_artifact.py", "public-demo"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Public demo artifact is publishable", result.stdout)

    def test_checked_in_public_demo_artifact_reports_raw_and_gzip_sizes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_public_demo_artifact.py", "public-demo"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("raw=", result.stdout)
        self.assertIn("gzip=", result.stdout)

    def test_old_artifact_is_rejected_when_app_javascript_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(Path("app/static/app.js"))

    def test_old_artifact_is_rejected_when_app_stylesheet_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(Path("app/static/style.css"))

    def test_old_artifact_is_rejected_when_base_template_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(Path("app/templates/base.html"))

    def test_old_artifact_is_rejected_when_index_template_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(Path("app/templates/index.html"))

    def test_generator_source_manifest_includes_fixture_builder_and_snapshot_renderer(self) -> None:
        source_paths = {path.as_posix() for path in SOURCE_INPUT_PATHS}

        self.assertIn("app/services/public_demo_fixture.py", source_paths)
        self.assertIn("app/services/snapshot_export.py", source_paths)

    def test_old_artifact_is_rejected_when_fixture_builder_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(
            Path("app/services/public_demo_fixture.py")
        )

    def test_old_artifact_is_rejected_when_snapshot_renderer_changes_without_version_bump(self) -> None:
        self._assert_source_change_rejects_checked_artifact(
            Path("app/services/snapshot_export.py")
        )

    def test_public_demo_without_source_parity_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            demo_dir.mkdir(parents=True)
            (demo_dir / "index.html").write_text(
                "\n".join(
                    (
                        "Frozen Sanitized Snapshot",
                        f"Artifact app v{__version__}",
                        "Capture time",
                        "Live-derived CORE 60-bay sample",
                        "Scrambled IDs",
                        "4x NVMe Carrier Card",
                        "Boot SATADOMs",
                        "mirror-8",
                    )
                ),
                encoding="utf-8",
            )
            (demo_dir / ".nojekyll").write_text("", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing public demo source parity manifest", result.stderr)

    def test_source_fingerprints_are_bound_to_embedded_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir)
            artifact_path = demo_dir / "index.html"
            manifest_line, artifact_html = artifact_path.read_text(encoding="utf-8").split("\n", 1)
            manifest = json.loads(manifest_line.removeprefix(SOURCE_PARITY_PREFIX).removesuffix(" -->"))
            artifact_html = artifact_html.replace(
                "Frozen Sanitized Snapshot",
                "Frozen Sanitized Snapshot ",
                1,
            )
            manifest["artifact_sha256"] = hashlib.sha256(artifact_html.encode("utf-8")).hexdigest()
            artifact_path.write_text(
                f"{SOURCE_PARITY_PREFIX}{json.dumps(manifest, sort_keys=True, separators=(',', ':'))} -->\n"
                f"{artifact_html}",
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source/output parity fingerprint mismatch", result.stderr)

    def test_public_demo_without_storage_fabric_route_action_is_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir)

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_demo_artifact_version_must_match_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir)
            artifact_path = demo_dir / "index.html"
            artifact_path.write_text(
                artifact_path.read_text(encoding="utf-8").replace(
                    f"Artifact app v{__version__}",
                    "Artifact app v0.0.0-stale",
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact app version 0.0.0-stale does not match source", result.stderr)
        self.assertIn(__version__, result.stderr)

    def test_public_demo_artifact_version_must_be_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir)
            artifact_path = demo_dir / "index.html"
            artifact_path.write_text(
                artifact_path.read_text(encoding="utf-8").replace(
                    f"Artifact app v{__version__}",
                    "Artifact app v",
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing parseable artifact app version", result.stderr)

    def test_public_demo_storage_fabric_route_action_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir)
            with (demo_dir / "index.html").open("a", encoding="utf-8") as artifact:
                artifact.write('\n<a id="sas-fabric-view-link" href="#sas-fabric-panel">Storage Fabric</a>\n')

            result = subprocess.run(
                [sys.executable, "scripts/check_public_demo_artifact.py", str(demo_dir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("snapshot Storage Fabric route action", result.stderr)

    def test_checked_in_public_demo_artifact_enforces_raw_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir, padding="x" * 128)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_public_demo_artifact.py",
                    str(demo_dir),
                    "--max-raw-bytes",
                    "64",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("raw size", result.stderr)
        self.assertIn("exceeds budget", result.stderr)

    def test_checked_in_public_demo_artifact_enforces_gzip_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            demo_dir = Path(temp_dir) / "public-demo"
            self._copy_checked_demo_artifact(demo_dir, padding="x" * 128)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_public_demo_artifact.py",
                    str(demo_dir),
                    "--max-gzip-bytes",
                    "16",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gzip size", result.stderr)
        self.assertIn("exceeds budget", result.stderr)

    def test_checked_in_public_demo_artifact_has_operator_markers(self) -> None:
        artifact_path = ROOT / "public-demo" / "index.html"
        self.assertTrue(artifact_path.exists(), f"missing checked-in artifact: {artifact_path}")
        html = artifact_path.read_text(encoding="utf-8")

        for marker in (
            "Frozen Sanitized Snapshot",
            f"Artifact app v{__version__}",
            "Capture time",
            PUBLIC_DEMO_GENERATED_AT.isoformat(),
            "Live-derived CORE 60-bay sample",
            "Scrambled IDs",
            "4x NVMe Carrier Card",
            "Boot SATADOMs",
            "mirror-8",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        self.assertNotIn('id="sas-fabric-view-link"', html)


class PublicDemoBuildScriptTests(unittest.TestCase):
    def test_publish_workflow_watches_source_parity_generators(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publish-public-demo.yml").read_text(encoding="utf-8")

        self.assertIn('- "scripts/build_public_demo.py"', workflow)
        self.assertIn('- "scripts/public_demo_source_parity.py"', workflow)

    def test_build_script_embeds_source_and_output_fingerprints(self) -> None:
        from scripts import build_public_demo as build_script

        checked_artifact = (ROOT / "public-demo" / "index.html").read_text(encoding="utf-8")
        generated_html = checked_artifact.split("\n", 1)[1]
        async_build = mock.AsyncMock(return_value=generated_html)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "index.html"
            args = argparse.Namespace(output=output_path, check=False)
            with (
                mock.patch.object(build_script, "build_public_demo_html", new=async_build),
                mock.patch.object(build_script, "parse_args", return_value=args),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = asyncio.run(build_script.run())
            artifact_html = output_path.read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertTrue(artifact_html.startswith(SOURCE_PARITY_PREFIX))
        self.assertEqual(check_source_parity_manifest(artifact_html, source_root=ROOT), [])

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
            self.assertNotIn("history/history.db", first_html)

    def test_build_script_help_marks_generation_as_local_history_path(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/build_public_demo.py", "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local ignored history/history.db", result.stdout)

    def test_build_script_reports_local_history_errors_without_traceback(self) -> None:
        from scripts import build_public_demo as build_script

        async_build = mock.AsyncMock(
            side_effect=RuntimeError(
                "Public demo release generation requires local ignored history/history.db."
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(output=Path(temp_dir) / "index.html", check=False)
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                mock.patch.object(build_script, "build_public_demo_html", new=async_build),
                mock.patch.object(build_script, "parse_args", return_value=args),
                contextlib.redirect_stderr(stderr),
                contextlib.redirect_stdout(stdout),
            ):
                result = asyncio.run(build_script.run())

        self.assertEqual(result, 1)
        self.assertIn("history/history.db", stderr.getvalue())
        self.assertIn("Clean CI validates", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_build_script_reports_source_parity_errors_without_traceback(self) -> None:
        from scripts import build_public_demo as build_script

        async_build = mock.AsyncMock(return_value="<!DOCTYPE html><html></html>\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(output=Path(temp_dir) / "index.html", check=False)
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                mock.patch.object(build_script, "build_public_demo_html", new=async_build),
                mock.patch.object(build_script, "parse_args", return_value=args),
                contextlib.redirect_stderr(stderr),
                contextlib.redirect_stdout(stdout),
            ):
                result = asyncio.run(build_script.run())

        self.assertEqual(result, 1)
        self.assertIn("source parity", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


@unittest.skipUnless(os.environ.get(LOCAL_HISTORY_ENV) == "1", LOCAL_HISTORY_SKIP_REASON)
class PublicDemoFixtureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        if not LOCAL_HISTORY_DB.exists():
            self.fail(
                f"{LOCAL_HISTORY_ENV}=1 but local ignored release input is missing: "
                f"{LOCAL_HISTORY_DB.relative_to(ROOT)}"
            )
        clear_export_caches()

    async def test_public_demo_html_is_deterministic(self) -> None:
        first_html = await build_public_demo_html()
        clear_export_caches()
        second_html = await build_public_demo_html()

        self.assertEqual(first_html, second_html)
        self.assertEqual(
            hashlib.sha256(first_html.encode("utf-8")).hexdigest(),
            hashlib.sha256(second_html.encode("utf-8")).hexdigest(),
        )
        self.assertIn(PUBLIC_DEMO_GENERATED_AT.isoformat(), first_html)
        self.assertIn("TN Core", first_html)
        self.assertIn("Supermicro CSE-946", first_html)
        self.assertIn("WDC WUH721818AL5204", first_html)
        self.assertIn("SAMSUNG MZILT3T8HALS/007", first_html)
        self.assertIn("4x NVMe Carrier Card", first_html)
        self.assertIn("Samsung SSD 970 EVO 2TB", first_html)
        self.assertIn("Scrambled IDs", first_html)
        self.assertIn('"history_window_hours": 168', first_html)
        self.assertIn("initialSelectedSlot: null", first_html)
        self.assertEqual(PUBLIC_DEMO_HISTORY_WINDOW_HOURS, 168)
        self.assertIn("preloadedSnapshotsByEnclosure", first_html)
        self.assertIn("preloadedStorageViewSmartSummaries", first_html)
        self.assertIn("Frozen Sanitized Snapshot", first_html)
        self.assertIn("Artifact app v", first_html)
        self.assertIn("Capture time", first_html)
        self.assertNotIn('id="sas-fabric-view-link"', first_html)
        self.assertNotIn('src="/static/app.js"', first_html)
        self.assertNotIn('href="/static/style.css"', first_html)
        self.assertNotIn("/static/images/hyper-m2-gen3-card.png", first_html)
        self.assertIn("data:image/png;base64", first_html)

    async def test_public_demo_html_omits_real_fixture_identifiers(self) -> None:
        html = await build_public_demo_html()
        forbidden_values = [
            "Archive CORE",
            "Offsite SCALE",
            "QSOSN",
            "ABC123456",
            "SATADOM123456",
            "REAR123456",
            "S464NB0K900412E",
            "PHKM8522005N200E",
            "SMC0515D93717D7B1810",
            "500304801f715f3f",
            "500304801f5a003f",
            "5000c500c2a7f220",
            "500304801f5a00bf",
            "10.13.",
            "192.168.",
            "BEGIN OPENSSH",
        ]

        for value in forbidden_values:
            with self.subTest(value=value):
                self.assertNotIn(value, html)

    def test_fixture_uses_core_top_loader_with_stable_scrambled_ids(self) -> None:
        bundle = build_public_demo_snapshot_bundle()

        snapshot = bundle.primary_snapshot
        self.assertEqual(snapshot.selected_system_label, "TN Core")
        self.assertEqual(snapshot.selected_profile.face_style, "top-loader")
        self.assertEqual(snapshot.layout_slot_count, 60)
        self.assertEqual(set(bundle.live_enclosure_snapshots), {"tn-core-cse-946-top-loader"})
        self.assertEqual(
            {view.id for view in bundle.storage_view_runtime.views},
            {"boot-doms", "nvme-carrier-x4"},
        )
        slots = {slot.slot: slot for slot in snapshot.slots}
        expected_empty_slots = {12, 13, 14, 27, 28, 29, 44, 45, 46, 47, 48, 49, 50}
        for slot_number in expected_empty_slots:
            with self.subTest(slot=slot_number, expectation="empty"):
                self.assertTrue(slots[slot_number].present)
                self.assertEqual(slots[slot_number].state.value, "empty")

        expected_vdevs = {
            "raidz2-0": (0, 1, 2, 3, 4, 5),
            "raidz2-1": (15, 16, 17, 18, 19, 20),
            "raidz2-2": (30, 31, 32, 33, 34, 35),
            "raidz2-3": (6, 7, 8, 9, 10, 11),
            "raidz2-4": (21, 22, 23, 24, 25, 26),
            "raidz2-5": (36, 37, 38, 39, 40, 41),
            "raidz2-6": (51, 52, 53, 54, 55, 56),
        }
        for vdev_name, slot_numbers in expected_vdevs.items():
            for slot_number in slot_numbers:
                with self.subTest(slot=slot_number, vdev=vdev_name):
                    self.assertTrue(slots[slot_number].present)
                    self.assertEqual(slots[slot_number].pool_name, "The-Repository")
                    self.assertEqual(slots[slot_number].vdev_name, vdev_name)
                    self.assertEqual(slots[slot_number].vdev_class, "data")

        self.assertEqual(slots[42].pool_name, "The-Repository")
        self.assertEqual(slots[42].vdev_name, "spares")
        self.assertEqual(slots[42].vdev_class, "spare")
        self.assertIsNone(slots[43].pool_name)
        self.assertIsNone(slots[43].vdev_name)
        self.assertIn("OK", slots[43].health or "")
        for slot_number in (57, 58, 59):
            with self.subTest(slot=slot_number, vdev="mirror-8"):
                self.assertEqual(slots[slot_number].model, "SAMSUNG MZILT3T8HALS/007")
                self.assertEqual(slots[slot_number].vdev_name, "mirror-8")
                self.assertEqual(slots[slot_number].vdev_class, "special")

        slot_57 = next(slot for slot in snapshot.slots if slot.slot == 57)
        self.assertEqual(slot_57.model, "SAMSUNG MZILT3T8HALS/007")
        self.assertEqual(slot_57.serial, "DEMO-SN-CORE-0057")
        self.assertEqual(bundle.smart_summary_cache["57"]["serial_number"], slot_57.serial)
        self.assertEqual(bundle.smart_summary_cache["57"]["temperature_c"], 32)
        nvme_view = next(view for view in bundle.storage_view_runtime.views if view.id == "nvme-carrier-x4")
        self.assertEqual(nvme_view.label, "4x NVMe Carrier Card")
        self.assertEqual(nvme_view.slot_layout, [[3], [2], [1], [0]])
        self.assertEqual([slot.slot_label for slot in nvme_view.slots], ["M2-1", "M2-2", "M2-3", "M2-4"])
        self.assertEqual(nvme_view.slots[0].model, "Samsung SSD 970 EVO 2TB")
        self.assertEqual(nvme_view.slots[0].serial, "DEMO-SN-NVME-0000")
        boot_view = next(view for view in bundle.storage_view_runtime.views if view.id == "boot-doms")
        self.assertEqual(boot_view.label, "Boot SATADOMs")
        self.assertEqual([slot.slot_label for slot in boot_view.slots], ["DOM-A", "DOM-B"])
        self.assertEqual(boot_view.slots[0].model, "SuperMicro SSD")
        self.assertIn("tn-core-cse-946-top-loader", bundle.live_enclosure_smart_summary_cache)
        self.assertIn("boot-doms", bundle.storage_view_smart_summary_cache)
        self.assertIn("nvme-carrier-x4", bundle.storage_view_smart_summary_cache)

    def test_build_script_writes_and_checks_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "index.html"
            build_result = subprocess.run(
                [sys.executable, "scripts/build_public_demo.py", "--output", str(output_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(build_result.returncode, 0, build_result.stderr)
            self.assertIn("Built public demo artifact", build_result.stdout)
            self.assertIn("TN Core", output_path.read_text(encoding="utf-8"))

            check_result = subprocess.run(
                [
                    sys.executable,
                    "scripts/build_public_demo.py",
                    "--output",
                    str(output_path),
                    "--check",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(check_result.returncode, 0, check_result.stderr)
            self.assertIn("Public demo artifact is current", check_result.stdout)
