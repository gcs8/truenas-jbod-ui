from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import dev_check


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_NPM_SHIM = r"C:\Program Files\nodejs\npm.cmd"


class DevCheckPlanTests(unittest.TestCase):
    def test_posix_safe_plan_matches_source_level_ci_commands(self) -> None:
        plan = dev_check.build_plan(
            "safe",
            platform="linux",
            root=ROOT,
            python_executable="python",
            environment={},
            find_executable=lambda _name: None,
        )
        argv = [check.argv for check in plan.checks]

        self.assertIn(
            ("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"),
            argv,
        )
        self.assertIn(
            ("python", "-m", "compileall", "app", "admin_service", "history_service", "scripts", "tests"),
            argv,
        )
        self.assertIn(
            (
                "python",
                "-m",
                "ruff",
                "check",
                "app",
                "admin_service",
                "history_service",
                "scripts",
                "tests",
                "--select",
                "E4,E7,E9,F",
            ),
            argv,
        )
        self.assertIn(("git", "diff", "--check"), argv)
        self.assertIn(("npm", "run", "test:unit"), argv)
        self.assertIn(("python", "scripts/build_perf_baseline.py", "--check"), argv)
        self.assertEqual(
            [skip for skip in plan.skips if skip.name == "Prometheus alert rules"][0].reason,
            "promtool is not available; install it or set PROMTOOL_BINARY to run this gate",
        )

    def test_full_plan_uses_full_discovery_on_posix(self) -> None:
        plan = dev_check.build_plan(
            "full",
            platform="darwin",
            root=ROOT,
            python_executable="python3",
            environment={},
            find_executable=lambda _name: None,
        )

        self.assertEqual(
            plan.checks[0].argv,
            ("python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"),
        )
        self.assertFalse(any(skip.name.startswith("Windows exclusion:") for skip in plan.skips))

    def test_javascript_syntax_plan_covers_fixed_assets_and_all_qa_specs_dynamically(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "qa").mkdir()
            for relative in ("qa/z-last.spec.js", "qa/a-first.spec.js"):
                (root / relative).write_text("// fixture\n", encoding="utf-8")

            plan = dev_check.build_plan(
                "safe",
                platform="linux",
                root=root,
                python_executable="python",
                environment={},
                find_executable=lambda _name: None,
            )

        node_argv = [check.argv for check in plan.checks if check.argv[:2] == ("node", "--check")]
        self.assertEqual(
            node_argv,
            [
                ("node", "--check", "app/static/app.js"),
                ("node", "--check", "app/static/sas_fabric_view.js"),
                ("node", "--check", "admin_service/static/admin.js"),
                ("node", "--check", "history_service/static/dashboard.js"),
                ("node", "--check", "qa/a-first.spec.js"),
                ("node", "--check", "qa/z-last.spec.js"),
            ],
        )

    def test_missing_qa_specs_is_a_planning_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "qa").mkdir()

            with self.assertRaisesRegex(dev_check.PlanError, r"No QA spec files found under qa/\*\.spec\.js"):
                dev_check.build_plan(
                    "safe",
                    platform="linux",
                    root=root,
                    python_executable="python",
                    environment={},
                    find_executable=lambda _name: None,
                )

    def test_promtool_binary_environment_override_is_used(self) -> None:
        binary = "/opt/prometheus/promtool"
        plan = dev_check.build_plan(
            "safe",
            platform="linux",
            root=ROOT,
            python_executable="python",
            environment={"PROMTOOL_BINARY": binary},
            find_executable=lambda name: binary if name == binary else None,
        )

        self.assertIn(
            (
                binary,
                "check",
                "rules",
                "prometheus/rules/truenas-jbod-ui-alerts-v1.yml",
            ),
            [check.argv for check in plan.checks],
        )
        self.assertFalse(any(skip.name == "Prometheus alert rules" for skip in plan.skips))

    def test_windows_plan_uses_only_centrally_classified_portable_suites(self) -> None:
        plan = dev_check.build_plan(
            "full",
            platform="win32",
            root=ROOT,
            python_executable="python.exe",
            environment={},
            find_executable=lambda _name: None,
        )

        python_tests = plan.checks[0]
        self.assertEqual(python_tests.name, "Python unittest (Windows portable suite)")
        self.assertEqual(python_tests.argv[:4], ("python.exe", "-m", "unittest", "-v"))
        self.assertEqual(set(python_tests.argv[4:]), set(dev_check.WINDOWS_PORTABLE_TEST_MODULES))
        self.assertNotIn("tests.test_scheduled_backup", python_tests.argv)
        self.assertNotIn("tests.test_history_service", python_tests.argv)
        self.assertNotIn("tests.test_system_backup", python_tests.argv)

        exclusion_skips = [skip for skip in plan.skips if skip.name.startswith("Windows exclusion:")]
        self.assertEqual(len(exclusion_skips), len(dev_check.WINDOWS_EXCLUSIONS))
        rendered = "\n".join(f"{skip.name}: {skip.reason}" for skip in exclusion_skips)
        for exclusion in dev_check.WINDOWS_EXCLUSIONS:
            self.assertIn(exclusion.category, rendered)
            self.assertIn(exclusion.reason, rendered)
            for module in exclusion.modules:
                self.assertIn(module, rendered)

    def test_every_tracked_test_is_classified_for_windows(self) -> None:
        discovered = {
            f"tests.{path.stem}"
            for path in (ROOT / "tests").glob("test_*.py")
        }
        excluded = {
            module
            for exclusion in dev_check.WINDOWS_EXCLUSIONS
            for module in exclusion.modules
        }

        self.assertEqual(discovered, set(dev_check.WINDOWS_PORTABLE_TEST_MODULES) | excluded)
        self.assertEqual(set(dev_check.WINDOWS_PORTABLE_TEST_MODULES) & excluded, set())

    def test_windows_plan_fails_closed_when_test_classification_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "qa").mkdir()
            (root / "qa/smoke.spec.js").write_text("// fixture\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests/test_new_contract.py").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(dev_check.PlanError, "Windows test classification is stale"):
                dev_check.build_plan(
                    "full",
                    platform="win32",
                    root=root,
                    python_executable="python.exe",
                    environment={},
                    find_executable=lambda _name: None,
                )

    def test_wrapper_commands_remain_in_parity_with_ci_source_gates(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        plan = dev_check.build_plan(
            "safe",
            platform="linux",
            root=ROOT,
            python_executable="python",
            environment={"PROMTOOL_BINARY": "promtool"},
            find_executable=lambda name: name,
        )

        command_lines = {" ".join(check.argv) for check in plan.checks}
        expected_ci_commands = {
            'python -m unittest discover -s tests -p test_*.py -v',
            "python -m compileall app admin_service history_service scripts tests",
            "python -m ruff check app admin_service history_service scripts tests --select E4,E7,E9,F",
            "git diff --check",
            "npm run test:unit",
            "promtool check rules prometheus/rules/truenas-jbod-ui-alerts-v1.yml",
        }
        self.assertTrue(expected_ci_commands <= command_lines)
        workflow_equivalents = {
            "python -m ruff check app admin_service history_service scripts tests --select E4,E7,E9,F": (
                "ruff check app admin_service history_service scripts tests --select E4,E7,E9,F"
            ),
        }
        for command in expected_ci_commands - {'python -m unittest discover -s tests -p test_*.py -v'}:
            command = workflow_equivalents.get(command, command)
            self.assertIn(command, workflow)
        self.assertIn('python -m unittest discover -s tests -p "test_*.py" -v', workflow)
        for path in dev_check.FIXED_JAVASCRIPT_PATHS:
            self.assertIn(f"node --check {path}", workflow)
        self.assertIn("specs=(qa/*.spec.js)", workflow)
        planned_qa_specs = {
            check.argv[-1]
            for check in plan.checks
            if check.argv[:2] == ("node", "--check") and check.argv[-1].startswith("qa/")
        }
        discovered_qa_specs = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "qa").glob("*.spec.js")
        }
        self.assertEqual(planned_qa_specs, discovered_qa_specs)
        self.assertIn(
            "python scripts/build_perf_baseline.py --check",
            (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
        )

    def test_contributing_names_wrapper_as_tier_one_authority_on_posix_and_windows(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn("authoritative Tier 1 source-validation entrypoint", contributing)
        self.assertIn("python scripts/dev_check.py --safe", contributing)
        self.assertIn(r".\.venv\Scripts\python.exe scripts\dev_check.py --safe", contributing)
        self.assertIn("Raw command reference", contributing)
        self.assertIn("SKIP", contributing)

    def test_release_checklist_uses_truthful_platform_aware_full_gate(self) -> None:
        checklist = (ROOT / "docs/RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        self.assertIn("python scripts/dev_check.py --full", checklist)
        self.assertIn(r".\.venv\Scripts\python.exe scripts\dev_check.py --full", checklist)
        self.assertIn("Windows portable suite", checklist)
        self.assertIn("record every named `SKIP`", checklist)
        self.assertIn("does not claim POSIX `fcntl`", checklist)


class DevCheckExecutionTests(unittest.TestCase):
    def test_runner_aggregates_failures_and_prints_named_summary(self) -> None:
        plan = dev_check.Plan(
            checks=(
                dev_check.Check("passing check", ("tool", "pass")),
                dev_check.Check("failing check", ("tool", "fail")),
                dev_check.Check("later check", ("tool", "later")),
            ),
            skips=(dev_check.Skip("optional check", "tool unavailable"),),
        )
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 7 if argv[-1] == "fail" else 0)

        output = io.StringIO()
        result = dev_check.run_plan(plan, root=ROOT, runner=runner, output=output)

        self.assertEqual(result, 1)
        self.assertEqual([argv for argv, _kwargs in calls], [check.argv for check in plan.checks])
        self.assertTrue(all(kwargs["cwd"] == ROOT for _argv, kwargs in calls))
        self.assertTrue(all(kwargs["check"] is False for _argv, kwargs in calls))
        self.assertTrue(all(kwargs["shell"] is False for _argv, kwargs in calls))
        summary = output.getvalue()
        self.assertIn("PASS  passing check", summary)
        self.assertIn("FAIL  failing check (exit 7)", summary)
        self.assertIn("PASS  later check", summary)
        self.assertIn("SKIP  optional check: tool unavailable", summary)
        self.assertIn("FINAL: FAIL", summary)

    def test_runner_returns_success_when_checks_pass_and_skips_are_explicit(self) -> None:
        plan = dev_check.Plan(
            checks=(dev_check.Check("passing check", ("tool", "pass")),),
            skips=(dev_check.Skip("optional check", "not installed"),),
        )
        output = io.StringIO()

        result = dev_check.run_plan(
            plan,
            root=ROOT,
            runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0),
            output=output,
        )

        self.assertEqual(result, 0)
        self.assertIn("FINAL: PASS", output.getvalue())
        self.assertIn("SKIP  optional check: not installed", output.getvalue())

    def test_argument_parser_requires_exactly_one_explicit_mode(self) -> None:
        parser = dev_check.build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args([])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--safe", "--full"])
        self.assertEqual(parser.parse_args(["--safe"]).mode, "safe")
        self.assertEqual(parser.parse_args(["--full"]).mode, "full")


class DevCheckToolResolutionTests(unittest.TestCase):
    def _named_check(self, plan: dev_check.Plan, name: str) -> dev_check.Check:
        matches = [check for check in plan.checks if check.name == name]
        self.assertEqual(len(matches), 1, f"expected exactly one {name!r} check")
        return matches[0]

    def test_npm_is_resolved_through_the_executable_finder(self) -> None:
        plan = dev_check.build_plan(
            "safe",
            platform="win32",
            root=ROOT,
            python_executable="python.exe",
            environment={},
            find_executable=lambda name: WINDOWS_NPM_SHIM if name == "npm" else None,
        )

        check = self._named_check(plan, "JavaScript unit tests")
        self.assertEqual(check.argv, (WINDOWS_NPM_SHIM, "run", "test:unit"))
        self.assertIsNone(check.missing_tool)

    def test_windows_falls_back_to_the_cmd_shim_when_the_bare_name_is_unresolvable(self) -> None:
        plan = dev_check.build_plan(
            "safe",
            platform="win32",
            root=ROOT,
            python_executable="python.exe",
            environment={},
            find_executable=lambda name: WINDOWS_NPM_SHIM if name == "npm.cmd" else None,
        )

        check = self._named_check(plan, "JavaScript unit tests")
        self.assertEqual(check.argv, (WINDOWS_NPM_SHIM, "run", "test:unit"))
        self.assertIsNone(check.missing_tool)

    def test_default_executable_finder_is_shutil_which(self) -> None:
        with mock.patch.object(dev_check.shutil, "which", side_effect=lambda name: f"/resolved/{name}") as which:
            plan = dev_check.build_plan(
                "safe",
                platform="linux",
                root=ROOT,
                python_executable="python",
                environment={},
            )

        self.assertIn("npm", [call.args[0] for call in which.call_args_list])
        self.assertEqual(
            self._named_check(plan, "JavaScript unit tests").argv,
            ("/resolved/npm", "run", "test:unit"),
        )

    def test_unresolvable_tool_becomes_a_named_failing_check_not_an_exception(self) -> None:
        plan = dev_check.build_plan(
            "safe",
            platform="linux",
            root=ROOT,
            python_executable="python",
            environment={},
            find_executable=lambda _name: None,
        )

        check = self._named_check(plan, "JavaScript unit tests")
        self.assertEqual(check.missing_tool, "npm")
        self.assertEqual(check.argv, ("npm", "run", "test:unit"))

        def runner(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise AssertionError(f"unresolvable tool must not be dispatched: {argv}")

        output = io.StringIO()
        result = dev_check.run_plan(
            dev_check.Plan(checks=(check,)),
            root=ROOT,
            runner=runner,
            output=output,
        )

        summary = output.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("FAIL  JavaScript unit tests: tool not found: npm", summary)
        self.assertNotIn("Traceback", summary)
        self.assertIn("FINAL: FAIL", summary)


if __name__ == "__main__":
    unittest.main()
