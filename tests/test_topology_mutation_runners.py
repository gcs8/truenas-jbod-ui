"""The Storage Fabric Topology mutation runners judge only what they ran (#1516).

A mutant counts as killed only when a collected test fails on the mutated
behaviour: an assertion, or an exception thrown from the product. A missing
module or import error (even inside a test that ran), a class or module
fixture error, an exception from test or harness code (even beside a test
that a kill would count), a hook failure, no test count, a missing anchor
or a missing target file is an error that fails the run, never a kill; so
is an unmutated run that reads a file outside the CI job's path filter.
Each case runs the real runner on a disposable tree with its own tiny
module and tests: a deliberately broken harness, and a deliberately broken
target, in each direction.
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
        (self.tree / ".github" / "workflows").mkdir(parents=True)
        (self.tree / ".github" / "workflows" / "ci.yml").write_text("          pattern='^(selftarget\\.py|selftests/.*|\\.github/.*)$'\n", encoding="utf-8")
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

    def test_an_exception_thrown_from_the_mutated_product_is_a_kill(self) -> None:
        self.assertEqual(self.judge("    return 1\n", "    return None + 1\n")["verdict"], "killed")

    def test_tests_that_only_error_in_test_code_are_an_error_not_a_kill(self) -> None:
        (self.tree / "selftests" / "test_value.py").write_text(TESTS.replace("self.assertEqual(value(), 1)", "self.assertEqual(value() + 0, 1)"), encoding="utf-8")
        result = self.judge("    return 1\n", "    return \"x\"\n")
        self.assertEqual(result["verdict"], "error", result)
        self.assertIn("from test code", result["detail"])

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


    def test_an_exception_the_product_raises_through_the_standard_library_is_a_kill(self) -> None:
        # The innermost frame is the json module; the innermost one inside
        # the tree is the mutated product.
        self.assertEqual(self.judge("    return 1\n", "    import json\n    return json.loads('not json')\n")["verdict"], "killed")

    def test_per_test_fixtures_do_not_kill(self) -> None:
        for phase in ("setUp", "tearDown", "cleanup"):
            for mutation in ("return 2", "raise ValueError('broken')"):
                with self.subTest(phase=phase, mutation=mutation):
                    setup = "    def setUp(self):\n        self.addCleanup(self.cleanup)\n" if phase == "cleanup" else ""
                    source = TESTS.replace("    def test_value(self):", setup + f"    def {phase}(self):\n        self.assertEqual(value(), 1)\n    def test_value(self):").replace("def test_value(self):\n        self.assertEqual(value(), 1)", "def test_value(self):\n        self.assertTrue(True)")
                    (self.tree / "selftests" / "test_value.py").write_text(source, encoding="utf-8")
                    result = self.judge("    return 1\n", f"    {mutation}\n")
                    self.assertEqual(result["verdict"], "error", result)

    def test_a_skipped_only_module_is_not_an_exercised_baseline(self) -> None:
        modules = [*self.modules, "selftests.test_skipped"]
        (self.tree / "selftests" / "test_skipped.py").write_text(TESTS.replace("    def test_value", "    @unittest.skip('not exercised')\n    def test_value"), encoding="utf-8")
        code, stderr = self.runner._run_modules(self.tree, modules, verbose=True)
        self.assertIn("selftests.test_skipped ran no test", self.runner.stock_problems(code, stderr, modules))

    def test_a_class_fixture_that_calls_the_broken_target_is_an_error_not_a_kill(self) -> None:
        # The target raises inside setUpClass: its class's test never runs,
        # and no collected test fails, though other tests pass.
        (self.tree / "selftests" / "test_value.py").write_text(
            "import unittest\n\nfrom selftarget import untested, value\n\n\n"
            "class Setup(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        cls.value = value()\n\n"
            "    def test_value(self):\n        self.assertEqual(self.value, 1)\n\n\n"
            "class Other(unittest.TestCase):\n    def test_other(self):\n        self.assertEqual(untested(), 3)\n", encoding="utf-8")
        result = self.judge("    return 1\n", "    raise ValueError(\"broken target\")\n")
        self.assertEqual(result["verdict"], "error", result)
        self.assertIn("fixture", result["detail"])

    def test_a_test_code_exception_beside_an_assertion_failure_is_an_error(self) -> None:
        # The broken target fails one test's assertion; the harness of
        # another test raises on its own. The run must not pass as a kill.
        (self.tree / "selftests" / "test_value.py").write_text(TESTS + (
            "\n    def test_fixture(self):\n        fixture = type('Fixture', (), {'value': 1}) if value() == 1 else None\n        self.assertEqual(fixture.value, 1)\n"), encoding="utf-8")
        result = self.judge("    return 1\n", "    return 2\n")
        self.assertEqual(result["verdict"], "error", result)
        self.assertIn("beside 1", result["detail"])

    def test_the_unmutated_run_fails_on_a_read_outside_the_ci_path_filter(self) -> None:
        trace = self.tree / "reads.log"
        code, stderr = self.runner._run_modules(self.tree, self.modules, verbose=True, trace=trace)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.runner.scope_problems(self.tree, trace), [])
        # A test that reads a file the filter misses, opened and given to a
        # child process stripped of the tracer's path.
        (self.tree / "docs").mkdir()
        (self.tree / "docs" / "outside.txt").write_text("x\n", encoding="utf-8")
        (self.tree / "docs" / "child.py").write_text("print(1)\n", encoding="utf-8")
        (self.tree / "selftests" / "test_reads.py").write_text(
            "import os\nimport subprocess\nimport sys\nimport unittest\nfrom pathlib import Path\n\n\n"
            "class Reads(unittest.TestCase):\n    def test_reads(self):\n"
            "        self.assertEqual(Path('docs/outside.txt').read_text(), 'x\\n')\n"
            "        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}\n"
            "        subprocess.run([sys.executable, 'docs/child.py'], env=env, check=True, capture_output=True)\n", encoding="utf-8")
        trace.unlink()
        code, stderr = self.runner._run_modules(self.tree, [*self.modules, "selftests.test_reads"], verbose=True, trace=trace)
        self.assertEqual(code, 0, stderr)
        problems = self.runner.scope_problems(self.tree, trace)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("docs/child.py", problems[0])
        self.assertIn("docs/outside.txt", problems[1])
        # The filter itself missing is no pass.
        (self.tree / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.runner.scope_problems(self.tree, trace)


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
        shutil.copytree(MUTATION / "read_trace", self.tree / "tests" / "mutation" / "read_trace")
        (self.tree / "app" / "static").mkdir(parents=True)
        (self.tree / "app" / "static" / "sas_fabric_topology_selftest.js").write_text(
            "module.exports = { value: () => 1, other: () => 3 };\n", encoding="utf-8")
        (self.tree / "tests" / "js").mkdir()
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value(), 1);")
        self.write_test("fabric_linux_sas_transport.test.js", "assert.equal(1, 1);")
        self.lists({"stages": [["tests/js/fabric_topology_selftest.test.js", "tests/js/fabric_linux_sas_transport.test.js"]]}, {})
        (self.tree / ".github" / "workflows").mkdir(parents=True)
        (self.tree / ".github" / "workflows" / "ci.yml").write_text("          pattern='^(app/.*|tests/.*|\\.github/.*)$'\n", encoding="utf-8")

    def write_test(self, name: str, body: str, top: str = "") -> None:
        # The product is run by path, as the topology helper runs scripts.
        (self.tree / "tests" / "js" / name).write_text(
            "\"use strict\";\nconst assert = require(\"node:assert/strict\");\nconst test = require(\"node:test\");\n"
            "const fs = require(\"node:fs\");\nconst path = require(\"node:path\");\nconst vm = require(\"node:vm\");\n"
            "const load = () => { const file = path.join(__dirname, \"..\", \"..\", \"app\", \"static\", \"sas_fabric_topology_selftest.js\");"
            " const module = { exports: {} }; vm.runInNewContext(fs.readFileSync(file, \"utf8\"), { module, require }, { filename: file }); return module.exports; };\n"
            f"{top}\ntest(\"selftest\", () => {{ {body} }});\n", encoding="utf-8")

    def lists(self, tests: dict, mutants: dict) -> None:
        (self.tree / "tests" / "mutation" / "topology_frontend_tests.json").write_text(json.dumps(tests), encoding="utf-8")
        (self.tree / "tests" / "mutation" / "topology_frontend_mutants.json").write_text(json.dumps(mutants), encoding="utf-8")
        (self.tree / "tests" / "mutation" / "topology_equivalent_mutants.json").write_text(json.dumps({"frontend": {}, "backend": {}}), encoding="utf-8")

    def run_runner(self, mutants: dict, only: str, *flags: str) -> tuple[int, str]:
        tests = json.loads((self.tree / "tests" / "mutation" / "topology_frontend_tests.json").read_text(encoding="utf-8"))
        self.lists(tests, mutants)
        result = subprocess.run([shutil.which("node") or "node", "tests/mutation/run_topology_frontend_mutants.cjs", "--only", only, "--jobs", "1", *flags],
                                cwd=self.tree, capture_output=True, text=True, timeout=300, env={**os.environ, "NODE_OPTIONS": ""})
        return result.returncode, result.stdout + result.stderr

    def test_an_assertion_failure_is_a_kill(self) -> None:
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"m\s+killed")

    def test_an_exception_thrown_from_the_mutated_product_is_a_kill(self) -> None:
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => null.x"}}, "m")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"m\s+killed")

    def test_a_test_file_that_does_not_load_or_a_test_code_exception_is_an_error(self) -> None:
        # The product loaded at the test file's top level, failing: the
        # file is not collected.
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(lib.value(), 1);", top="const lib = load();")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: (() => { throw new Error(\"boom\"); })()"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"m\s+error")
        # An exception thrown by the test's own code is not the mutant's doing.
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value().toFixed(0), \"1\");")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => null"}}, "m")
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

    def test_an_exception_from_an_unnamed_script_a_test_evaluates_is_an_error_not_a_kill(self) -> None:
        # The broken target returns null; the test's own helper evaluates
        # an unnamed script with it, which throws. No product frame threw.
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(fixtureOf(load().value()), undefined);",
                        top="const fixtureOf = (value) => vm.runInNewContext(`(${JSON.stringify(value)}).fixture`);")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => null"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"m\s+error")
        self.assertIn("neither an assertion nor a product exception", out)

    def test_a_harness_exception_beside_an_assertion_failure_is_an_error(self) -> None:
        # One test fails its assertion on the broken target; another test's
        # own fixture raises. The run must not pass as a kill.
        (self.tree / "tests" / "js" / "fabric_topology_selftest.test.js").write_text(
            "\"use strict\";\nconst assert = require(\"node:assert/strict\");\nconst test = require(\"node:test\");\n"
            "const fs = require(\"node:fs\");\nconst path = require(\"node:path\");\nconst vm = require(\"node:vm\");\n"
            "const load = () => { const file = path.join(__dirname, \"..\", \"..\", \"app\", \"static\", \"sas_fabric_topology_selftest.js\");"
            " const module = { exports: {} }; vm.runInNewContext(fs.readFileSync(file, \"utf8\"), { module, require }, { filename: file }); return module.exports; };\n"
            "test(\"selftest\", () => { assert.equal(load().value(), 1); });\n"
            "test(\"fixture\", () => { const fixture = load().value() === 1 ? {} : null; assert.equal(fixture.missing, undefined); });\n", encoding="utf-8")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"m\s+error")
        self.assertIn("beside 1 that did", out)

    def test_a_hook_that_fails_is_an_error_not_a_kill(self) -> None:
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().other(), 3);",
                        top="test.before(() => { if (load().value() !== 1) throw new Error(\"hook\"); });")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"m\s+error")

    def test_an_exception_the_product_throws_through_a_native_frame_is_a_kill(self) -> None:
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => JSON.parse(\"not json\")"}}, "m")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"m\s+killed")

    def test_the_unmutated_run_fails_on_a_read_outside_the_ci_path_filter(self) -> None:
        (self.tree / "docs").mkdir()
        (self.tree / "docs" / "outside.json").write_text("{}\n", encoding="utf-8")
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value(), 1); assert.deepEqual(JSON.parse(fs.readFileSync(path.join(__dirname, \"..\", \"..\", \"docs\", \"outside.json\"), \"utf8\")), {});")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("the tests read docs/outside.json, which the CI path filter of this job does not cover", out)
        # Read by a child process the test starts, without the tracer.
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value(), 1); require(\"node:child_process\").execFileSync(process.execPath, [path.join(__dirname, \"..\", \"..\", \"docs\", \"child.js\")], { env: {} });")
        (self.tree / "docs" / "outside.json").unlink()
        (self.tree / "docs" / "child.js").write_text("process.exit(0);\n", encoding="utf-8")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("the tests read docs/child.js", out)
        # The filter itself missing is no pass.
        (self.tree / "docs" / "child.js").unlink()
        self.write_test("fabric_topology_selftest.test.js", "assert.equal(load().value(), 1);")
        (self.tree / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("CI path filter of the topology mutation job is not found", out)

    def test_python_child_data_must_be_covered_by_the_ci_filter(self) -> None:
        import sys

        (self.tree / "docs").mkdir()
        (self.tree / "tests" / "reader.py").write_text("from pathlib import Path\nprint(Path('docs/count.txt').read_text())\n", encoding="utf-8")
        (self.tree / "docs" / "count.txt").write_text("7", encoding="utf-8")
        self.write_test("fabric_topology_selftest.test.js", f"assert.equal(require('node:child_process').execFileSync({json.dumps(sys.executable)}, ['tests/reader.py']).toString().trim(), '7'); assert.equal(load().value(), 1);")
        mutant = {"m": {"from": "value: () => 1", "to": "value: () => 2"}}
        code, out = self.run_runner(mutant, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("the tests read docs/count.txt", out)
        if shutil.which("strace"):
            # The kernel sees the data file even in an isolated Python
            # interpreter which ignores sitecustomize and its parent env.
            self.write_test("fabric_topology_selftest.test.js", f"assert.equal(require('node:child_process').execFileSync({json.dumps(sys.executable)}, ['-I', 'tests/reader.py'], {{env: {{}}}}).toString().trim(), '7'); assert.equal(load().value(), 1);")
            code, out = self.run_runner(mutant, "m", "--trace-opens")
            self.assertEqual(code, 1, out)
            self.assertIn("the tests read docs/count.txt", out)
        workflow = self.tree / ".github" / "workflows" / "ci.yml"
        workflow.write_text(workflow.read_text().replace("app/.*|", "docs/count[.]txt|app/.*|"), encoding="utf-8")
        code, out = self.run_runner(mutant, "m", *(["--trace-opens"] if shutil.which("strace") else []))
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"m\s+killed")

    def test_a_topology_test_file_the_list_does_not_name_fails_the_run(self) -> None:
        self.write_test("fabric_topology_unlisted.test.js", "assert.equal(1, 1);")
        code, out = self.run_runner({"m": {"from": "value: () => 1", "to": "value: () => 2"}}, "m")
        self.assertEqual(code, 1, out)
        self.assertIn("not in the stage list", out)


if __name__ == "__main__":
    unittest.main()
