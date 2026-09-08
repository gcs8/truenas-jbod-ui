from __future__ import annotations

import re
import subprocess
from pathlib import Path
import tempfile
import unittest

from scripts.public_demo_inputs import PUBLIC_DEMO_INPUT_PATHS
from scripts.public_demo_source_parity import (
    add_source_parity_manifest,
    parse_manifest,
    recorded_source_revision_errors,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = subprocess.check_output(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    text=True,
).strip()


class PublicDemoProvenanceTests(unittest.TestCase):
    def artifact_html(self) -> str:
        checked = (ROOT / "public-demo/index.html").read_text(encoding="utf-8")
        _manifest, artifact_html, errors = parse_manifest(checked)
        self.assertEqual(errors, [])
        return artifact_html

    def test_manifest_records_visible_source_revision_and_separate_build_id(self) -> None:
        rendered = add_source_parity_manifest(
            self.artifact_html(),
            source_root=ROOT,
            source_revision=SOURCE_REVISION,
        )
        manifest, artifact_html, errors = parse_manifest(rendered)

        self.assertEqual(errors, [])
        self.assertEqual(manifest["source_revision"], SOURCE_REVISION)
        self.assertRegex(manifest["build_id"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(manifest["build_id"], SOURCE_REVISION)
        self.assertIn("Source revision", artifact_html)
        self.assertIn(SOURCE_REVISION, artifact_html)
        self.assertIn("Build ID", artifact_html)
        self.assertIn(manifest["build_id"], artifact_html)

    def test_build_id_is_deterministic_and_revision_bound(self) -> None:
        first = add_source_parity_manifest(
            self.artifact_html(),
            source_root=ROOT,
            source_revision=SOURCE_REVISION,
        )
        second = add_source_parity_manifest(
            self.artifact_html(),
            source_root=ROOT,
            source_revision=SOURCE_REVISION,
        )
        alternate = add_source_parity_manifest(
            self.artifact_html(),
            source_root=ROOT,
            source_revision="f" * 40,
        )

        first_manifest, _html, _errors = parse_manifest(first)
        second_manifest, _html, _errors = parse_manifest(second)
        alternate_manifest, _html, _errors = parse_manifest(alternate)
        self.assertEqual(first_manifest["build_id"], second_manifest["build_id"])
        self.assertNotEqual(first_manifest["build_id"], alternate_manifest["build_id"])

    def test_invalid_source_revision_is_rejected(self) -> None:
        for revision in ("", "abc123", "G" * 40, "0" * 39, "0" * 41):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(ValueError, "source revision"):
                    add_source_parity_manifest(
                        self.artifact_html(),
                        source_root=ROOT,
                        source_revision=revision,
                    )

    def test_checked_artifact_displays_full_unambiguous_identities(self) -> None:
        checked = (ROOT / "public-demo/index.html").read_text(encoding="utf-8")
        manifest, artifact_html, errors = parse_manifest(checked)

        self.assertEqual(errors, [])
        self.assertRegex(manifest.get("source_revision", ""), r"^[0-9a-f]{40}$")
        self.assertRegex(manifest.get("build_id", ""), r"^[0-9a-f]{64}$")
        self.assertEqual(artifact_html.count(manifest["source_revision"]), 1)
        self.assertEqual(artifact_html.count(manifest["build_id"]), 1)
        self.assertEqual(len(re.findall(r"Source revision", artifact_html)), 1)
        self.assertEqual(len(re.findall(r"Build ID", artifact_html)), 1)

    def test_recorded_source_revision_rejects_uncommitted_and_later_input_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=repository, check=True)
            for relative_path in PUBLIC_DEMO_INPUT_PATHS:
                target = repository / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"fixture:{relative_path.as_posix()}\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture source"], cwd=repository, check=True)
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
            self.assertEqual(
                recorded_source_revision_errors(source_root=repository, source_revision=revision),
                [],
            )

            changed = repository / PUBLIC_DEMO_INPUT_PATHS[0]
            changed.write_text("changed\n", encoding="utf-8")
            self.assertEqual(
                recorded_source_revision_errors(source_root=repository, source_revision=revision),
                ["declared public demo inputs have uncommitted changes"],
            )
            subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "changed input"], cwd=repository, check=True)
            self.assertEqual(
                recorded_source_revision_errors(source_root=repository, source_revision=revision),
                ["declared public demo inputs changed after the recorded source revision"],
            )


if __name__ == "__main__":
    unittest.main()
