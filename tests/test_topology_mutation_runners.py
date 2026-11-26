"""The Storage Fabric Topology mutation runners judge only what they ran (#1516).

A mutant counts as killed only when an assertion fails in a collected test.
A missing module or import error (even inside a test that ran), a run whose
tests only error, no test count, a missing anchor or a missing target file is
an error that fails the run, never a kill. Each case runs the real runner on
a disposable tree with its own tiny module and tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATION = ROOT / "tests" / "mutation"


def _backend():
    spec = importlib.util.spec_from_file_location("topology_backend_mutants", MUTATION / "run_topology_backend_mutants.py")
    if spec is None or spec.loader is None:
        raise ImportError("the backend mutation runner is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TARGET = "def value():\n    return 1\n\n\ndef untested():\n    return 3\n"
TESTS = (
    "import unittest\n\nfrom selftarget import value\n\n\n"
    "class ValueTests(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), 1)\n"
)


class BackendRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = _backend()
        self.tree = Path(tempfile.mkdtemp(prefix="topology-runner-selftest-"))
        self.addCleanup(shutil.rmtree, self.tree, True)
        (self.tree / "selftarget.py").write_text(TARGET, encoding="utf-8")
        (self.tree / "selftests").mkdir()
        (self.tree / "selftests" / "__init__.py").write_text("", encoding="utf-8")
        (self.tree / "selftests" / "test_value.py").write_text(TESTS, encoding="utf-8")
        self.modules = ["selftests.test_value"]

    def judge(self, old: str, new: str, path: str = "selftarget.py") -> dict:
        return self.runner._judge(self.tree, "case", {"case": (path, old, new)}, self.modules)

    def test_an_assertion_failure_is_a_kill(self) -> None:
        self.assertEqual(self.judge("    return 1\n", "    return 2\n")["verdict"], "killed")

    def test_an_assertion_failure_inside_a_subtest_is_a_kill(self) -> None:
        (self.tree / "selftests" / "test_value.py").write_text(TESTS.replace(
            "        self.assertEqual(value(), 1)\n",
            "        for mode in (\"a\", \"b\"):\n            with self.subTest(mode=mode):\n                self.assertEqual(value(), 1)\n"), encoding="utf-8")
        self.assertEqual(self.judge("    return 1\n", "    return 2\n")["verdict"], "killed")

    def test_a_missing_module_inside_a_test_that_ran_is_an_error_not_a_kill(self) -> None:
        result = self.judge("    return 1\n", "    import absent_selftest_fixture  # noqa: F401\n    return 1\n")
        self.assertEqual(result["verdict"], "error", result)
        self.assertIn("import", result["detail"])

    def test_tests_that_only_error_are_an_error_not_a_kill(self) -> None:
        result = self.judge("    return 1\n", "    return None + 1\n")
        self.assertEqual(result["verdict"], "error", result)
        self.assertIn("no assertion failed", result["detail"])

    def test_a_change_no_test_observes_survives(self) -> None:
        self.assertEqual(self.judge("    return 3\n", "    return 4\n")["verdict"], "survived")

    def test_a_missing_anchor_or_target_is_an_error(self) -> None:
        self.assertEqual(self.judge("not in the file", "x")["verdict"], "error")
        self.assertEqual(self.judge("    return 1\n", "    return 2\n", path="absent.py")["verdict"], "error")

    def test_a_run_with_no_test_count_is_an_error_and_a_module_with_no_test_is_no_baseline(self) -> None:
        self.assertEqual(self.runner.classify(5, "\n----------------------------------------------------------------------\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n")[0], "error")
        (self.tree / "selftests" / "test_empty.py").write_text("import unittest\n", encoding="utf-8")
        code, stderr = self.runner._run_modules(self.tree, ["selftests.test_value", "selftests.test_empty"], verbose=True)
        self.assertIn("selftests.test_empty ran no test", self.runner.stock_problems(code, stderr, ["selftests.test_value", "selftests.test_empty"]))
        code, stderr = self.runner._run_modules(self.tree, ["selftests.test_value"], verbose=True)
        self.assertEqual(self.runner.stock_problems(code, stderr, ["selftests.test_value"]), [])


class FrontendRunnerTests(unittest.TestCase):
    """The frontend runner on a disposable tree: one topology script, a
    required test list, one hook. A kill needs a failing assertion in a real
    test; a load failure, a missing module, a test file with no test and a
    required file that is missing fail the run."""

    def setUp(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        self.tree = Path(tempfile.mkdtemp(prefix="topology-frontend-selftest-"))
        self.addCleanup(shutil.rmtree, self.tree, True)
        (self.tree / "tests" / "mutation").mkdir(parents=True)
        for name in ("run_topology_frontend_mutants.cjs", "topology_frontend_hook.cjs"):
            shutil.copy2(MUTATION / name, self.tree / "tests" / "mutation" / name)
        (self.tree / "app" / "static").mkdir(parents=True)
        (self.tree / "app" / "static" / "sas_fabric_topology_selftest.js").write_text(
            "module.exports = { value: () => 1, other: () => 3 };\n", encoding="utf-8")
        (self.tree / "tests" / "js").mkdir()
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value(), 1);")
        self.write_test("fabric_linux_sas_transport.test.js", "assert.equal(1, 1);")
        self.lists({"stages": [["tests/js/fabric_topology_selftest.test.js", "tests/js/fabric_linux_sas_transport.test.js"]]}, {})

    def write_test(self, name: str, body: str) -> None:
        (self.tree / "tests" / "js" / name).write_text(
            "\"use strict\";\nconst assert = require(\"node:assert/strict\");\nconst test = require(\"node:test\");\n"
            "const fs = require(\"node:fs\");\nconst path = require(\"node:path\");\n"
            "const load = () => { const file = path.join(__dirname, \"..\", \"..\", \"app\", \"static\", \"sas_fabric_topology_selftest.js\");"
            " const module = { exports: {} }; new Function(\"module\", fs.readFileSync(file, \"utf8\"))(module); return module.exports; };\n"
            f"test(\"selftest\", () => {{ {body} }});\n", encoding="utf-8")

    def lists(self, tests: dict, mutants: dict) -> None:
        (self.tree / "tests" / "mutation" / "topology_frontend_tests.json").write_text(json.dumps(tests), encoding="utf-8")
        (self.tree / "tests" / "mutation" / "topology_frontend_mutants.json").write_text(json.dumps(mutants), encoding="utf-8")
        (self.tree / "tests" / "mutation" / "topology_equivalent_mutants.json").write_text(json.dumps({"frontend": {}, "backend": {}}), encoding="utf-8")

    def run_runner(self, mutants: dict, only: str) -> tuple[int, str]:
        tests = json.loads((self.tree / "tests" / "mutation" / "topology_frontend_tests.json").read_text(encoding="utf-8"))
        self.lists(tests, mutants)
        result = subprocess.run([shutil.which("node") or "node", "tests/mutation/run_topology_frontend_mutants.cjs", "--only", only, "--jobs", "1"],
                                cwd=self.tree, capture_output=True, text=True, timeout=300, env={**os.environ, "NODE_OPTIONS": ""})
        return result.returncode, result.stdout + result.stderr

    def test_an_assertion_failure_is_a_kill(self) -> None:
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"m\s+killed")

    def test_a_load_failure_or_a_crash_without_an_assertion_is_an_error(self) -> None:
        for label, to in (("load", "value: (() => { throw new Error(\"boom\"); })()"), ("crash", "value: () => null.x")):
            with self.subTest(label):
                code, out = self.run_runner({"m": {"from": "value: () => 1", "to": to}}, "m")
                self.assertEqual(code, 1, out)
                self.assertRegex(out, r"m\s+error")

    def test_a_missing_module_inside_a_test_is_an_error(self) -> None:
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => require(\"absent-selftest-module\")"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"m\s+error")

    def test_a_required_test_file_missing_or_without_tests_fails_the_run(self) -> None:
        (self.tree / "tests" / "js" / "fabric_linux_sas_transport.test.js").write_text("// no test\n", encoding="utf-8")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("runs no test of its own", out)
        (self.tree / "tests" / "js" / "fabric_linux_sas_transport.test.js").unlink()
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("missing", out)

    def test_a_topology_test_file_the_list_does_not_name_fails_the_run(self) -> None:
        self.write_test("fabric_topology_unlisted.test.js", "assert.equal(1, 1);")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("not in the stage list", out)


if __name__ == "__main__":
    unittest.main()
