"""#1191: the fixture-only browser specs run as parallel CI legs.

Every fixture spec under ``qa/`` must run on exactly one leg of the
``fixture-browser`` matrix in ``.github/workflows/ci.yml``. The legs need only
the route job, so they start beside the unit shards; the required
``Checked-in public demo artifact`` check must fail unless every leg and both
source gates passed. These tests read the workflow and the ``qa/`` tree only;
nothing runs a browser.
"""

from __future__ import annotations

import copy
import itertools
import os
import re
import subprocess
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
QA_DIR = ROOT / "qa"
LEG_JOB = "fixture-browser"
AGGREGATE_JOB = "public-demo-artifact"
AGGREGATE_NAME = "Checked-in public demo artifact"
EXPECTED_LEGS = ("1", "2", "3", "4")
LEG_RUN_STEP = "Run fixture-only browser specs"
SOURCE_GATES = ("javascript-source", "python-source")
AGGREGATE_STEP = "Require every leg and both source gates to succeed"
# Environment variable in the aggregate's check step -> the job whose result it holds.
AGGREGATE_RESULT_ENV = {
    "LEG_RESULT": LEG_JOB,
    "JAVASCRIPT_SOURCE_RESULT": "javascript-source",
    "PYTHON_SOURCE_RESULT": "python-source",
}
JOB_RESULTS = ("success", "failure", "cancelled", "skipped", "")

# Spec files under qa/ that deliberately run on no fixture leg, and where they
# run instead. A new spec file must be added to a leg or to this table.
NOT_ON_A_FIXTURE_LEG = {
    "qa/public-demo.spec.js": "the smoke leg runs it against a current-source demo build",
    "qa/admin-operations.spec.js": "the Admin clean-room browser QA job runs it against a started admin service",
    "qa/ui-switching.spec.js": "live appliance QA, manual only",
    "qa/esxi-smoke.spec.js": "live appliance QA, manual only",
    "qa/private-restore.spec.js": "private restore QA, manual only",
    "qa/fabric-stale-refresh.spec.js": (
        "standalone entry point; qa/header-and-disk-path-layout.spec.js registers the same suite"
    ),
    "qa/smart-browser-ownership.spec.js": (
        "standalone entry point; qa/header-and-disk-path-layout.spec.js registers the same suite"
    ),
}


def load_workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def qa_spec_files(root: Path = ROOT) -> list[str]:
    """Every file Playwright's ``testMatch: "**/*.spec.js"`` can load under qa/."""
    return sorted(path.relative_to(root).as_posix() for path in (root / "qa").rglob("*.spec.js"))


def leg_entries(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    job = workflow.get("jobs", {}).get(LEG_JOB)
    if not isinstance(job, dict):
        return []
    include = job.get("strategy", {}).get("matrix", {}).get("include")
    return include if isinstance(include, list) else []


def leg_specs(workflow: dict[str, Any]) -> list[tuple[str, list[str]]]:
    return [
        (str(entry.get("leg")), str(entry.get("specs") or "").split())
        for entry in leg_entries(workflow)
    ]


def leg_assignment_problems(workflow: dict[str, Any], spec_files: list[str]) -> list[str]:
    """Return why the leg lists do not run every fixture spec exactly once."""
    problems: list[str] = []
    legs = leg_specs(workflow)
    if not legs:
        return [f"job {LEG_JOB} has no strategy.matrix.include leg list"]
    leg_ids = [leg for leg, _ in legs]
    if tuple(sorted(leg_ids)) != EXPECTED_LEGS:
        problems.append(f"legs are {leg_ids}, expected exactly {list(EXPECTED_LEGS)}")
    for leg, specs in legs:
        if not specs:
            problems.append(f"leg {leg} lists no spec")

    listed = Counter(spec for _, specs in legs for spec in specs)
    fixture_specs = [spec for spec in spec_files if spec not in NOT_ON_A_FIXTURE_LEG]
    for spec in fixture_specs:
        if listed[spec] == 0:
            problems.append(f"{spec} is not listed on any leg")
        elif listed[spec] > 1:
            problems.append(f"{spec} is listed {listed[spec]} times")
    for spec in sorted(listed):
        if spec not in spec_files:
            problems.append(f"{spec} is listed on a leg but is not a spec file under qa/")
        elif spec in NOT_ON_A_FIXTURE_LEG:
            problems.append(f"{spec} is listed on a leg but {NOT_ON_A_FIXTURE_LEG[spec]}")
        # Playwright reads each argument as a regular expression over the
        # file path, so an argument must select its own file and no other.
        selected = [candidate for candidate in spec_files if re.search(spec, candidate)]
        if spec in spec_files and selected != [spec]:
            problems.append(f"{spec} as a Playwright filter selects {selected}")
    for spec in sorted(NOT_ON_A_FIXTURE_LEG):
        if spec not in spec_files:
            problems.append(f"{spec} is in NOT_ON_A_FIXTURE_LEG but no longer exists")
    return problems


def step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def aggregate_check_step(job: dict[str, Any]) -> dict[str, Any]:
    """The required aggregate's one step, found without relying on its name."""
    steps = job.get("steps", [])
    assert len(steps) == 1, f"{AGGREGATE_JOB} should have exactly one step, has {len(steps)}"
    return steps[0]


def job_needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


class FixtureBrowserLegAssignmentTests(unittest.TestCase):
    def test_every_fixture_spec_runs_on_exactly_one_leg(self) -> None:
        self.assertEqual(leg_assignment_problems(load_workflow(), qa_spec_files()), [])

    def test_a_spec_removed_from_every_leg_is_reported(self) -> None:
        workflow = load_workflow()
        self.assertEqual(leg_assignment_problems(workflow, qa_spec_files()), [])
        mutated = copy.deepcopy(workflow)
        entry = leg_entries(mutated)[0]
        dropped = entry["specs"].split()[0]
        entry["specs"] = " ".join(entry["specs"].split()[1:])

        self.assertIn(
            f"{dropped} is not listed on any leg",
            leg_assignment_problems(mutated, qa_spec_files()),
        )

    def test_a_spec_listed_on_two_legs_is_reported(self) -> None:
        workflow = load_workflow()
        self.assertEqual(leg_assignment_problems(workflow, qa_spec_files()), [])
        mutated = copy.deepcopy(workflow)
        first, second = leg_entries(mutated)[:2]
        doubled = first["specs"].split()[0]
        second["specs"] = f"{second['specs']} {doubled}"

        self.assertIn(
            f"{doubled} is listed 2 times",
            leg_assignment_problems(mutated, qa_spec_files()),
        )

    def test_a_new_spec_file_on_no_leg_is_reported(self) -> None:
        files = sorted([*qa_spec_files(), "qa/new-fixture-check.spec.js"])

        self.assertEqual(
            leg_assignment_problems(load_workflow(), files),
            ["qa/new-fixture-check.spec.js is not listed on any leg"],
        )

    def test_a_missing_leg_or_a_manual_spec_on_a_leg_is_reported(self) -> None:
        mutated = copy.deepcopy(load_workflow())
        legs = leg_entries(mutated)
        removed = legs.pop()
        legs[0]["specs"] = f"{legs[0]['specs']} {removed['specs']} qa/ui-switching.spec.js"

        problems = leg_assignment_problems(mutated, qa_spec_files())
        self.assertIn(f"legs are {['1', '2', '3']}, expected exactly {list(EXPECTED_LEGS)}", problems)
        self.assertIn(
            "qa/ui-switching.spec.js is listed on a leg but live appliance QA, manual only",
            problems,
        )

    def test_a_filter_that_also_selects_another_spec_is_reported(self) -> None:
        files = sorted([*qa_spec_files(), "qa/extra/qa/offline-snapshot.spec.js"])
        mutated = copy.deepcopy(load_workflow())
        leg_entries(mutated)[0]["specs"] += " qa/extra/qa/offline-snapshot.spec.js"

        problems = leg_assignment_problems(mutated, files)
        self.assertIn(
            "qa/offline-snapshot.spec.js as a Playwright filter selects "
            "['qa/extra/qa/offline-snapshot.spec.js', 'qa/offline-snapshot.spec.js']",
            problems,
        )

    def test_the_primary_crawl_and_the_rest_of_ui_coverage_are_separate_specs(self) -> None:
        primary = (QA_DIR / "ui-coverage-primary-crawl.spec.js").read_text(encoding="utf-8")
        rest = (QA_DIR / "ui-coverage.spec.js").read_text(encoding="utf-8")
        title = "the crawler's layout, heat-map and contrast checks pass on the synthetic fixtures"

        self.assertEqual(primary.count(f'test("{title}"'), 1)
        self.assertNotIn(title, rest)
        self.assertEqual(len(re.findall(r'^test\(', primary, re.MULTILINE)), 1)
        self.assertEqual(len(re.findall(r'^test\(', rest, re.MULTILINE)), 10)
        legs = dict(leg_specs(load_workflow()))
        with_primary = [leg for leg, specs in legs.items() if "qa/ui-coverage-primary-crawl.spec.js" in specs]
        with_rest = [leg for leg, specs in legs.items() if "qa/ui-coverage.spec.js" in specs]
        self.assertEqual(len(with_primary), 1)
        self.assertEqual(len(with_rest), 1)
        self.assertNotEqual(with_primary, with_rest)


class FixtureBrowserLegJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow()
        self.legs = self.workflow["jobs"][LEG_JOB]

    def test_legs_run_in_parallel_on_standard_hosted_runners(self) -> None:
        strategy = self.legs["strategy"]

        self.assertEqual(self.legs["runs-on"], "ubuntu-latest")
        self.assertIs(strategy["fail-fast"], False)
        self.assertEqual(strategy["max-parallel"], len(EXPECTED_LEGS))
        self.assertEqual(set(strategy["matrix"]), {"include"})
        self.assertIn("route", job_needs(self.legs))
        self.assertEqual(self.legs["if"], "needs.route.outputs.run == 'true'")
        self.assertIn("${{ matrix.leg }}", self.legs["name"])
        self.assertNotIn("continue-on-error", self.legs)

    def test_legs_need_only_the_route_job(self) -> None:
        # Nothing the source gates produce is used by a leg, so the legs start
        # beside the unit shards; the required aggregate enforces the gates.
        self.assertEqual(job_needs(self.legs), ["route"])

    def test_each_leg_runs_only_its_own_spec_list_once(self) -> None:
        step = step_named(self.legs, LEG_RUN_STEP)

        self.assertEqual(step["env"]["LEG_SPECS"], "${{ matrix.specs }}")
        self.assertNotIn("${{", step["run"])
        self.assertIn('read -r -a specs <<< "$LEG_SPECS"', step["run"])
        playwright = [line.strip() for line in step["run"].splitlines() if "npx playwright test" in line]
        self.assertEqual(playwright, ['npx playwright test "${specs[@]}" --workers=1 --retries=0'])
        self.assertIn("git status --short", step["run"])
        self.assertNotIn("if", step)

    def test_smoke_and_artifact_aware_javascript_run_on_one_leg_only(self) -> None:
        smoke_legs = [entry["leg"] for entry in leg_entries(self.workflow) if entry.get("smoke") is True]
        self.assertEqual(len(smoke_legs), 1)
        for entry in leg_entries(self.workflow):
            self.assertIn(entry.get("smoke"), (True, False), entry["leg"])

        once_only = (
            "python scripts/check_public_demo_artifact.py public-demo",
            "python scripts/check_public_docs.py",
            "npx playwright test qa/public-demo.spec.js --retries=0",
            "node --test tests/js/*.test.js",
        )
        for command in once_only:
            with self.subTest(command=command):
                steps = [step for step in self.legs["steps"] if command in str(step.get("run", ""))]
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0].get("if"), "matrix.smoke")

    def test_failure_diagnostics_upload_only_on_failure_with_short_retention(self) -> None:
        uploads = [
            step for step in self.legs["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]

        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["if"], "failure()")
        self.assertLessEqual(int(uploads[0]["with"]["retention-days"]), 7)
        allowed = ("actions/checkout@", "actions/setup-python@", "actions/setup-node@", "actions/upload-artifact@")
        for step in self.legs["steps"]:
            if "uses" in step:
                self.assertTrue(str(step["uses"]).startswith(allowed), step["uses"])


class RequiredAggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow()
        self.aggregate = self.workflow["jobs"][AGGREGATE_JOB]

    def test_the_required_check_waits_for_every_leg_and_requires_success(self) -> None:
        needs = job_needs(self.aggregate)

        self.assertEqual(self.aggregate["name"], AGGREGATE_NAME)
        self.assertEqual(self.aggregate["runs-on"], "ubuntu-latest")
        self.assertEqual(sorted(needs), sorted(["route", LEG_JOB, *SOURCE_GATES]))
        self.assertEqual(len(needs), 4)
        # !cancelled() keeps it from being skipped when a leg or a source gate
        # fails or is skipped; the route check keeps a routed-off duplicate
        # run skipped.
        self.assertEqual(self.aggregate["if"], "${{ !cancelled() && needs.route.outputs.run == 'true' }}")
        self.assertNotIn("continue-on-error", self.aggregate)
        step = aggregate_check_step(self.aggregate)
        self.assertEqual(step.get("name"), AGGREGATE_STEP)
        self.assertEqual(
            step.get("env"),
            {name: f"${{{{ needs.{job}.result }}}}" for name, job in AGGREGATE_RESULT_ENV.items()},
        )
        self.assertNotIn("${{", step["run"])
        self.assertNotIn("shell", step)
        self.assertNotIn("continue-on-error", step)

    def test_the_aggregate_is_the_only_job_that_needs_a_source_gate(self) -> None:
        # Listed from the workflow, so a later edit cannot drop a gate from the
        # required check, or hang a new job on one, without failing here.
        jobs = self.workflow["jobs"]
        for gate in SOURCE_GATES:
            with self.subTest(gate=gate):
                dependents = sorted(name for name, job in jobs.items() if gate in job_needs(job))
                self.assertEqual(dependents, [AGGREGATE_JOB])
        self.assertEqual(job_needs(jobs["python-source"]), ["route", "python-unittest"])
        self.assertEqual(job_needs(jobs["javascript-source"]), ["route"])

    @unittest.skipIf(os.name == "nt", "runs the step's shell command with bash")
    def test_the_result_check_fails_unless_the_legs_and_both_source_gates_succeeded(self) -> None:
        script = aggregate_check_step(self.aggregate)["run"]
        for results in itertools.product(JOB_RESULTS, repeat=len(AGGREGATE_RESULT_ENV)):
            env = dict(zip(AGGREGATE_RESULT_ENV, results))
            expected = 0 if all(result == "success" for result in results) else 1
            with self.subTest(**env):
                # GitHub runs an unspecified Linux shell as `bash -e {0}`.
                completed = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env={"PATH": os.environ.get("PATH", ""), **env},
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected)

if __name__ == "__main__":
    unittest.main()
