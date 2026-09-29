from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCS_IMAGE_ROOT = ROOT / "docs/images/screenshots"
WIKI_IMAGE_ROOT = ROOT / "wiki/images"
MANIFEST_PATH = DOCS_IMAGE_ROOT / "manifest.json"
EXPECTED_NAMES = {
    "public-demo-overview.png",
    "public-demo-history.png",
}
EXPECTED_WIDTHS = {
    "public-demo-history.png": 1920,
    "public-demo-overview.png": 1920,
}


SYNTHETIC_REVISION = "0" * 39 + "1"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def synthetic_png(width: int, height: int, filler: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(filler))
        + png_chunk(b"IEND", b"")
    )


def png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", payload[16:24])


class PublicScreenshotContractTests(unittest.TestCase):
    def test_only_current_fixture_screenshots_are_tracked(self) -> None:
        docs_names = {path.name for path in DOCS_IMAGE_ROOT.glob("*.png")}
        wiki_names = {path.name for path in WIKI_IMAGE_ROOT.glob("*.png")}

        self.assertEqual(docs_names, EXPECTED_NAMES)
        self.assertEqual(wiki_names, EXPECTED_NAMES)

    def test_manifest_binds_exact_image_bytes_and_demo_source(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        artifact = ROOT / "public-demo/index.html"

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["provenance"], "synthetic-public-demo")
        self.assertRegex(manifest["source_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            manifest["source_artifact_sha256"],
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )
        self.assertEqual(set(manifest["images"]), EXPECTED_NAMES)
        for name, record in manifest["images"].items():
            docs_path = DOCS_IMAGE_ROOT / name
            wiki_path = WIKI_IMAGE_ROOT / name
            self.assertEqual(docs_path.read_bytes(), wiki_path.read_bytes())
            self.assertEqual(record["sha256"], hashlib.sha256(docs_path.read_bytes()).hexdigest())
            self.assertEqual(record["bytes"], docs_path.stat().st_size)
            self.assertEqual([*png_size(docs_path)], record["dimensions"])
            self.assertEqual(record["dimensions"][0], EXPECTED_WIDTHS[name])
            self.assertEqual(record["pixel_review"], "PASS")

    def test_exact_byte_screenshot_checker_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/check_public_screenshots.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("4 image copies", result.stdout)
        self.assertIn("exact-byte", result.stdout)

    def test_report_mode_describes_candidate_bytes_without_asserting_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            docs_root = root / "docs/images/screenshots"
            docs_root.mkdir(parents=True)
            (root / "public-demo").mkdir()
            (root / "public-demo/index.html").write_bytes(
                b'<!-- public-demo-source-parity {"source_revision": "'
                + SYNTHETIC_REVISION.encode("ascii")
                + b'"} -->\n<html></html>\n'
            )
            payloads = {
                "public-demo-overview.png": synthetic_png(1920, 4104, b"overview"),
                "public-demo-history.png": synthetic_png(1920, 4860, b"history"),
            }
            for name, payload in payloads.items():
                (docs_root / name).write_bytes(payload)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_public_screenshots.py",
                    "--root",
                    str(root),
                    "--report",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["provenance"], "synthetic-public-demo")
            self.assertEqual(report["source_revision"], SYNTHETIC_REVISION)
            self.assertEqual(
                report["source_artifact_sha256"],
                hashlib.sha256((root / "public-demo/index.html").read_bytes()).hexdigest(),
            )
            self.assertEqual(set(report["images"]), EXPECTED_NAMES)
            for name, payload in payloads.items():
                with self.subTest(name=name):
                    record = report["images"][name]
                    self.assertEqual(record["pixel_review"], "PENDING")
                    self.assertEqual(record["bytes"], len(payload))
                    self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())
                    self.assertEqual(record["dimensions"], [*png_size(docs_root / name)])
            # No manifest exists in the temporary root, so report mode cannot be
            # asserting one.
            self.assertFalse((docs_root / "manifest.json").exists())

    def test_report_mode_leaves_the_checked_in_manifest_and_images_untouched(self) -> None:
        before = {
            path: path.read_bytes()
            for path in (MANIFEST_PATH, *sorted(DOCS_IMAGE_ROOT.glob("*.png")))
        }

        result = subprocess.run(
            [sys.executable, "scripts/check_public_screenshots.py", "--report"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        for record in report["images"].values():
            self.assertEqual(record["pixel_review"], "PENDING")
        for path, payload in before.items():
            with self.subTest(path=path.name):
                self.assertEqual(path.read_bytes(), payload)

    def test_pixel_review_record_names_every_exact_image_hash(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        review = (ROOT / "docs/PUBLIC_SCREENSHOT_REVIEW.md").read_text(encoding="utf-8")

        self.assertIn(manifest["source_revision"], review)
        self.assertIn(manifest["source_artifact_sha256"], review)
        for name, record in manifest["images"].items():
            with self.subTest(name=name):
                self.assertIn(name, review)
                self.assertIn(record["sha256"], review)
        self.assertIn("Final pixel verdict: `PASS`", review)

    def test_legacy_live_capture_scripts_are_removed(self) -> None:
        legacy = {
            "capture_readme_screenshots.py",
            "capture_history_export_screenshots.py",
            "capture_release_workflow_screenshots.py",
            "capture_visual_tour_screenshots.py",
        }

        self.assertFalse(any((ROOT / "scripts" / name).exists() for name in legacy))
        capture_script = (ROOT / "scripts/capture_public_demo_screenshots.js").read_text(encoding="utf-8")
        self.assertIn("public-demo/index.html", capture_script)
        self.assertIn('timezoneId: "UTC"', capture_script)
        self.assertIn('require("./screenshot_capture_state.js")', capture_script)
        self.assertLess(
            capture_script.index("await normalizeCaptureState(page)"),
            capture_script.index("await page.screenshot"),
        )
        self.assertNotIn("public-demo-mobile.png", capture_script)
        self.assertNotIn("http://localhost:8080", capture_script)
        self.assertNotIn("system_id", capture_script)
        self.assertNotIn("enclosure_id", capture_script)

    def test_current_docs_reference_fixture_screenshots_only(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        visual_tour = (ROOT / "wiki/Visual-Tour.md").read_text(encoding="utf-8")

        for name in EXPECTED_NAMES:
            self.assertIn(name, readme + visual_tour)
        self.assertNotIn("v0.18.0.png", readme)
        self.assertNotIn("v0.18.0.png", visual_tour)
        self.assertNotIn("public-demo-mobile.png", readme)
        self.assertNotIn("public-demo-mobile.png", visual_tour)

    def test_desktop_only_policy_stays_out_of_human_facing_docs(self) -> None:
        product_brief = (ROOT / "docs/PUBLIC_DEMO_PRODUCT_BRIEF.md").read_text(
            encoding="utf-8"
        )
        contributor_rails = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        artifact_readme = (ROOT / "public-demo/README.md").read_text(encoding="utf-8")
        visual_tour = (ROOT / "wiki/Visual-Tour.md").read_text(encoding="utf-8")
        public_demo_guide = (ROOT / "wiki/Public-Demo-Site.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Mobile and tablet layouts are unsupported", product_brief)
        self.assertIn("mobile and tablet layouts are", contributor_rails)

        for document in (
            readme,
            artifact_readme,
            visual_tour,
            public_demo_guide,
        ):
            self.assertNotIn(
                "Mobile and tablet layouts are unsupported",
                " ".join(document.split()),
            )

    def test_maintainer_publish_guide_uses_the_desktop_screenshot_count(self) -> None:
        publishing_guide = (ROOT / "docs/PUBLISHING_THE_WIKI.md").read_text(
            encoding="utf-8"
        )
        normalized = " ".join(publishing_guide.split())

        self.assertIn("those two review fields", normalized)
        self.assertNotIn("those three review fields", normalized)


if __name__ == "__main__":
    unittest.main()
