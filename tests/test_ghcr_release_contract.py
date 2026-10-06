from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/publish-ghcr.yml"
RUNBOOK_PATH = REPO_ROOT / "docs/IMMUTABLE_GHCR_DEPLOYMENT.md"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
RELEASE_CHECKLIST_PATH = REPO_ROOT / "docs/RELEASE_CHECKLIST.md"
DEPLOY_HELPER_PATH = REPO_ROOT / "scripts/update_immutable_deployment.py"


def _run_release_step(name: str, tag: str, prerelease: bool) -> tuple[int, str, list[str], bool]:
    """Run one publish-workflow step's own bash for a ``release`` event.

    Returns the exit code, output, resolved image tags and whether the step
    called the public-demo validator (a stub ``python`` records the call).
    """

    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == name)
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        stub = root / "bin" / "python"
        stub.parent.mkdir()
        stub.write_text(f'#!/bin/sh\necho "$@" > "{root}/validator-called"\n', encoding="utf-8")
        stub.chmod(0o755)
        output = root / "github-output"
        env = {
            "PATH": f"{stub.parent}:{os.environ['PATH']}",
            "GITHUB_EVENT_NAME": "release",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "gcs8/truenas-jbod-ui",
            "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
            "IMAGE_NAME": "ghcr.io/gcs8/truenas-jbod-ui",
            "INPUT_TAGS": "",
            "INPUT_LATEST": "false",
            "RELEASE_TAG": tag,
            "PRERELEASE": "true" if prerelease else "false",
        }
        result = subprocess.run(
            [shutil.which("bash") or "/bin/bash", "-c", step["run"]],
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )
        text = output.read_text(encoding="utf-8") if output.exists() else ""
        tags = re.search(r"tags<<EOF\n(.*?)EOF\n", text, re.S)
        return (
            result.returncode,
            result.stdout + result.stderr,
            tags.group(1).split() if tags else [],
            (root / "validator-called").exists(),
        )


class GHCRReleaseContractTests(unittest.TestCase):
    def test_stable_release_moves_latest_and_never_dev(self) -> None:
        code, _out, tags, _ = _run_release_step("Resolve publish tags", "v0.24.0", prerelease=False)

        self.assertEqual(code, 0)
        self.assertEqual(
            tags,
            [f"ghcr.io/gcs8/truenas-jbod-ui:{tag}" for tag in ("v0.24.0", "0.24.0", "latest")],
        )

    def test_prerelease_moves_dev_and_never_latest(self) -> None:
        code, _out, tags, _ = _run_release_step("Resolve publish tags", "v0.24.0-beta.1", prerelease=True)

        self.assertEqual(code, 0)
        self.assertEqual(
            tags,
            [f"ghcr.io/gcs8/truenas-jbod-ui:{tag}" for tag in ("v0.24.0-beta.1", "0.24.0-beta.1", "dev")],
        )

    def test_demo_gate_runs_for_stable_and_waits_for_a_prerelease(self) -> None:
        gate = "Require a public demo rebuilt for this release"

        code, _out, _tags, called = _run_release_step(gate, "v0.24.0", prerelease=False)
        self.assertEqual((code, called), (0, True))

        code, out, _tags, called = _run_release_step(gate, "v0.24.0-beta.1", prerelease=True)
        self.assertEqual((code, called), (0, False))
        self.assertIn("publishes to dev", out)

    def test_demo_gate_refuses_a_tag_that_disagrees_with_the_prerelease_flag(self) -> None:
        gate = "Require a public demo rebuilt for this release"

        for tag, prerelease in (("v0.24.0-beta.1", False), ("v0.24.0", True), ("v0.24.0+build-1", True)):
            with self.subTest(tag=tag, prerelease=prerelease):
                code, out, _tags, called = _run_release_step(gate, tag, prerelease)
                self.assertNotEqual(code, 0)
                self.assertFalse(called)
                self.assertIn("is marked prerelease=", out)

    def test_publish_workflow_records_the_pushed_manifest_digest(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("id: build", workflow)
        self.assertIn("steps.build.outputs.digest", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("Immutable manifest digest", workflow)
        self.assertRegex(workflow, r"sha256:\[0-9a-f\]\{64\}")

    def test_release_publish_refuses_a_public_demo_not_rebuilt_for_the_release(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        gate = workflow.index("Require a public demo rebuilt for this release")
        push = workflow.index("Build and push image")

        # The gate runs on every release event, before anything is pushed.
        self.assertLess(gate, push)
        gate_step = workflow[gate:push]
        self.assertIn("if: github.event_name == 'release'", gate_step)
        self.assertIn('python scripts/validate_release_wrap.py "$RELEASE_TAG" --public-demo-only', gate_step)
        self.assertIn("RELEASE_TAG: ${{ github.event.release.tag_name }}", gate_step)
        self.assertNotIn("continue-on-error", gate_step)
        self.assertIn("fetch-depth: 0", workflow[: gate])

    def test_runbook_distinguishes_mutable_tags_from_immutable_digests(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("Every registry tag is a mutable pointer", runbook)
        self.assertIn("Only a digest reference is immutable", runbook)
        self.assertRegex(
            runbook,
            re.compile(r"JBOD_UI_IMAGE=ghcr\.io/gcs8/truenas-jbod-ui@sha256:<64-hex-digest>"),
        )
        self.assertNotIn("you want an exact GitHub release tag", runbook)

    def test_runbook_requires_digest_evidence_activation_and_rollback(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        helper = DEPLOY_HELPER_PATH.read_text(encoding="utf-8")

        for required_text in (
            "Verify Runtime Convergence",
            "Rollback To The Previous Digest",
            "scripts/update_immutable_deployment.py update",
            "scripts/update_immutable_deployment.py verify",
            "scripts/update_immutable_deployment.py rollback",
            ".jbod-ui-image-update",
        ):
            self.assertIn(required_text, runbook)
        self.assertNotIn("image-update-receipt.env", runbook)
        self.assertNotIn(". ./", runbook)
        self.assertIn("candidate_digest != spec.expected_image", helper)
        self.assertIn("_capture_previous_runtime", helper)
        self.assertIn("_verify_runtime", helper)
        self.assertIn("_restore_previous", helper)

    def test_runbook_separates_image_only_from_pinned_compose_replacement(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        helper = DEPLOY_HELPER_PATH.read_text(encoding="utf-8")

        self.assertIn("release_revision='REPLACE_WITH_40_HEX_SOURCE_REVISION'", runbook)
        self.assertNotIn("release_revision=<", runbook)
        self.assertIn(
            "raw.githubusercontent.com/gcs8/truenas-jbod-ui/$release_revision/scripts/update_immutable_deployment.py",
            runbook,
        )
        self.assertIn("--compose docker-compose.yml=compose.yaml", runbook)
        self.assertIn("--project-name truenas-jbod-ui", runbook)
        self.assertIn("Live Compose bytes are preserved", runbook)
        self.assertIn("Only explicit `--replace-compose`", runbook)
        self.assertIn("{spec.source_revision}/{item.source}", helper)

    def test_environment_example_recommends_digest_for_controlled_deployments(self) -> None:
        env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

        self.assertIn("latest remains the compatibility default", env_example)
        self.assertIn("name@sha256", env_example)

    def test_release_checklist_requires_immutable_deployment_evidence(self) -> None:
        checklist = RELEASE_CHECKLIST_PATH.read_text(encoding="utf-8")

        for required_text in (
            "full `name@sha256` image reference",
            "exact source revision",
            "pre-update rollback digest",
            "running container image IDs",
            "rollback result",
        ):
            self.assertIn(required_text, checklist)


if __name__ == "__main__":
    unittest.main()
