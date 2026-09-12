from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
PUBLISH_GHCR_WORKFLOW = WORKFLOW_DIR / "publish-ghcr.yml"
PUBLISH_PUBLIC_DEMO_WORKFLOW = WORKFLOW_DIR / "publish-public-demo.yml"
CAPTURE_SCREENSHOTS_WORKFLOW = WORKFLOW_DIR / "capture-public-demo-screenshots.yml"
RELEASE_CHECKLIST = ROOT / "docs" / "RELEASE_CHECKLIST.md"
PUBLIC_DEMO_SPEC = ROOT / "qa" / "public-demo.spec.js"
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

    def test_ci_trigger_matches_the_documented_branch_policy(self) -> None:
        workflow = yaml.safe_load(self.read(CI_WORKFLOW))
        triggers = workflow.get("on", workflow.get(True, {}))
        contributing = self.read(ROOT / "CONTRIBUTING.md")

        self.assertEqual(triggers["pull_request"]["branches"], ["main"])
        self.assertEqual(triggers["push"]["branches"], ["**"])
        self.assertIn(
            "CI runs on every branch push and on pull requests targeting `main`.",
            contributing,
        )

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
        self.assertIn("cleanup() {", workflow_text)
        self.assertIn(
            "docker compose -f tests/fixtures/ci-smoke.compose.yml down --volumes --remove-orphans",
            workflow_text,
        )
        self.assertIn('if [ -f "$compose_contract_root/compose.yaml" ]; then', workflow_text)
        self.assertIn("trap cleanup EXIT", workflow_text)
        self.assertIn("http://127.0.0.1:18080/livez", workflow_text)
        self.assertIn("http://127.0.0.1:18080/healthz", workflow_text)
        self.assertIn('"dependency_status": "unknown"', workflow_text)
        self.assertIn('"cache_state": "empty"', workflow_text)
        self.assertIn(
            'cp docker-compose.nonroot.yml "$compose_contract_root/nonroot.yaml"',
            workflow_text,
        )
        self.assertGreaterEqual(
            workflow_text.count('-f "$compose_contract_root/nonroot.yaml"'), 2
        )
        self.assertIn("hardened_compose_service_contracts=ok", workflow_text)
        self.assertIn(
            "probe_compose_service enclosure-admin 0 10001 0000000000000009 "
            "/app/host-prep /app/data",
            workflow_text,
        )

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

        # 29 existing uses plus checkout, setup-python, and setup-node in the
        # owner-gated Pages readback job, plus checkout and upload-artifact in
        # the dispatch-only screenshot capture workflow.
        self.assertEqual(action_count, 34)
        self.assertEqual(unpinned, [])
        self.assertEqual(uncommented, [])

    def test_all_workflow_checkouts_disable_persisted_credentials(self) -> None:
        violations: list[str] = []
        for workflow_path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            workflow = yaml.safe_load(self.read(workflow_path))
            for job_name, job in workflow.get("jobs", {}).items():
                for step in job.get("steps", []):
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        if step.get("with", {}).get("persist-credentials") is not False:
                            violations.append(f"{workflow_path.name}:{job_name}:{step.get('name')}")

        self.assertEqual(violations, [])

    def test_all_workflow_npm_ci_commands_ignore_lifecycle_scripts(self) -> None:
        violations: list[str] = []
        for workflow_path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
            workflow = yaml.safe_load(self.read(workflow_path))
            for job_name, job in workflow.get("jobs", {}).items():
                for step in job.get("steps", []):
                    for line in str(step.get("run", "")).splitlines():
                        if re.search(r"(?:^|\s)npm\s+ci(?:\s|$)", line) and "--ignore-scripts" not in line:
                            violations.append(f"{workflow_path.name}:{job_name}:{step.get('name')}")

        self.assertEqual(violations, [])

    def test_release_tag_expression_is_passed_via_step_environment(self) -> None:
        workflow = yaml.safe_load(self.read(PUBLISH_GHCR_WORKFLOW))
        prep_step = next(
            step
            for step in workflow["jobs"]["publish"]["steps"]
            if step.get("name") == "Resolve publish tags"
        )

        self.assertEqual(prep_step["env"]["RELEASE_TAG"], "${{ github.event.release.tag_name }}")
        self.assertNotIn("${{ github.event.release.tag_name }}", prep_step["run"])
        self.assertIn('release_tag="$RELEASE_TAG"', prep_step["run"])

    def test_current_source_fixture_jobs_install_runtime_requirements_before_generation(self) -> None:
        jobs = (
            (CI_WORKFLOW, "public-demo-artifact"),
            (PUBLISH_PUBLIC_DEMO_WORKFLOW, "verify"),
        )
        for workflow_path, job_name in jobs:
            with self.subTest(workflow=workflow_path.name, job=job_name):
                workflow = yaml.safe_load(self.read(workflow_path))
                steps = workflow["jobs"][job_name]["steps"]
                setup_index = next(
                    index
                    for index, step in enumerate(steps)
                    if str(step.get("uses", "")).startswith("actions/setup-python@")
                )
                install_index = next(
                    index
                    for index, step in enumerate(steps)
                    if "python -m pip install -r requirements.txt" in str(step.get("run", ""))
                )
                generator_index = next(
                    index
                    for index, step in enumerate(steps)
                    if "python scripts/build_current_source_browser_fixture.py" in str(step.get("run", ""))
                )

                self.assertLess(setup_index, install_index)
                self.assertLess(install_index, generator_index)
                self.assertNotIn("requirements-dev.txt", str(steps[install_index].get("run", "")))

    def test_public_demo_publish_push_trigger_requires_checked_in_artifact_change(self) -> None:
        workflow = yaml.safe_load(self.read(PUBLISH_PUBLIC_DEMO_WORKFLOW))
        triggers = workflow.get("on", workflow.get(True, {}))

        self.assertIn(".github/workflows/publish-public-demo.yml", triggers["pull_request"]["paths"])
        self.assertIn("scripts/build_current_source_browser_fixture.py", triggers["pull_request"]["paths"])
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertEqual(triggers["push"]["paths"], ["public-demo/**"])
        self.assertIn("workflow_dispatch", triggers)

    def test_public_demo_artifact_and_browser_smoke_remain_in_ci(self) -> None:
        spec = self.read(PUBLIC_DEMO_SPEC)
        self.assertNotIn("test.skip", spec)
        self.assertIn("SLOT_FOCUS_ARTIFACT", spec)

        for workflow_path in (CI_WORKFLOW, PUBLISH_PUBLIC_DEMO_WORKFLOW):
            with self.subTest(workflow=workflow_path.name):
                workflow_text = self.read(workflow_path)
                self.assertIn("python scripts/check_public_demo_artifact.py public-demo", workflow_text)
                self.assertIn("python scripts/build_current_source_browser_fixture.py", workflow_text)
                self.assertIn("PUBLIC_DEMO_ARTIFACT: public-demo/index.html", workflow_text)
                self.assertIn("SLOT_FOCUS_ARTIFACT:", workflow_text)
                self.assertIn("npx playwright test qa/public-demo.spec.js --retries=0", workflow_text)
                self.assertIn("npm ci --ignore-scripts", workflow_text)
                self.assertIn('rm -rf "$fixture_root"', workflow_text)
                self.assertIn("git status --short", workflow_text)

    def test_screenshot_capture_workflow_is_dispatch_only_pinned_and_read_only(self) -> None:
        workflow = yaml.safe_load(self.read(CAPTURE_SCREENSHOTS_WORKFLOW))
        lock = json.loads(self.read(ROOT / "package-lock.json"))
        locked_playwright = lock["packages"]["node_modules/@playwright/test"]["version"]
        triggers = workflow.get("on", workflow.get(True, {}))
        job = workflow["jobs"]["capture"]
        commands = "\n".join(str(step.get("run", "")) for step in job["steps"])

        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertIn("ref", triggers["workflow_dispatch"]["inputs"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", job)
        image = job["container"]["image"]
        expected_tag = f"mcr.microsoft.com/playwright:v{locked_playwright}-jammy"
        self.assertRegex(image, rf"^{re.escape(expected_tag)}@sha256:[0-9a-f]{{64}}$")
        self.assertEqual(job["env"]["CONTAINER_IMAGE"], image)
        self.assertEqual(job["env"]["CONTAINER_TAG"], expected_tag)
        self.assertEqual(job["env"]["PLAYWRIGHT_VERSION"], locked_playwright)
        self.assertIn('printf \'container image: %s\\n\' "$CONTAINER_IMAGE"', commands)
        self.assertIn('printf \'container tag: %s\\n\' "$CONTAINER_TAG"', commands)
        ref_check = next(
            step
            for step in job["steps"]
            if step.get("name") == "Require the requested ref to be the checked-out commit SHA"
        )
        self.assertEqual(ref_check["env"]["REQUESTED_REF"], "${{ inputs.ref }}")
        self.assertIn('[[ ! "$REQUESTED_REF" =~ ^[0-9a-f]{40}$ ]]', ref_check["run"])
        self.assertIn('resolved_ref="$(git rev-parse HEAD)"', ref_check["run"])
        self.assertIn('[ "$resolved_ref" != "$REQUESTED_REF" ]', ref_check["run"])

        self.assertEqual(commands.count("node scripts/capture_public_demo_screenshots.js"), 2)
        self.assertIn('cmp -s "$CANDIDATE_DIR/run-1/$name" "$CANDIDATE_DIR/run-2/$name"', commands)
        self.assertIn("the capture is not byte-reproducible in this environment", commands)
        self.assertIn("npm ci --ignore-scripts", commands)
        self.assertIn("scripts/check_public_screenshots.py --report", commands)
        self.assertIn(
            'sha256sum ./*.png | tee sha256sums.txt',
            commands,
        )
        self.assertIn("fc-match", commands)
        self.assertIn("git status --short", commands)
        for forbidden in ("git push", "git commit", "gh pr ", "gh release", "peter-evans"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.read(CAPTURE_SCREENSHOTS_WORKFLOW))

        upload = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        self.assertEqual(upload["with"]["retention-days"], 14)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(
            upload["with"]["path"], "${{ runner.temp }}/public-demo-screenshot-candidate"
        )

    def test_release_checklist_uses_and_reviews_the_dispatch_capture_artifact(self) -> None:
        checklist = self.read(RELEASE_CHECKLIST)
        screenshot_section = checklist.split("## Screenshots", 1)[1].split("\n## ", 1)[0]

        for required in (
            "capture-public-demo-screenshots.yml",
            "full commit SHA",
            "qualification_only=false",
            "public-demo-screenshot-candidate",
            "gh run download",
            "sha256sum --check sha256sums.txt",
            "proposed-manifest.json",
            "platform-fonts.json",
            "capture.log",
            "SCREENSHOT_CAPTURE.md",
        ):
            with self.subTest(required=required):
                self.assertIn(required, screenshot_section)
        self.assertNotIn(
            "node scripts/capture_public_demo_screenshots.js",
            screenshot_section,
        )

    def test_screenshot_capture_qualification_run_cannot_pass_as_a_candidate(self) -> None:
        workflow = yaml.safe_load(self.read(CAPTURE_SCREENSHOTS_WORKFLOW))
        triggers = workflow.get("on", workflow.get(True, {}))
        inputs = triggers["workflow_dispatch"]["inputs"]
        job = workflow["jobs"]["capture"]
        docs = self.read(ROOT / "docs" / "SCREENSHOT_CAPTURE.md")
        capture_step = next(
            run
            for run in (str(step.get("run", "")) for step in job["steps"])
            if "proposed-manifest.json" in run
        )
        upload = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )

        self.assertEqual(inputs["qualification_only"]["type"], "boolean")
        self.assertIs(inputs["qualification_only"]["default"], True)
        self.assertIn("commit SHA", inputs["ref"]["description"])
        self.assertEqual(job["env"]["QUALIFICATION_ONLY"], "${{ inputs.qualification_only }}")
        self.assertIn('if [ "$QUALIFICATION_ONLY" = "true" ]', capture_step)
        self.assertIn("public-demo-screenshot-qualification", str(upload["with"]["name"]))
        self.assertIn("public-demo-screenshot-candidate", str(upload["with"]["name"]))
        self.assertIn("public-demo-screenshot-qualification", docs)

    def test_screenshot_capture_checks_the_fonts_chromium_actually_used(self) -> None:
        workflow = yaml.safe_load(self.read(CAPTURE_SCREENSHOTS_WORKFLOW))
        job = workflow["jobs"]["capture"]
        runs = [str(step.get("run", "")) for step in job["steps"]]
        probe_step = next(run for run in runs if "report_public_demo_platform_fonts.js" in run)
        fc_match_step = next(run for run in runs if "fc-match" in run)
        script = self.read(ROOT / "scripts" / "report_public_demo_platform_fonts.js")

        self.assertIn(
            'node scripts/report_public_demo_platform_fonts.js "$CANDIDATE_DIR/platform-fonts.json"',
            probe_step,
        )
        # The expected families are compared against the platform fonts Chromium
        # reported, not against fc-match, whose output stays informational.
        for variable in ("EXPECTED_SANS_FALLBACK", "EXPECTED_MONO_FALLBACK"):
            with self.subTest(variable=variable):
                self.assertIn(variable, probe_step)
                self.assertNotIn(variable, fc_match_step)
        self.assertIn("probes.sans.dominant_family", probe_step)
        self.assertIn("probes.mono.dominant_family", probe_step)
        self.assertIn('"$CANDIDATE_DIR/platform-fonts.json"', probe_step)

        for fragment in (
            "CSS.getPlatformFontsForNode",
            "DOM.enable",
            "CSS.enable",
            "DOM.getDocument",
            "DOM.querySelector",
            "newCDPSession",
            "glyphCount",
            "dominant_family",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)

    def test_public_demo_pages_request_allowlist_is_probe_specific(self) -> None:
        spec = self.read(PUBLIC_DEMO_SPEC)

        self.assertIn(
            'const CHROME_LOCALHOST_DEVTOOLS_PROBE = "/.well-known/appspecific/com.chrome.devtools.json";',
            spec,
        )
        self.assertIn("function isExpectedPagesRequest(requestURL)", spec)
        self.assertIn("requestPath === CHROME_LOCALHOST_DEVTOOLS_PROBE", spec)
        self.assertIn("const unexpectedPagesRequests = fixture.requests.filter", spec)
        self.assertIn("expect(unexpectedPagesRequests).toEqual([])", spec)
        self.assertNotIn(
            'fixture.requests.every((request) => request.startsWith("/truenas-jbod-ui/"))',
            spec,
        )

    def test_public_docs_screenshots_and_deployment_readback_are_release_gates(self) -> None:
        ci = self.read(CI_WORKFLOW)
        publish = self.read(PUBLISH_PUBLIC_DEMO_WORKFLOW)

        for workflow_text in (ci, publish):
            self.assertIn("python scripts/check_public_docs.py", workflow_text)
            self.assertIn("python scripts/check_public_screenshots.py", workflow_text)
        self.assertIn("source_sha: ${{ github.sha }}", publish)
        self.assertIn("page_url: ${{ steps.deployment.outputs.page_url }}", publish)
        self.assertIn("scripts/check_public_demo_deployment.py", publish)
        self.assertIn("PUBLIC_DEMO_URL:", publish)
        self.assertIn("--grep \"published public demo\"", publish)

    def test_public_demo_source_revision_jobs_checkout_full_history(self) -> None:
        expected_jobs = {
            CI_WORKFLOW: ("python-source", "public-demo-artifact"),
            PUBLISH_PUBLIC_DEMO_WORKFLOW: ("verify",),
        }

        for workflow_path, job_names in expected_jobs.items():
            workflow = yaml.safe_load(self.read(workflow_path))
            for job_name in job_names:
                with self.subTest(workflow=workflow_path.name, job=job_name):
                    checkout = next(
                        step
                        for step in workflow["jobs"][job_name]["steps"]
                        if str(step.get("uses", "")).startswith("actions/checkout@")
                    )
                    self.assertEqual(checkout.get("with", {}).get("fetch-depth"), 0)

    def test_release_checklist_public_demo_commands_supply_both_required_artifacts(self) -> None:
        checklist = self.read(ROOT / "docs" / "RELEASE_CHECKLIST.md")
        public_demo_commands = [
            line
            for line in checklist.splitlines()
            if "npx playwright test" in line and "qa/public-demo.spec.js" in line
        ]

        self.assertGreaterEqual(len(public_demo_commands), 2)
        for command in public_demo_commands:
            with self.subTest(command=command):
                self.assertIn("PUBLIC_DEMO_ARTIFACT", command)
                self.assertIn("SLOT_FOCUS_ARTIFACT", command)
        self.assertGreaterEqual(
            checklist.count("scripts/build_current_source_browser_fixture.py --output"),
            len(public_demo_commands),
        )

    def test_release_checklist_browser_fixture_cleanup_is_unique_and_failure_safe(self) -> None:
        checklist = self.read(ROOT / "docs" / "RELEASE_CHECKLIST.md")
        release_start = checklist.index("- if the release changes public-demo behavior or data")
        release_end = checklist.index("## Config And Examples", release_start)
        release_section = checklist[release_start:release_end]
        powershell_marker = "    ```powershell\n"
        self.assertEqual(release_section.count(powershell_marker), 1)
        powershell_start = release_section.index(powershell_marker) + len(powershell_marker)
        powershell_end = release_section.index("\n    ```", powershell_start)
        powershell = release_section[powershell_start:powershell_end]

        self.assertIn("trap 'rm -f -- \"$slot_focus_artifact\"' EXIT", checklist)
        self.assertIn("trap - EXIT", checklist)
        self.assertIn(
            "$slot_focus_artifact = [System.IO.Path]::GetTempFileName()",
            powershell,
        )
        self.assertNotIn("PUBLIC_DEMO_LOCAL_HISTORY", powershell)
        self.assertIn("finally {", powershell)

        self.assertIn(
            "Remove-Item Env:PUBLIC_DEMO_ARTIFACT -ErrorAction SilentlyContinue",
            powershell,
        )
        self.assertIn(
            "Remove-Item Env:SLOT_FOCUS_ARTIFACT -ErrorAction SilentlyContinue",
            powershell,
        )
        self.assertIn(
            """    } finally {
        Remove-Item Env:PUBLIC_DEMO_ARTIFACT -ErrorAction SilentlyContinue
        Remove-Item Env:SLOT_FOCUS_ARTIFACT -ErrorAction SilentlyContinue
    }""",
            powershell,
        )
        self.assertIn(
            "Remove-Item -LiteralPath $slot_focus_artifact -ErrorAction SilentlyContinue",
            powershell,
        )
        self.assertNotIn("set PUBLIC_DEMO_LOCAL_HISTORY=1", powershell)
        self.assertNotIn(r"%TEMP%\truenas-jbod-ui-slot-focus.html", powershell)

    def test_release_cleanup_contract_rejects_a_block_relocated_outside_the_sequence(
        self,
    ) -> None:
        checklist_path = ROOT / "docs" / "RELEASE_CHECKLIST.md"
        checklist = self.read(checklist_path)
        cleanup = """    } finally {
        Remove-Item Env:PUBLIC_DEMO_ARTIFACT -ErrorAction SilentlyContinue
        Remove-Item Env:SLOT_FOCUS_ARTIFACT -ErrorAction SilentlyContinue
    }"""
        mutated = checklist.replace(cleanup, "", 1) + f"\n```powershell\n{cleanup}\n```\n"
        case = CIWorkflowContractTests(
            "test_release_checklist_browser_fixture_cleanup_is_unique_and_failure_safe"
        )
        case.read = lambda _path: mutated  # type: ignore[method-assign]

        with self.assertRaises(AssertionError):
            case.test_release_checklist_browser_fixture_cleanup_is_unique_and_failure_safe()

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

    def test_changelog_entry_gate_runs_only_for_pull_requests_without_pr_code_execution(self) -> None:
        workflow = yaml.safe_load(self.read(CI_WORKFLOW))
        job = workflow["jobs"]["changelog-entry"]

        self.assertEqual(job["name"], "Changelog entry")
        self.assertEqual(job["if"], "github.event_name == 'pull_request'")
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "read"})
        checkout = next(
            step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        gate_step = next(
            step for step in job["steps"] if "check_changelog_entry.py" in str(step.get("run", ""))
        )
        self.assertEqual(gate_step["env"]["PR_BASE_SHA"], "${{ github.event.pull_request.base.sha }}")
        self.assertEqual(gate_step["env"]["PR_NUMBER"], "${{ github.event.pull_request.number }}")
        self.assertNotIn("${{", gate_step["run"])
        self.assertIn('--labels "$labels"', gate_step["run"])

    def test_pr_label_workflow_never_checks_out_or_runs_pull_request_code(self) -> None:
        workflow = yaml.safe_load(self.read(WORKFLOW_DIR / "pr-labels.yml"))
        triggers = workflow.get("on", workflow.get(True, {}))

        self.assertEqual(
            triggers["pull_request_target"]["types"],
            ["opened", "edited", "synchronize", "reopened"],
        )
        self.assertEqual(workflow["permissions"], {"pull-requests": "write"})
        for job_name, job in workflow["jobs"].items():
            for step in job["steps"]:
                with self.subTest(job=job_name, step=step.get("name")):
                    self.assertNotIn("uses", step)
                    self.assertNotIn("${{", str(step.get("run", "")))
        label_step = workflow["jobs"]["label"]["steps"][0]
        self.assertEqual(label_step["env"]["PR_TITLE"], "${{ github.event.pull_request.title }}")
        for mapping in (
            "feat) want+=(enhancement)",
            "fix) want+=(bug)",
            "perf) want+=(performance)",
            "docs) want+=(documentation)",
            "test) want+=(tests)",
            "ci) want+=(ci)",
            "refactor|chore|build) want+=(internal)",
            "security) want+=(security)",
        ):
            self.assertIn(mapping, label_step["run"])
        self.assertRegex(
            label_step["run"],
            r"\*\)\s+echo \"Unknown conventional type.*\n\s+exit 0\n\s+;;",
        )

    def test_pr_label_workflow_uses_rest_without_graphql_pr_commands(self) -> None:
        workflow = yaml.safe_load(self.read(WORKFLOW_DIR / "pr-labels.yml"))
        label_step = workflow["jobs"]["label"]["steps"][0]
        script = label_step["run"]

        self.assertNotIn("gh pr ", script)
        self.assertIn('gh api "repos/$GH_REPO/issues/$PR_NUMBER/labels?per_page=100"', script)
        self.assertIn('gh api --method POST "repos/$GH_REPO/issues/$PR_NUMBER/labels"', script)
        self.assertIn(
            'gh api --method DELETE "repos/$GH_REPO/issues/$PR_NUMBER/labels/$label"',
            script,
        )

    def test_release_checklist_collects_bounded_branch_metadata_and_keeps_wiki_publication_owner_gated(self) -> None:
        checklist = self.read(ROOT / "docs" / "RELEASE_CHECKLIST.md")
        contributing = self.read(ROOT / "CONTRIBUTING.md")

        merged_pr_command = next(
            line for line in checklist.splitlines() if "gh pr list -R gcs8/truenas-jbod-ui" in line
        )
        self.assertIn("--limit 1000", merged_pr_command)
        self.assertNotIn("merged:", merged_pr_command)
        self.assertIn(
            "--json number,mergedAt,labels,mergeCommit,baseRefName,headRefName,isCrossRepository",
            merged_pr_command,
        )
        self.assertIn(
            "--limit 1000 --json "
            "number,mergedAt,labels,mergeCommit,baseRefName,headRefName,isCrossRepository",
            " ".join(contributing.split()),
        )
        checklist_text = " ".join(checklist.split())
        self.assertIn("No release command may push the wiki automatically", checklist_text)
        self.assertIn("only its distinct publication row may remain `Blocked`", checklist_text)

    def test_dependencies_category_precedes_internal_for_mixed_label_prs(self) -> None:
        config = yaml.safe_load(self.read(ROOT / ".github" / "release.yml"))
        titles = [category["title"] for category in config["changelog"]["categories"]]

        self.assertLess(titles.index("Dependencies"), titles.index("Internal"))

    def test_release_notes_categories_follow_the_documented_label_order(self) -> None:
        config = yaml.safe_load(self.read(ROOT / ".github" / "release.yml"))

        self.assertEqual(config["changelog"]["exclude"]["labels"], ["no-changelog"])
        self.assertEqual(
            [(category["title"], category["labels"]) for category in config["changelog"]["categories"]],
            [
                ("Breaking changes", ["breaking"]),
                ("Security", ["security"]),
                ("Features", ["enhancement"]),
                ("Fixes", ["bug"]),
                ("Performance", ["performance"]),
                ("Documentation", ["documentation"]),
                ("Dependencies", ["dependencies"]),
                ("Internal", ["internal", "tests", "ci"]),
            ],
        )

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
            "Changelog entry",
        ):
            self.assertIn(required_check, contributing)
        self.assertIn("Coverage is report-only", contributing)
        self.assertIn("CodeQL is report-only", contributing)


if __name__ == "__main__":
    unittest.main()
