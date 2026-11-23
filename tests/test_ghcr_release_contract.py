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


def _run_release_step(
    name: str,
    tag: str,
    prerelease: bool,
    *,
    manual_ref: str | None = None,
    input_tags: str = "",
    make_latest: bool = False,
    fixture_files: tuple[str, ...] = (),
) -> tuple[int, str, list[str], bool]:
    """Run one publish-workflow step's own bash for a ``release`` event.

    Returns the exit code, output, resolved image tags and whether the step
    called the public-demo validator (a stub ``python`` records the call).
    With ``manual_ref`` the step runs as a ``workflow_dispatch`` from that ref.
    """

    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == name)
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for filename in fixture_files:
            (root / filename).touch()
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
        if manual_ref is not None:
            env.update(
                {
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REF": manual_ref,
                    "GITHUB_REF_NAME": manual_ref.split("/", 2)[2],
                    "INPUT_TAGS": input_tags,
                    "INPUT_LATEST": "true" if make_latest else "false",
                    "RELEASE_TAG": "",
                    "PRERELEASE": "",
                }
            )
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

    def _run_manual(self, ref: str, input_tags: str = "", make_latest: bool = False) -> tuple[int, str, list[str]]:
        code, out, tags, _ = _run_release_step(
            "Resolve publish tags", "", False,
            manual_ref=ref, input_tags=input_tags, make_latest=make_latest,
        )
        return code, out, [tag.removeprefix("ghcr.io/gcs8/truenas-jbod-ui:") for tag in tags]

    def test_manual_branch_run_publishes_only_its_branch_and_sha_tags(self) -> None:
        code, _out, tags = self._run_manual("refs/heads/fix/example-change")

        self.assertEqual(code, 0)
        self.assertEqual(tags, ["fix-example-change", "sha-0123456"])

        code, _out, tags = self._run_manual("refs/heads/fix/example-change", input_tags="qa-one, qa-two")

        self.assertEqual(code, 0)
        self.assertEqual(tags, ["qa-one", "qa-two"])

    def test_manual_run_moves_latest_only_from_main_or_a_stable_release_tag(self) -> None:
        for ref in ("refs/heads/main", "refs/tags/v0.24.0"):
            with self.subTest(ref=ref):
                code, _out, tags = self._run_manual(ref, make_latest=True)
                self.assertEqual(code, 0)
                self.assertEqual(tags[-1], "latest")

        refused = (
            ("refs/heads/fix/example-change", "", True),
            ("refs/heads/fix/example-change", "latest", False),
            ("refs/heads/latest", "", False),
            ("refs/tags/v0.24.0-beta.1", "", True),
            ("refs/heads/mainline", "", True),
        )
        for ref, input_tags, make_latest in refused:
            with self.subTest(ref=ref, input_tags=input_tags, make_latest=make_latest):
                code, out, tags = self._run_manual(ref, input_tags=input_tags, make_latest=make_latest)
                self.assertNotEqual(code, 0)
                self.assertEqual(tags, [])
                self.assertIn(f"Refusing to publish the reserved tag 'latest' from {ref}.", out)

    def test_manual_run_moves_dev_only_from_main_or_a_release_tag(self) -> None:
        for ref in ("refs/heads/main", "refs/tags/v0.24.0-beta.1", "refs/tags/v0.24.0"):
            with self.subTest(ref=ref):
                code, _out, tags = self._run_manual(ref, input_tags="dev")
                self.assertEqual((code, tags), (0, ["dev"]))

        for ref in ("refs/heads/fix/example-change", "refs/heads/dev"):
            with self.subTest(ref=ref):
                code, out, tags = self._run_manual(ref, input_tags="" if ref.endswith("/dev") else "dev")
                self.assertNotEqual(code, 0)
                self.assertEqual(tags, [])
                self.assertIn(f"Refusing to publish the reserved tag 'dev' from {ref}.", out)

    def test_manual_run_publishes_a_version_tag_only_from_its_own_git_tag(self) -> None:
        code, _out, tags = self._run_manual("refs/tags/v0.24.0", input_tags="v0.24.0,0.24.0")
        self.assertEqual((code, tags), (0, ["v0.24.0", "0.24.0"]))

        code, _out, tags = self._run_manual("refs/tags/v0.24.0-beta.1")
        self.assertEqual((code, tags), (0, ["v0.24.0-beta.1", "sha-0123456"]))

        refused = (
            ("refs/heads/main", "v0.23.0"),
            ("refs/heads/fix/example-change", "0.24.0"),
            ("refs/tags/v0.24.0", "v0.23.0"),
            ("refs/tags/v0.24.0", "qa-one,0.24.1"),
            ("refs/heads/main", "0.24"),
            ("refs/heads/v0.24.0", ""),
        )
        for ref, input_tags in refused:
            with self.subTest(ref=ref, input_tags=input_tags):
                code, out, tags = self._run_manual(ref, input_tags=input_tags)
                self.assertNotEqual(code, 0)
                self.assertEqual(tags, [])
                self.assertIn("Refusing to publish the reserved tag", out)
                self.assertIn(f"from {ref}.", out)

    def test_manual_dev_rejects_incomplete_and_suffixed_release_ref_prefixes(self) -> None:
        for ref in (
            "refs/tags/v0.24.0notes",
            "refs/tags/v0.24.0/feature",
            "refs/tags/v0.24",
            "refs/tags/v0.24.0-",
            "refs/tags/v0.24.0-rc..1",
            "refs/tags/0.24.0",
            "refs/heads/v0.24.0",
            "refs/heads/v0.24.0-rc.1",
            "refs/heads/main/feature",
        ):
            with self.subTest(ref=ref):
                code, out, tags = self._run_manual(ref, input_tags="qa-one, dev")
                self.assertNotEqual(code, 0)
                self.assertEqual(tags, [])
                self.assertIn("Refusing to publish the reserved tag 'dev'", out)

        for ref in (
            "refs/tags/v0.24.0-rc.1",
            "refs/tags/v0.24.0-beta.2",
            "refs/tags/v0.24.0-beta-one.2",
            "refs/tags/v0.24.0+build.1",
        ):
            with self.subTest(ref=ref):
                code, out, tags = self._run_manual(ref, input_tags="dev")
                self.assertEqual((code, tags), (0, ["dev"]), out)

    def test_manual_latest_rejects_prereleases_and_nonstable_refs(self) -> None:
        for ref in (
            "refs/tags/v0.24.0-rc.1",
            "refs/tags/v0.24.0-beta.1",
            "refs/tags/v0.24.0+build.1",
            "refs/tags/v0.24.0notes",
            "refs/tags/0.24.0",
            "refs/tags/main",
            "refs/heads/v0.24.0",
        ):
            for input_tags, make_latest in (("qa, latest", False), ("qa", True)):
                with self.subTest(ref=ref, input_tags=input_tags, make_latest=make_latest):
                    code, out, tags = self._run_manual(ref, input_tags, make_latest)
                    self.assertNotEqual(code, 0)
                    self.assertEqual(tags, [])
                    self.assertIn("Refusing to publish the reserved tag 'latest'", out)

    def test_manual_version_aliases_require_the_full_matching_tag_ref(self) -> None:
        for suffix in ("", "-rc.1", "-beta.2"):
            names = [f"v0.24.0{suffix}", f"0.24.0{suffix}"]
            for ref in (
                f"refs/heads/v0.24.0{suffix}",
                f"refs/tags/0.24.0{suffix}",
                "refs/heads/main",
                "refs/tags/v0.24.1",
            ):
                for name in names:
                    with self.subTest(ref=ref, name=name):
                        code, out, tags = self._run_manual(ref, input_tags=name)
                        self.assertNotEqual(code, 0)
                        self.assertEqual(tags, [])
                        self.assertIn("Refusing to publish the reserved tag", out)
            code, out, tags = self._run_manual(f"refs/tags/{names[0]}", input_tags=",".join(names))
            self.assertEqual((code, tags), (0, names), out)

    def test_manual_empty_and_separated_inputs_keep_defaults_and_deduplicate(self) -> None:
        for input_tags in ("", " , , \t\n\r "):
            with self.subTest(input_tags=input_tags):
                code, out, tags = self._run_manual("refs/heads/fix/example-change", input_tags)
                self.assertEqual((code, tags), (0, ["fix-example-change", "sha-0123456"]), out)
        code, out, tags = self._run_manual(
            "refs/heads/fix/example-change", "qa-one, qa-two\tsha-abc1234\nqa-one\rqa-three"
        )
        self.assertEqual((code, tags), (0, ["qa-one", "qa-two", "sha-abc1234", "qa-three"]), out)

    def test_manual_input_tags_do_not_expand_checkout_filenames(self) -> None:
        code, out, tags, _ = _run_release_step(
            "Resolve publish tags", "", False, manual_ref="refs/heads/feature",
            input_tags="qa-*", fixture_files=("qa-one", "qa-two"),
        )
        self.assertEqual((code, tags), (0, ["ghcr.io/gcs8/truenas-jbod-ui:qa-*"]), out)

    def test_manual_input_tags_remain_literal_shell_data(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "injected"
            for payload in (
                f"$(touch${{IFS}}{marker})",
                f"`touch${{IFS}}{marker}`",
                f"qa;touch${{IFS}}{marker}",
                f'qa\";touch${{IFS}}{marker};#',
            ):
                with self.subTest(payload=payload):
                    code, out, tags = self._run_manual("refs/heads/feature", input_tags=payload)
                    self.assertEqual((code, tags), (0, [payload]), out)
                    self.assertFalse(marker.exists(), "image_tags executed as shell code")

        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        publish = workflow["jobs"]["publish"]
        self.assertIn("inputs.image_tags", publish["env"]["INPUT_TAGS"])
        step = next(s for s in publish["steps"] if s.get("name") == "Resolve publish tags")
        self.assertNotIn("${{", step["run"], "Pass inputs through env, never interpolate into bash")

    def test_release_candidate_event_keeps_the_exact_release_tags_and_dev(self) -> None:
        code, out, tags, _ = _run_release_step("Resolve publish tags", "v0.24.0-rc.2", prerelease=True)
        self.assertEqual(
            (code, tags),
            (0, [f"ghcr.io/gcs8/truenas-jbod-ui:{tag}" for tag in ("v0.24.0-rc.2", "0.24.0-rc.2", "dev")]),
            out,
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
