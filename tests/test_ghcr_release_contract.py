from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/publish-ghcr.yml"
RUNBOOK_PATH = REPO_ROOT / "wiki/Docker-and-GHCR-Deployment.md"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
RELEASE_CHECKLIST_PATH = REPO_ROOT / "docs/RELEASE_CHECKLIST.md"
DEPLOY_HELPER_PATH = REPO_ROOT / "scripts/update_immutable_deployment.py"


class GHCRReleaseContractTests(unittest.TestCase):
    def test_publish_workflow_records_the_pushed_manifest_digest(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("id: build", workflow)
        self.assertIn("steps.build.outputs.digest", workflow)
        self.assertIn("GITHUB_STEP_SUMMARY", workflow)
        self.assertIn("Immutable manifest digest", workflow)
        self.assertRegex(workflow, r"sha256:\[0-9a-f\]\{64\}")

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

    def test_runbook_pins_compose_files_to_the_image_source_revision(self) -> None:
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
        self.assertIn("same exact 40-hex source", runbook)
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
