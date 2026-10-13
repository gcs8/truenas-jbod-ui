"""The CI unittest shard table covers every test module, and CI runs every shard."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
sys.path.insert(0, str(ROOT))

from scripts import run_test_shard  # noqa: E402


def _write_test_module(tests_dir: Path, name: str, body: str) -> None:
    (tests_dir / f"{name}.py").write_text(body, encoding="utf-8")


class _SyntheticTestsDir:
    """A temporary tests directory whose modules never leak into later tests.

    Discovery imports a module by its bare name and puts the start directory on
    ``sys.path``; two tests using the same synthetic name from different
    directories would otherwise collide in ``sys.modules``.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        self._case = case
        self._names: list[str] = []
        self.path = Path(tempfile.mkdtemp(prefix="unittest-shards-"))

    def __enter__(self) -> "_SyntheticTestsDir":
        return self

    def __exit__(self, *_exc) -> None:
        for name in self._names:
            sys.modules.pop(name, None)
        while str(self.path) in sys.path:
            sys.path.remove(str(self.path))
        import shutil

        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, name: str, body: str) -> str:
        self._names.append(name)
        _write_test_module(self.path, name, body)
        return name


PASSING_MODULE = """
import unittest


class Passing(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""

FAILING_MODULE = """
import unittest


class Failing(unittest.TestCase):
    def test_fails(self):
        self.fail("synthetic failure")
"""

TWO_PASSING_TESTS_MODULE = """
import unittest


class Passing(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)
"""


class ShardTableTests(unittest.TestCase):
    def test_every_test_module_is_assigned_to_exactly_one_shard(self) -> None:
        self.assertEqual(run_test_shard.partition_problems(), [])

    def test_partition_guard_names_unassigned_duplicate_and_missing_modules(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            tests_dir = Path(raw_dir)
            for name in ("test_a", "test_b", "test_new"):
                _write_test_module(tests_dir, name, PASSING_MODULE)
            problems = run_test_shard.partition_problems(
                {"1": ("test_a", "test_b"), "2": ("test_b", "test_gone")},
                tests_dir,
            )

        self.assertEqual(
            problems,
            [
                "test_b is assigned to 2 shards",
                "test_gone is assigned but tests/test_gone.py does not exist",
                "tests/test_new.py is not assigned to any shard",
            ],
        )

    def test_run_refuses_a_drifted_partition_before_running_anything(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            results_dir = synthetic.path / "results"
            synthetic.write("test_drift_assigned", PASSING_MODULE)
            synthetic.write("test_drift_unassigned", PASSING_MODULE)
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = run_test_shard.run_shard(
                    "1",
                    shards={"1": ("test_drift_assigned",)},
                    tests_dir=synthetic.path,
                    results_dir=results_dir,
                    stream=io.StringIO(),
                )

            self.assertEqual(code, 2)
            self.assertIn("test_drift_unassigned.py is not assigned", stderr.getvalue())
            self.assertFalse(results_dir.exists())


class ShardRunTests(unittest.TestCase):
    def test_run_executes_the_shard_modules_and_records_the_outcome(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            results_dir = synthetic.path / "results"
            synthetic.write("test_outcome_pass", PASSING_MODULE)
            synthetic.write("test_outcome_fail", FAILING_MODULE)
            shards = {"1": ("test_outcome_pass",), "2": ("test_outcome_fail",)}

            passing = run_test_shard.run_shard(
                "1", shards=shards, tests_dir=synthetic.path, results_dir=results_dir, stream=io.StringIO()
            )
            failing = run_test_shard.run_shard(
                "2", shards=shards, tests_dir=synthetic.path, results_dir=results_dir, stream=io.StringIO()
            )

            self.assertEqual((passing, failing), (0, 1))
            first = json.loads(run_test_shard.result_path(results_dir, "1").read_text(encoding="utf-8"))
            second = json.loads(run_test_shard.result_path(results_dir, "2").read_text(encoding="utf-8"))

        self.assertEqual(first["modules"], ["test_outcome_pass"])
        self.assertEqual(first["test_counts"], {"test_outcome_pass": 1})
        self.assertEqual(second["test_counts"], {"test_outcome_fail": 1})
        self.assertEqual((first["tests_run"], first["was_successful"]), (1, True))
        self.assertEqual((second["tests_run"], second["failures"], second["was_successful"]), (1, 1, False))
        self.assertEqual(first["python_version"], run_test_shard.python_version_label())

    def test_run_attempt_receipts_are_distinct_and_record_identity(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_attempt", PASSING_MODULE),)}
            results = synthetic.path / "results"
            for attempt in (1, 2):
                self.assertEqual(run_test_shard.run_shard(
                    "1", shards=shards, tests_dir=synthetic.path, results_dir=results,
                    stream=io.StringIO(), run_attempt=attempt,
                ), 0)
            self.assertEqual(len(list(results.glob("*.json"))), 2)
            for attempt in (1, 2):
                payload = json.loads((results / f"shard-1-attempt-{attempt}.json").read_text())
                self.assertEqual(payload["run_attempt"], attempt)
            with mock.patch.object(run_test_shard, "build_suite_and_counts") as build:
                self.assertEqual(run_test_shard.run_shard(
                    "1", shards=shards, tests_dir=synthetic.path, results_dir=results,
                    stream=io.StringIO(), run_attempt=2,
                ), 2)
                build.assert_not_called()

    def test_run_rejects_invalid_attempt_before_execution(self) -> None:
        for attempt in (True, 0, -1, "2"):
            with self.subTest(attempt=attempt), mock.patch.object(run_test_shard, "build_suite_and_counts") as build:
                self.assertEqual(run_test_shard.run_shard("1", run_attempt=attempt), 2)
                build.assert_not_called()

    def test_cli_uses_ci_attempt_environment_and_allows_explicit_override(self) -> None:
        with mock.patch.dict(os.environ, {"SHARD_RUN_ATTEMPT": "2"}):
            parser = run_test_shard._build_parser()
            self.assertEqual(parser.parse_args(["run", "--shard", "1"]).run_attempt, 2)
            self.assertEqual(parser.parse_args(["run", "--shard", "1", "--run-attempt", "10"]).run_attempt, 10)

    def test_suite_is_loaded_in_discovery_form(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            synthetic.write("test_form_probe", PASSING_MODULE)
            suite = run_test_shard.build_suite(("test_form_probe",), synthetic.path)
            ids = [test.id() for test in _flatten(suite)]

        self.assertEqual(ids, ["test_form_probe.Passing.test_ok"])


class ShardVerifyTests(unittest.TestCase):
    def _results(self, results_dir: Path, shards: dict[str, tuple[str, ...]], **overrides) -> None:
        results_dir.mkdir(parents=True, exist_ok=True)
        for shard_id, modules in shards.items():
            payload = {
                "shard": shard_id,
                "python_version": "3.14",
                "modules": list(modules),
                "test_counts": {module: 3 for module in modules},
                "tests_run": 3,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "unexpected_successes": 0,
                "was_successful": True,
            }
            payload.update(overrides.get(shard_id, {}))
            run_test_shard.result_path(results_dir, shard_id).write_text(json.dumps(payload), encoding="utf-8")

    def test_verify_passes_only_with_one_green_result_per_shard(self) -> None:
        shards = {"1": ("test_a",), "2": ("test_b",)}
        with tempfile.TemporaryDirectory() as raw_dir:
            tests_dir = Path(raw_dir)
            for name in ("test_a", "test_b"):
                _write_test_module(tests_dir, name, PASSING_MODULE)
            results_dir = tests_dir / "results"

            self._results(results_dir, shards)
            expected_counts = {"test_a": 3, "test_b": 3}
            self.assertEqual(
                run_test_shard.verify_results(
                    results_dir,
                    "3.14",
                    shards=shards,
                    tests_dir=tests_dir,
                    expected_counts=expected_counts,
                ),
                [],
            )

            cases = {
                "missing": ({"1": shards["1"]}, {}, "shard 2: no result"),
                "failed": (shards, {"2": {"was_successful": False, "failures": 1}}, "shard 2: not successful"),
                "empty": (shards, {"1": {"tests_run": 0}}, "shard 1: ran no tests"),
                "wrong-python": (shards, {"1": {"python_version": "3.12"}}, "expected '3.14'"),
                "drifted-modules": (shards, {"2": {"modules": ["test_a"]}}, "module list differs"),
                "missing-counts": (shards, {"1": {"test_counts": None}}, "test_counts is not an object"),
            }
            for label, (present, overrides, expected) in cases.items():
                with self.subTest(case=label):
                    for path in results_dir.glob("*.json"):
                        path.unlink()
                    self._results(results_dir, present, **overrides)
                    problems = run_test_shard.verify_results(
                        results_dir,
                        "3.14",
                        shards=shards,
                        tests_dir=tests_dir,
                        expected_counts=expected_counts,
                    )
                    self.assertTrue(any(expected in problem for problem in problems), problems)

            self.assertIn(
                "results directory is missing",
                run_test_shard.verify_results(
                    tests_dir / "absent",
                    "3.14",
                    shards=shards,
                    tests_dir=tests_dir,
                    expected_counts=expected_counts,
                )[0],
            )

    def _attempt(self, results_dir, shards, shard_id, attempt, **overrides):
        self._results(results_dir, {shard_id: shards[shard_id]})
        source = run_test_shard.result_path(results_dir, shard_id)
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload.update(run_attempt=attempt, **overrides)
        source.unlink()
        target = results_dir / f"shard-{shard_id}-attempt-{attempt}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def _verify_attempts(self, tests_dir, shards):
        return run_test_shard.verify_results(
            tests_dir / "results", "3.14", shards=shards, tests_dir=tests_dir,
            expected_counts={module: 3 for modules in shards.values() for module in modules},
        )

    def test_latest_attempt_replaces_failure_and_keeps_unaffected_shards(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),),
                      "2": (synthetic.write("test_b", PASSING_MODULE),)}
            results = synthetic.path / "results"
            self._attempt(results, shards, "1", 2, was_successful=False, failures=1,
                          test_counts={"test_a": 2})
            self._attempt(results, shards, "2", 2)
            self._attempt(results, shards, "1", 10)
            paths = list(results.glob("*.json"))
            for order in (paths, list(reversed(paths))):
                with self.subTest(order=[p.name for p in order]), mock.patch.object(
                    Path, "rglob", return_value=iter(order)
                ):
                    self.assertEqual(self._verify_attempts(synthetic.path, shards), [])

    def test_latest_failed_attempt_cannot_fall_back_to_older_success(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            results = synthetic.path / "results"
            self._attempt(results, shards, "1", 1)
            self._attempt(results, shards, "1", 2, was_successful=False, failures=1)
            self.assertTrue(any("not successful" in p for p in self._verify_attempts(synthetic.path, shards)))

    def test_malformed_receipt_identity_fails_closed_even_with_newer_success(self) -> None:
        cases = [None, True, 0, -1, "1", 1.5, 3]
        for attempt in cases:
            with self.subTest(attempt=attempt), _SyntheticTestsDir(self) as synthetic:
                shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
                results = synthetic.path / "results"
                path = self._attempt(results, shards, "1", 1)
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["run_attempt"] = attempt
                path.write_text(json.dumps(payload), encoding="utf-8")
                self._attempt(results, shards, "1", 10)
                self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_receipt_shard_must_match_filename(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            self._attempt(synthetic.path / "results", shards, "1", 1, shard="2")
            self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_duplicate_same_attempt_in_separate_artifacts_is_ambiguous(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            results = synthetic.path / "results"
            first = self._attempt(results / "artifact-a", shards, "1", 1)
            second = results / "artifact-b" / first.name
            second.parent.mkdir()
            second.write_bytes(first.read_bytes())
            problems = self._verify_attempts(synthetic.path, shards)
            self.assertTrue(any("ambiguous" in p for p in problems), problems)

    def test_legacy_receipt_is_attempt_one_and_cannot_tie_explicit_attempt_one(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            results = synthetic.path / "results"
            self._attempt(results, shards, "1", 2)
            self._results(results, shards, **{"1": {"was_successful": False, "failures": 1}})
            self.assertEqual(self._verify_attempts(synthetic.path, shards), [])
            path = self._attempt(results, shards, "1", 1)
            (results / "shard-1.json").write_bytes(path.read_bytes())
            self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_unreadable_newest_receipt_cannot_fall_back_to_success(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            results = synthetic.path / "results"
            self._attempt(results, shards, "1", 1)
            (results / "shard-1-attempt-2.json").write_text("{", encoding="utf-8")
            self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_duplicate_json_keys_are_ambiguous_receipts(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            path = self._attempt(synthetic.path / "results", shards, "1", 1)
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace('"run_attempt": 1', '"run_attempt": 2, "run_attempt": 1'), encoding="utf-8")
            self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_invalid_attempt_filenames_and_newest_version_fail_closed(self) -> None:
        for filename in ("shard-1-attempt-0.json", "shard-1-attempt--1.json", "shard-1-attempt-x.json"):
            with self.subTest(filename=filename), _SyntheticTestsDir(self) as synthetic:
                shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
                results = synthetic.path / "results"
                path = self._attempt(results, shards, "1", 1)
                (results / filename).write_bytes(path.read_bytes())
                self.assertTrue(self._verify_attempts(synthetic.path, shards))
        with _SyntheticTestsDir(self) as synthetic:
            shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
            results = synthetic.path / "results"
            self._attempt(results, shards, "1", 1)
            self._attempt(results, shards, "1", 2, python_version="3.12")
            self.assertTrue(any("expected '3.14'" in p for p in self._verify_attempts(synthetic.path, shards)))

    def test_successful_shard_with_multiple_skipped_subtests_is_admitted(self) -> None:
        body = """
import unittest

class SkippedCases(unittest.TestCase):
    def test_cases(self):
        for index in range(3):
            with self.subTest(index=index):
                self.skipTest("synthetic unavailable feature")
"""
        for attempt in (None, 2):
            with self.subTest(attempt=attempt), _SyntheticTestsDir(self) as synthetic:
                module = synthetic.write("test_skipped_cases", body)
                shards = {"1": (module,)}
                results = synthetic.path / "results"
                self.assertEqual(
                    run_test_shard.run_shard(
                        "1", shards=shards, tests_dir=synthetic.path,
                        results_dir=results, stream=io.StringIO(), run_attempt=attempt,
                    ),
                    0,
                )
                receipt = json.loads(
                    run_test_shard.result_path(results, "1", attempt).read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["tests_run"], 1)
                self.assertEqual(receipt["skipped"], 3)
                self.assertIs(receipt["was_successful"], True)
                self.assertEqual(
                    run_test_shard.verify_results(
                        results, run_test_shard.python_version_label(), shards=shards,
                        tests_dir=synthetic.path, expected_counts={module: 1},
                    ),
                    [],
                )

    def test_success_flag_cannot_hide_invalid_outcome_counters(self) -> None:
        for override in ({"failures": 1}, {"errors": 1}, {"unexpected_successes": 1},
                         {"tests_run": True}, {"errors": -1}, {"skipped": "0"},
                         {"tests_run": 4}, {"skipped": -1}, {"skipped": True},
                         {"skipped": None}, {"skipped": 1.5}):
            with self.subTest(override=override), _SyntheticTestsDir(self) as synthetic:
                shards = {"1": (synthetic.write("test_a", PASSING_MODULE),)}
                self._attempt(synthetic.path / "results", shards, "1", 1, **override)
                self.assertTrue(self._verify_attempts(synthetic.path, shards))

    def test_verify_rejects_a_removed_test_method_while_the_module_remains(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            results_dir = synthetic.path / "results"
            synthetic.write("test_mutation", PASSING_MODULE)
            shards = {"1": ("test_mutation",)}
            self.assertEqual(
                run_test_shard.run_shard(
                    "1",
                    shards=shards,
                    tests_dir=synthetic.path,
                    results_dir=results_dir,
                    stream=io.StringIO(),
                ),
                0,
            )

            problems = run_test_shard.verify_results(
                results_dir,
                run_test_shard.python_version_label(),
                shards=shards,
                tests_dir=synthetic.path,
                expected_counts={"test_mutation": 2},
            )

        self.assertEqual(
            problems,
            ["test-count manifest: test_mutation lost 1 test (expected 2, found 1)"],
        )

    def test_verify_requires_additions_to_update_the_manifest(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            results_dir = synthetic.path / "results"
            synthetic.write("test_addition", TWO_PASSING_TESTS_MODULE)
            shards = {"1": ("test_addition",)}
            self.assertEqual(
                run_test_shard.run_shard(
                    "1",
                    shards=shards,
                    tests_dir=synthetic.path,
                    results_dir=results_dir,
                    stream=io.StringIO(),
                ),
                0,
            )

            problems = run_test_shard.verify_results(
                results_dir,
                run_test_shard.python_version_label(),
                shards=shards,
                tests_dir=synthetic.path,
                expected_counts={"test_addition": 1},
            )

        self.assertEqual(
            problems,
            ["test-count manifest: test_addition gained 1 test (expected 1, found 2); update the manifest"],
        )


class TestCountManifestTests(unittest.TestCase):
    def test_generator_writes_stable_sorted_module_counts(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            synthetic.write("test_z", PASSING_MODULE)
            synthetic.write("test_a", TWO_PASSING_TESTS_MODULE)
            manifest_path = synthetic.path / "counts.json"

            counts = run_test_shard.write_test_count_manifest(
                manifest_path,
                modules=("test_z", "test_a"),
                tests_dir=synthetic.path,
            )

            self.assertEqual(counts, {"test_a": 2, "test_z": 1})
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8")),
                {"schema": 1, "modules": {"test_a": 2, "test_z": 1}},
            )
            self.assertTrue(manifest_path.read_text(encoding="utf-8").endswith("\n"))

    def test_generator_refuses_import_failures_instead_of_recording_failed_tests(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            synthetic.write("test_broken", "import dependency_that_does_not_exist\n")
            manifest_path = synthetic.path / "counts.json"

            with self.assertRaisesRegex(ValueError, "test_broken failed discovery"):
                run_test_shard.write_test_count_manifest(
                    manifest_path,
                    modules=("test_broken",),
                    tests_dir=synthetic.path,
                )

            self.assertFalse(manifest_path.exists())

    def test_update_counts_refuses_a_module_level_skip(self) -> None:
        with _SyntheticTestsDir(self) as synthetic:
            synthetic.write(
                "test_skipped",
                "import unittest\nraise unittest.SkipTest('platform')\n"
                "class T(unittest.TestCase):\n    def test_a(self):\n        pass\n",
            )
            manifest_path = synthetic.path / "counts.json"

            with self.assertRaisesRegex(ValueError, "test_skipped skipped itself at import"):
                run_test_shard.write_test_count_manifest(
                    manifest_path,
                    modules=("test_skipped",),
                    tests_dir=synthetic.path,
                )

            self.assertFalse(manifest_path.exists())

    @unittest.skipIf(
        sys.platform == "win32",
        "full discovery imports POSIX-only modules excluded from the Windows portable suite",
    )
    def test_checked_in_manifest_matches_full_discovery(self) -> None:
        expected = run_test_shard.load_test_count_manifest()
        actual = run_test_shard.discovered_test_counts(run_test_shard.discovered_modules())

        self.assertEqual(run_test_shard.test_count_problems(expected, actual), [])


class CIShardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))

    def test_ci_runs_every_shard_on_both_python_versions(self) -> None:
        job = self.workflow["jobs"]["python-unittest"]

        self.assertEqual(job["strategy"]["matrix"]["python-version"], ["3.12", "3.14"])
        self.assertEqual(job["strategy"]["matrix"]["shard"], list(run_test_shard.SHARDS))
        self.assertIs(job["strategy"]["fail-fast"], False)
        self.assertIn("${{ matrix.python-version }}", job["name"])
        self.assertIn("${{ matrix.shard }}", job["name"])
        run_commands = [str(step.get("run") or "") for step in job["steps"]]
        self.assertTrue(
            any('run_test_shard.py run --shard "${{ matrix.shard }}"' in command for command in run_commands),
            run_commands,
        )
        upload = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertIn("${{ matrix.python-version }}", upload["with"]["name"])
        self.assertIn("${{ matrix.shard }}", upload["with"]["name"])

    def test_required_check_fails_closed_on_any_shard_outcome(self) -> None:
        gate = self.workflow["jobs"]["python-source"]

        self.assertIn("python-unittest", gate["needs"])
        self.assertIn("!cancelled()", gate["if"])
        self.assertIn("needs.route.outputs.run == 'true'", gate["if"])
        self.assertEqual(gate["strategy"]["matrix"]["python-version"], ["3.12", "3.14"])
        download = next(
            step for step in gate["steps"] if str(step.get("uses", "")).startswith("actions/download-artifact@")
        )
        self.assertIn("${{ matrix.python-version }}", download["with"]["pattern"])
        verify = next(step for step in gate["steps"] if "run_test_shard.py verify" in str(step.get("run") or ""))
        self.assertIn('--python-version "${{ matrix.python-version }}"', verify["run"])
        self.assertNotIn("if", verify, "the verify step must run unconditionally in the gate")

    def test_artifact_and_coverage_paths_are_attempt_scoped(self) -> None:
        shard_job = self.workflow["jobs"]["python-unittest"]
        self.assertEqual(shard_job["env"]["SHARD_RUN_ATTEMPT"], "${{ github.run_attempt }}")
        for step in shard_job["steps"]:
            if str(step.get("uses", "")).startswith("actions/upload-artifact@"):
                self.assertIn("${{ github.run_attempt }}", step["with"]["name"])
        coverage = next(s for s in shard_job["steps"] if "COVERAGE_FILE" in s.get("env", {}))
        self.assertIn("${{ github.run_attempt }}", coverage["env"]["COVERAGE_FILE"])
        gate = self.workflow["jobs"]["python-source"]
        download = next(s for s in gate["steps"] if "actions/download-artifact@" in s.get("uses", ""))
        self.assertIs(download["with"]["merge-multiple"], False)
        self.assertNotIn("github.run_attempt", download["with"]["pattern"],
                         "failed-only reruns need unaffected shards from earlier attempts")
        combine = next(s for s in gate["steps"] if "coverage combine" in str(s.get("run") or ""))
        self.assertIn("shard-results/*/.coverage.shard-*", combine["run"])
        upload = next(s for s in gate["steps"] if "actions/upload-artifact@" in s.get("uses", ""))
        self.assertIn("${{ github.run_attempt }}", upload["with"]["name"])

    def test_gate_cannot_reuse_old_receipt_when_rerun_fails_before_writing(self) -> None:
        gate = self.workflow["jobs"]["python-source"]
        status = next(s for s in gate["steps"] if s.get("env", {}).get("SHARD_JOB_RESULT"))
        self.assertEqual(status["env"]["SHARD_JOB_RESULT"], "${{ needs.python-unittest.result }}")
        self.assertIn('test "$SHARD_JOB_RESULT" = success', status["run"])
        self.assertNotIn("if", status)

    def test_report_only_coverage_runs_on_the_sysmon_capable_leg_and_is_combined_in_the_gate(self) -> None:
        shard_job = self.workflow["jobs"]["python-unittest"]
        gate = self.workflow["jobs"]["python-source"]
        coverage_step = next(step for step in shard_job["steps"] if "coverage run" in str(step.get("run") or ""))
        plain_step = next(step for step in shard_job["steps"] if str(step.get("run") or "").startswith("python scripts/run_test_shard.py run"))

        self.assertEqual(coverage_step["if"], "matrix.python-version == '3.14'")
        self.assertEqual(plain_step["if"], "matrix.python-version != '3.14'")
        self.assertEqual(coverage_step["env"]["COVERAGE_CORE"], "sysmon")
        combine = next(step for step in gate["steps"] if "coverage combine" in str(step.get("run") or ""))
        self.assertEqual(combine["if"], "matrix.python-version == '3.14'")
        self.assertIn("coverage xml -o coverage.xml", combine["run"])


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


if __name__ == "__main__":
    unittest.main()
