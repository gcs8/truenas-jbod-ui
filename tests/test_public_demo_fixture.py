from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

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

    def test_checked_in_public_demo_artifact_has_operator_markers(self) -> None:
        artifact_path = ROOT / "public-demo" / "index.html"
        self.assertTrue(artifact_path.exists(), f"missing checked-in artifact: {artifact_path}")
        html = artifact_path.read_text(encoding="utf-8")

        for marker in (
            "Frozen Offline Artifact",
            "Live-derived CORE 60-bay sample",
            "Scrambled IDs",
            "4x NVMe Carrier Card",
            "Boot SATADOMs",
            "mirror-8",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)


class PublicDemoBuildScriptTests(unittest.TestCase):
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
        self.assertIn("Frozen Offline Artifact", first_html)
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
