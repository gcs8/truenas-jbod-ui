"""CI proves the shards ran exactly the discovered test identities, with no count file.

Synthetic controls for the four ways a count-only check is fooled: a test no
shard ran, a test that ran twice, a same-count substitution, and a same-count
deletion plus addition in source (which only a comparison with the base finds).
"""

from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_test_shard.py"
sys.path.insert(0, str(ROOT))

from scripts import run_test_shard  # noqa: E402

BINDING = {
    "python_version": run_test_shard.python_version_label(),
    "source_commit": "c" * 40,
    "run_id": "123",
}


def _two_tests(second: str) -> str:
    return (
        "import unittest\n\n\n"
        "class M(unittest.TestCase):\n"
        "    def test_a(self):\n        pass\n\n"
        f"    def test_{second}(self):\n        pass\n"
    )


ONE_TEST = "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_x(self):\n        pass\n"


class _Tree:
    """A throwaway tests directory whose bare-name modules never leak."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="unittest-ids-"))
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.results = self.root / "results"
        self._names: set[str] = set()

    def __enter__(self) -> "_Tree":
        return self

    def __exit__(self, *_exc) -> None:
        self.forget()
        while str(self.tests) in sys.path:
            sys.path.remove(str(self.tests))
        shutil.rmtree(self.root, ignore_errors=True)

    def forget(self) -> None:
        for name in self._names:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()

    def write(self, name: str, body: str) -> str:
        self.forget()
        self._names.add(name)
        path = self.tests / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        for cached in self.tests.glob(f"__pycache__/{name}.*"):
            cached.unlink()
        return name

    def binding(self, shards) -> dict[str, str]:
        return {**BINDING, "partition_fingerprint": run_test_shard.partition_fingerprint(shards)}

    def inventory(self, shards) -> dict:
        self.forget()
        return run_test_shard.build_inventory(self.tests, binding=self.binding(shards))

    def run(self, shard_id, shards, **kwargs) -> int:
        self.forget()
        return run_test_shard.run_shard(
            shard_id, shards=shards, tests_dir=self.tests, results_dir=self.results,
            stream=io.StringIO(), binding=self.binding(shards), **kwargs,
        )

    def verify(self, inventory, shards) -> list[str]:
        return run_test_shard.verify_results(
            self.results, BINDING["python_version"], inventory, shards=shards, tests_dir=self.tests
        )

    def receipt(self, shard_id: str) -> Path:
        return run_test_shard.result_path(self.results, shard_id)


def _rewrite(path: Path, **changes) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload), encoding="utf-8")


class SyntheticControlTests(unittest.TestCase):
    def test_a_discovered_test_that_no_shard_ran_fails_and_is_named(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_om", _two_tests("b")),)}
            inventory = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 0)
            self.assertEqual(tree.verify(inventory, shards), [])
            only_a = ["test_om.M.test_a"]
            _rewrite(tree.receipt("1"), collected_ids=only_a, started_ids=only_a,
                     completed_ids=only_a, tests_run=1)

            problems = tree.verify(inventory, shards)

        self.assertIn("test identities that no shard completed (1): test_om.M.test_b", problems)

    def test_an_omission_hidden_by_a_repeat_keeps_the_count_but_fails(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_om", _two_tests("b")),)}
            inventory = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 0)
            twice = ["test_om.M.test_a", "test_om.M.test_a"]
            _rewrite(tree.receipt("1"), started_ids=twice, completed_ids=twice, tests_run=2)

            problems = tree.verify(inventory, shards)

        self.assertIn("test identities that no shard completed (1): test_om.M.test_b", problems)
        self.assertIn(
            "test identities completed more than once (1): test_om.M.test_a (shards 1, 1)", problems
        )

    def test_a_test_that_runs_in_two_shards_fails_and_names_both(self) -> None:
        with _Tree() as tree:
            first = tree.write("test_dup_a", ONE_TEST)
            second = tree.write(
                "test_dup_b",
                "import unittest\nfrom test_dup_a import A\n\n\n"
                "class B(unittest.TestCase):\n    def test_y(self):\n        pass\n",
            )
            shards = {"1": (first,), "2": (second,)}
            with self.assertRaises(run_test_shard.DiscoveryError) as raised:
                tree.inventory(shards)
            self.assertIn(
                "test_dup_b collected tests owned by another module: test_dup_a.A.test_x",
                raised.exception.problems,
            )
            # Even an inventory that ignored the import catches the repeat run.
            inventory = {
                "schema": 2, "kind": "inventory", "profile": "linux-full", **tree.binding(shards),
                "modules": {first: ["test_dup_a.A.test_x"], second: ["test_dup_b.B.test_y"]},
                "ids": ["test_dup_a.A.test_x", "test_dup_b.B.test_y"],
            }
            self.assertEqual((tree.run("1", shards), tree.run("2", shards)), (0, 0))

            problems = tree.verify(inventory, shards)

        self.assertIn(
            "test identities completed more than once (1): test_dup_a.A.test_x (shards 1, 2)", problems
        )

    def test_a_same_count_substitution_fails_and_names_both_identities(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_sub", _two_tests("b")),)}
            inventory = tree.inventory(shards)
            tree.write("test_sub", _two_tests("replacement"))
            self.assertEqual(tree.run("1", shards), 0)

            problems = tree.verify(inventory, shards)

        self.assertIn("test identities that no shard completed (1): test_sub.M.test_b", problems)
        self.assertIn(
            "test identities completed but not discovered (1): test_sub.M.test_replacement", problems
        )

    def test_a_same_count_source_replacement_needs_the_base_comparison(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_rep", _two_tests("b")),)}
            base = tree.inventory(shards)
            tree.write("test_rep", _two_tests("replacement"))
            candidate = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 0)

            # Candidate-only accounting agrees with itself: this gap is why the
            # base comparison exists.
            self.assertEqual(tree.verify(candidate, shards), [])
            removed, added, problems = run_test_shard.compare_inventories(base, candidate)

        self.assertEqual(removed, ["test_rep.M.test_b"])
        self.assertEqual(added, ["test_rep.M.test_replacement"])
        self.assertEqual(problems, [])


class DiscoveryInventoryTests(unittest.TestCase):
    def test_inventory_lists_every_module_identity_sorted(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_one", ONE_TEST), tree.write("test_two", _two_tests("b")))}
            inventory = tree.inventory(shards)

        self.assertEqual(
            inventory["modules"],
            {"test_one": ["test_one.A.test_x"], "test_two": ["test_two.M.test_a", "test_two.M.test_b"]},
        )
        self.assertEqual(inventory["ids"], ["test_one.A.test_x", "test_two.M.test_a", "test_two.M.test_b"])
        self.assertEqual((inventory["schema"], inventory["kind"], inventory["profile"]), (2, "inventory", "linux-full"))
        self.assertIs(run_test_shard.validate_inventory(inventory), inventory)

    def test_discovery_refuses_inputs_that_would_hide_tests(self) -> None:
        cases = {
            "import error": ("import dependency_that_does_not_exist\n", "test_bad failed discovery"),
            "module skip": (
                "import unittest\nraise unittest.SkipTest('platform')\n" + ONE_TEST.split("\n", 1)[1],
                "test_bad skipped itself at import",
            ),
            "no tests": ("import unittest\n", "test_bad collected no tests"),
            "repeated identity": (
                "import unittest\n\n\nclass A(unittest.TestCase):\n    def test_x(self):\n        pass\n\n\n"
                "def load_tests(loader, tests, pattern):\n    tests.addTests(loader.loadTestsFromTestCase(A))\n"
                "    return tests\n",
                "test_bad collected duplicate test identities: test_bad.A.test_x",
            ),
        }
        for label, (body, expected) in cases.items():
            with self.subTest(case=label), _Tree() as tree:
                tree.write("test_good", ONE_TEST)
                tree.write("test_bad", body)
                with self.assertRaises(run_test_shard.DiscoveryError) as raised:
                    tree.inventory({"1": ("test_bad", "test_good")})
                self.assertTrue(
                    any(problem.startswith(expected) for problem in raised.exception.problems),
                    raised.exception.problems,
                )

    def test_a_nested_test_package_is_refused_rather_than_silently_dropped(self) -> None:
        with _Tree() as tree:
            tree.write("test_top", ONE_TEST)
            nested = tree.tests / "nestedpkg"
            nested.mkdir()
            (nested / "__init__.py").write_text("", encoding="utf-8")
            (nested / "test_inner.py").write_text(ONE_TEST, encoding="utf-8")
            tree._names.update({"nestedpkg", "nestedpkg.test_inner"})
            with self.assertRaises(run_test_shard.DiscoveryError) as raised:
                tree.inventory({"1": ("test_top",)})

        self.assertIn("unfiltered discovery disagrees", raised.exception.problems[0])
        self.assertIn("nestedpkg.test_inner.A.test_x", raised.exception.problems[0])

    def test_inventory_validation_rejects_malformed_payloads(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_v", _two_tests("b")),)}
            good = tree.inventory(shards)
        mutations = {
            "schema 1": {"schema": 1},
            "wrong kind": {"kind": "shard"},
            "empty commit": {"source_commit": ""},
            "unsorted ids": {"modules": {"test_v": ["test_v.M.test_b", "test_v.M.test_a"]}},
            "foreign id": {"modules": {"test_v": ["test_v.M.test_a", "test_w.M.test_b"]}},
            "ids not the union": {"ids": ["test_v.M.test_a"]},
            "empty module": {"modules": {"test_v": []}, "ids": []},
        }
        for label, change in mutations.items():
            with self.subTest(case=label), self.assertRaises(ValueError):
                run_test_shard.validate_inventory({**good, **change})


class ExecutionAccountingTests(unittest.TestCase):
    def test_receipt_records_collected_started_and_completed_identities(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_rec", _two_tests("b")),)}
            self.assertEqual(tree.run("1", shards), 0)
            receipt = json.loads(tree.receipt("1").read_text(encoding="utf-8"))

        ids = ["test_rec.M.test_a", "test_rec.M.test_b"]
        self.assertEqual((receipt["schema"], receipt["kind"]), (2, "shard"))
        self.assertEqual((receipt["collected_ids"], receipt["started_ids"], receipt["completed_ids"]), (ids, ids, ids))
        self.assertEqual(receipt["source_commit"], BINDING["source_commit"])
        self.assertEqual(receipt["run_id"], BINDING["run_id"])
        self.assertNotIn("test_counts", receipt)

    def test_a_class_fixture_skip_leaves_tests_unstarted_and_fails(self) -> None:
        body = (
            "import unittest\n\n\nclass F(unittest.TestCase):\n"
            "    @classmethod\n    def setUpClass(cls):\n        raise unittest.SkipTest('fixture unavailable')\n\n"
            "    def test_a(self):\n        pass\n"
        )
        with _Tree() as tree:
            shards = {"1": (tree.write("test_fixture", body),)}
            inventory = tree.inventory(shards)
            tree.run("1", shards)

            problems = tree.verify(inventory, shards)

        self.assertIn("shard 1: started set is missing 1: test_fixture.F.test_a", problems)
        self.assertIn("test identities that no shard completed (1): test_fixture.F.test_a", problems)

    def test_expected_failure_is_admitted_and_unexpected_success_is_not(self) -> None:
        expected_failure = (
            "import unittest\n\n\nclass E(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n    def test_known(self):\n        self.fail('known')\n"
        )
        with _Tree() as tree:
            shards = {"1": (tree.write("test_xf", expected_failure),)}
            inventory = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 0)
            self.assertEqual(tree.verify(inventory, shards), [])
            self.assertEqual(json.loads(tree.receipt("1").read_text())["expected_failures"], 1)
        with _Tree() as tree:
            shards = {"1": (tree.write("test_xs", expected_failure.replace("self.fail('known')", "pass")),)}
            inventory = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 1)
            problems = tree.verify(inventory, shards)
        self.assertIn("shard 1: nonzero unexpected_successes count", problems)

    def test_stale_bindings_are_rejected(self) -> None:
        for field, value in (
            ("source_commit", "d" * 40), ("run_id", "999"), ("partition_fingerprint", "0" * 64),
        ):
            with self.subTest(field=field), _Tree() as tree:
                shards = {"1": (tree.write("test_bind", ONE_TEST),)}
                inventory = tree.inventory(shards)
                self.assertEqual(tree.run("1", shards), 0)
                _rewrite(tree.receipt("1"), **{field: value})
                problems = tree.verify(inventory, shards)
                self.assertTrue(
                    any(problem.startswith(f"shard 1: {field} ") for problem in problems), problems
                )
        with _Tree() as tree:
            shards = {"1": (tree.write("test_bind", ONE_TEST),)}
            inventory = tree.inventory(shards)
            self.assertEqual(tree.run("1", shards), 0)
            stale_python = tree.verify({**inventory, "python_version": "3.0"}, shards)
            stale_table = run_test_shard.verify_results(
                tree.results, BINDING["python_version"], inventory,
                shards={"1": ("test_bind",), "2": ()}, tests_dir=tree.tests,
            )
        self.assertIn(f"inventory: built on Python '3.0', expected {BINDING['python_version']!r}", stale_python)
        self.assertIn("inventory: built for a different shard table", stale_table)

    def test_a_schema_one_receipt_cannot_prove_identities(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_old", ONE_TEST),)}
            inventory = tree.inventory(shards)
            tree.results.mkdir()
            tree.receipt("1").write_text(json.dumps({
                "shard": "1", "python_version": BINDING["python_version"], "modules": ["test_old"],
                "test_counts": {"test_old": 1}, "tests_run": 1, "failures": 0, "errors": 0,
                "skipped": 0, "unexpected_successes": 0, "was_successful": True,
            }), encoding="utf-8")

            problems = tree.verify(inventory, shards)

        self.assertTrue(any("is not schema 2" in problem for problem in problems), problems)
        self.assertIn("test identities that no shard completed (1): test_old.A.test_x", problems)


class CommandLineTests(unittest.TestCase):
    def _cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            capture_output=True, text=True, timeout=120, cwd=cwd,
        )

    def test_discover_imports_another_root_in_isolation(self) -> None:
        with _Tree() as tree:
            (tree.root / "app").mkdir()
            (tree.root / "app" / "__init__.py").write_text("MARKER = 'other_root'\n", encoding="utf-8")
            tree.write(
                "test_iso",
                "import unittest\nimport app\n\n\nclass T(unittest.TestCase):\n    pass\n\n\n"
                "setattr(T, 'test_' + app.MARKER, lambda self: None)\n",
            )
            output = tree.root / "out" / "inventory.json"
            completed = self._cli("discover", "--root", str(tree.root), "--output", str(output))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            inventory = run_test_shard.load_inventory(output)
            again = self._cli("discover", "--root", str(tree.root), "--output", str(output))

        self.assertEqual(inventory["ids"], ["test_iso.T.test_other_root"])
        self.assertNotEqual(again.returncode, 0, "an existing inventory must not be overwritten")

    def test_compare_fails_on_removals_unless_approved_and_writes_a_summary(self) -> None:
        with _Tree() as tree:
            shards = {"1": (tree.write("test_cmp", _two_tests("b")),)}
            base_path, candidate_path = tree.root / "base.json", tree.root / "candidate.json"
            base_path.write_text(json.dumps(tree.inventory(shards)), encoding="utf-8")
            tree.write("test_cmp", _two_tests("replacement"))
            candidate = tree.inventory(shards)
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            summary = tree.root / "summary.md"
            args = ("compare", "--base", str(base_path), "--candidate", str(candidate_path))

            refused = self._cli(*args, "--summary", str(summary))
            approved = self._cli(*args, "--removals-approved")
            same = self._cli("compare", "--base", str(candidate_path), "--candidate", str(candidate_path))
            candidate_path.write_text(json.dumps({**candidate, "python_version": "3.0"}), encoding="utf-8")
            mismatched = self._cli(*args)
            summary_text = summary.read_text(encoding="utf-8")

        self.assertEqual(refused.returncode, 1)
        self.assertIn("removed: test_cmp.M.test_b", refused.stdout)
        self.assertIn("added: test_cmp.M.test_replacement", refused.stdout)
        self.assertIn("`tests-removed-ok` label", refused.stderr)
        self.assertIn("removed: test_cmp.M.test_b", summary_text)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertIn("removed: test_cmp.M.test_b", approved.stdout)
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertIn("Added: 0. Removed or renamed: 0.", same.stdout)
        self.assertEqual(mismatched.returncode, 2)

    def test_verify_cli_requires_a_readable_inventory(self) -> None:
        with _Tree() as tree:
            completed = self._cli(
                "verify", "--results-dir", str(tree.results), "--python-version", "3.12",
                "--inventory", str(tree.root / "absent.json"),
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("cannot read inventory", completed.stderr)

    def test_count_manifest_commands_are_gone(self) -> None:
        self.assertFalse((ROOT / "tests" / "unittest_test_counts.json").exists())
        for command in ("check-counts", "update-counts"):
            with self.subTest(command=command), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    run_test_shard._build_parser().parse_args([command])


if __name__ == "__main__":
    unittest.main()
