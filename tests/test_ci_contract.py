from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
ADMIN_CLEANROOM_CONFIG = ROOT / "qa" / "fixtures" / "admin-cleanroom-config.yaml"
ADMIN_CLEANROOM_SPEC = ROOT / "qa" / "admin-operations.spec.js"
LIVE_UI_SPEC = ROOT / "qa" / "ui-switching.spec.js"
LIVE_ESXI_SPEC = ROOT / "qa" / "esxi-smoke.spec.js"
SMOKE_CONFIG = ROOT / "tests" / "fixtures" / "ci-smoke-config.yaml"
SMOKE_COMPOSE = ROOT / "tests" / "fixtures" / "ci-smoke.compose.yml"
EXTERNAL_ACTION_RE = re.compile(
    r"^\s*uses:\s*(?P<action>[^@\s#]+)@(?P<ref>[^\s#]+)(?:\s+#\s*(?P<version>v\d+))?\s*$",
    re.MULTILINE,
)


class CIWorkflowContractTests(unittest.TestCase):
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_python_floor_and_ceiling_are_separate_matrix_entries(self) -> None:
        workflow = yaml.safe_load(self.read(CI_WORKFLOW))
        job = workflow["jobs"]["python-source"]

        self.assertEqual(job["strategy"]["matrix"]["python-version"], ["3.12", "3.14"])
        self.assertIn("${{ matrix.python-version }}", job["name"])
        setup_step = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
        self.assertEqual(setup_step["with"]["python-version"], "${{ matrix.python-version }}")

    def test_bounded_ruff_gate_and_config_are_present(self) -> None:
        workflow_text = self.read(CI_WORKFLOW)
        requirements = self.read(ROOT / "requirements-dev.txt")
        pyproject = self.read(ROOT / "pyproject.toml")

        self.assertRegex(requirements, r"(?m)^ruff(?:==|>=).+$")
        self.assertIn("ruff-check:", workflow_text)
        self.assertIn("ruff check app admin_service history_service scripts tests --select E4,E7,E9,F", workflow_text)
        self.assertIn("select = [\"E4\", \"E7\", \"E9\", \"F\"]", pyproject)

    def test_ci_smoke_fixture_disables_live_dependencies(self) -> None:
        config = yaml.safe_load(self.read(SMOKE_CONFIG))
        compose = yaml.safe_load(self.read(SMOKE_COMPOSE))

        self.assertFalse(config["app"]["release_check_enabled"])
        self.assertFalse(config["app"]["startup_warm_cache_enabled"])
        self.assertFalse(config["app"]["startup_warm_smart_enabled"])
        self.assertEqual(config["truenas"]["host"], "http://127.0.0.1:9")
        self.assertFalse(config["ssh"]["enabled"])
        service = compose["services"]["enclosure-ui"]
        self.assertEqual(service["build"]["context"], "../..")
        self.assertEqual(service["build"]["dockerfile"], "Dockerfile")
        self.assertIn("18080:8000", service["ports"])
        self.assertIn("./ci-smoke-config.yaml:/app/config/config.yaml:ro", service["volumes"])

    def test_ci_runs_production_container_smoke_and_checks_health(self) -> None:
        workflow_text = self.read(CI_WORKFLOW)

        self.assertIn("container-smoke:", workflow_text)
        self.assertIn("docker compose -f tests/fixtures/ci-smoke.compose.yml up -d --build --wait --wait-timeout 90", workflow_text)
        self.assertIn("trap 'docker compose -f tests/fixtures/ci-smoke.compose.yml down --volumes --remove-orphans' EXIT", workflow_text)
        self.assertIn("http://127.0.0.1:18080/livez", workflow_text)
        self.assertIn("http://127.0.0.1:18080/healthz", workflow_text)
        self.assertIn('"dependency_status": "unknown"', workflow_text)
        self.assertIn('"cache_state": "empty"', workflow_text)

    def test_ci_runs_admin_browser_qa_against_cleanroom_fixture_without_skips(self) -> None:
        workflow = yaml.safe_load(self.read(CI_WORKFLOW))
        config = yaml.safe_load(self.read(ADMIN_CLEANROOM_CONFIG))
        spec = self.read(ADMIN_CLEANROOM_SPEC)

        job = workflow["jobs"]["admin-browser-cleanroom"]
        commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
        browser_step = next(
            step
            for step in job["steps"]
            if step.get("name") == "Run admin browser checks against the clean-room fixture"
        )
        self.assertEqual(config["systems"], [])
        self.assertFalse(config["app"]["release_check_enabled"])
        self.assertFalse(config["app"]["startup_warm_cache_enabled"])
        self.assertFalse(config["app"]["startup_warm_smart_enabled"])
        self.assertEqual(
            browser_step["env"]["APP_CONFIG_PATH"],
            "${{ github.workspace }}/qa/fixtures/admin-cleanroom-config.yaml",
        )
        self.assertIn("npm ci --ignore-scripts", commands)
        self.assertIn("npx playwright test qa/admin-operations.spec.js", commands)
        self.assertIn("http://127.0.0.1:8082/healthz", commands)
        self.assertIn("git status --short", commands)
        self.assertNotIn("test.skip", spec)

    def test_appliance_browser_specs_are_explicit_and_portable(self) -> None:
        contributing = self.read(ROOT / "CONTRIBUTING.md")
        ui_spec = self.read(LIVE_UI_SPEC)
        esxi_spec = self.read(LIVE_ESXI_SPEC)

        for spec in (ui_spec, esxi_spec):
            self.assertIn("PLAYWRIGHT_LIVE_APPLIANCE_QA", spec)
            self.assertIn("Live appliance QA requires", spec)
        self.assertNotRegex(
            ui_spec,
            r"orderedSystemCandidates\(systems,\s*\[\s*currentSystem\s*,",
        )
        self.assertNotRegex(ui_spec, r"20\d{2}-\d{2}-\d{2}T")
        self.assertIn(
            "PLAYWRIGHT_LIVE_APPLIANCE_QA=1 npx playwright test qa/ui-switching.spec.js qa/esxi-smoke.spec.js",
            contributing,
        )

    def test_external_actions_are_sha_pinned_with_version_comments(self) -> None:
        unpinned: list[str] = []
        uncommented: list[str] = []
        action_count = 0
        workflow_paths = sorted(
            path for path in WORKFLOW_DIR.iterdir() if path.suffix in {".yml", ".yaml"}
        )
        for workflow_path in workflow_paths:
            for match in EXTERNAL_ACTION_RE.finditer(self.read(workflow_path)):
                action = match.group("action")
                if action.startswith(("./", "docker://")):
                    continue
                action_count += 1
                if re.fullmatch(r"[0-9a-f]{40}", match.group("ref")) is None:
                    unpinned.append(f"{workflow_path.name}: {action}@{match.group('ref')}")
                if match.group("version") is None:
                    uncommented.append(f"{workflow_path.name}: {action}")

        self.assertEqual(action_count, 28)
        self.assertEqual(unpinned, [])
        self.assertEqual(uncommented, [])

    def test_public_demo_artifact_and_browser_smoke_remain_in_ci(self) -> None:
        workflow_text = self.read(CI_WORKFLOW)

        self.assertIn("python scripts/check_public_demo_artifact.py public-demo", workflow_text)
        self.assertIn("npx playwright test qa/public-demo.spec.js", workflow_text)
        self.assertIn("npm ci --ignore-scripts", workflow_text)

    def test_dependabot_keeps_immutable_actions_maintained(self) -> None:
        config = yaml.safe_load(self.read(ROOT / ".github" / "dependabot.yml"))
        actions_entries = [
            item
            for item in config["updates"]
            if item.get("package-ecosystem") == "github-actions"
        ]

        self.assertEqual(len(actions_entries), 1)
        self.assertEqual(actions_entries[0]["directory"], "/")
        self.assertEqual(actions_entries[0]["schedule"]["interval"], "weekly")

    def test_contributing_documents_blocking_and_report_only_checks(self) -> None:
        contributing = self.read(ROOT / "CONTRIBUTING.md")

        self.assertIn("## CI blocking policy", contributing)
        for required_check in (
            "Diff hygiene",
            "Python compile and unittest (3.12)",
            "Python compile and unittest (3.14)",
            "Bounded Ruff",
            "Production container smoke",
            "JavaScript syntax and npm lock",
            "Checked-in public demo artifact",
            "Admin clean-room browser QA",
        ):
            self.assertIn(required_check, contributing)
        self.assertIn("Coverage is report-only", contributing)
        self.assertIn("CodeQL is report-only", contributing)


if __name__ == "__main__":
    unittest.main()
