"""Offline contracts for the pinned workflow lint gate."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/workflow_lint.py"


class WorkflowLintTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "the executable workflow lint gate is missing")
        spec = importlib.util.spec_from_file_location("workflow_lint", SCRIPT)
        assert spec is not None and spec.loader is not None
        self.lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lint)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = ".github/workflows/example.yml"
        source = self.root / self.path
        source.parent.mkdir(parents=True)
        source.write_text("on: push\njobs: {}\n", encoding="utf-8")
        self.finding = {
            "ident": "dangerous-triggers", "desc": "synthetic finding",
            "url": "https://docs.zizmor.sh/audits/#dangerous-triggers",
            "determinations": {"confidence": "Medium", "severity": "High", "persona": "Regular"},
            "locations": [{
                "symbolic": {
                    "key": {"Local": {"verbatim_path": self.path}},
                    "annotation": "synthetic location", "route": {"route": [{"Key": "on"}]},
                    "feature_kind": "KeyOnly", "kind": "Primary",
                },
                "concrete": {
                    "location": {
                        "start_point": {"row": 0, "column": 0},
                        "end_point": {"row": 0, "column": 2},
                        "offset_span": {"start": 0, "end": 2},
                    },
                    "feature": "on", "comments": [],
                },
            }],
            "ignored": False, "fixes": [],
        }
        self.entry = {
            "rule": "dangerous-triggers", "path": self.path, "line": 1, "column": 1,
            "severity": "High", "confidence": "Medium",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "reason": "Synthetic title-only label workflow, no checkout or code execution.",
        }
        self.policy = {"version": 1, "scanner": "zizmor 1.33.1", "format": "json-v1", "findings": [self.entry]}

    def result(self, findings=None, code=14):
        return subprocess.CompletedProcess([], code, json.dumps([self.finding] if findings is None else findings), "")

    def baseline(self, policy=None):
        path = self.root / ".github/workflow-lint-baseline.json"
        path.write_text(json.dumps(self.policy if policy is None else policy), encoding="utf-8")
        return self.lint.load_baseline(self.root)

    def check(self, result=None, baseline=None):
        return self.lint.check_zizmor(
            self.root, self.result() if result is None else result,
            self.baseline() if baseline is None else baseline, [self.path],
        )

    def test_selection_includes_every_yaml_workflow_not_just_changed_files(self):
        for name in ("new.yaml", "another.yml", "nested/future.yaml", "not-a-workflow.txt"):
            path = self.root / ".github/workflows" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.assertEqual(self.lint.workflow_paths(self.root), [
            ".github/workflows/another.yml", self.path, ".github/workflows/nested/future.yaml",
            ".github/workflows/new.yaml",
        ])

    def test_empty_selection_fails(self):
        (self.root / self.path).unlink()
        with self.assertRaises(self.lint.LintError):
            self.lint.workflow_paths(self.root)

    def test_exact_unchanged_finding_is_accepted(self):
        self.assertEqual(self.check(), 1)

    def test_new_finding_cannot_use_an_existing_entry(self):
        other = copy.deepcopy(self.finding)
        other["ident"] = "template-injection"
        with self.assertRaises(self.lint.LintError):
            self.check(self.result([self.finding, other]))

    def test_changed_source_requires_fresh_review_even_outside_flagged_line(self):
        baseline = self.baseline()
        with (self.root / self.path).open("a", encoding="utf-8") as stream:
            stream.write("# a new executable step could follow the trigger\n")
        with self.assertRaises(self.lint.LintError):
            self.check(baseline=baseline)
        with self.assertRaises(self.lint.LintError):
            self.baseline()

    def test_changed_location_or_severity_or_confidence_is_not_accepted(self):
        for field, value in (("row", 1), ("severity", "Low"), ("confidence", "Low")):
            finding = copy.deepcopy(self.finding)
            if field == "row":
                finding["locations"][0]["concrete"]["location"]["start_point"][field] = value
                code = 14
            else:
                finding["determinations"][field] = value
                code = 12 if field == "severity" else 14
            with self.subTest(field=field), self.assertRaises(self.lint.LintError):
                self.check(self.result([finding], code))

    def test_missing_or_duplicate_native_findings_fail(self):
        for findings, code in (([], 0), ([self.finding, self.finding], 14)):
            with self.subTest(count=len(findings)), self.assertRaises(self.lint.LintError):
                self.check(self.result(findings, code))

    def test_baseline_rejects_unknown_fields_duplicates_and_missing_reason(self):
        variants = []
        for change in ({"reason": " "}, {"unknown": True}, {"line": True}, {"path": "../other.yml"},
                       {"source_sha256": "not-a-digest"}):
            policy = copy.deepcopy(self.policy)
            policy["findings"][0].update(change)
            variants.append(policy)
        for change in ({"unknown": []}, {"version": True}, {"scanner": "zizmor 0.0.0"}, {"format": "json"}):
            variants.append({**self.policy, **change})
        variants.append({**self.policy, "findings": [self.entry, self.entry]})
        policy = copy.deepcopy(self.policy)
        del policy["findings"][0]["reason"]
        variants.append(policy)
        for index, policy in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(self.lint.LintError):
                self.baseline(policy)

    def test_duplicate_json_keys_fail_for_baseline_and_native_output(self):
        path = self.root / ".github/workflow-lint-baseline.json"
        path.write_text('{"version":1,"version":1}', encoding="utf-8")
        with self.assertRaises(self.lint.LintError):
            self.lint.load_baseline(self.root)
        result = self.result()
        result.stdout = result.stdout.replace('"ident": "dangerous-triggers"',
                                              '"ident": "dangerous-triggers", "ident": "dangerous-triggers"')
        with self.assertRaises(self.lint.LintError):
            self.check(result)

    def test_missing_malformed_or_inconsistent_reports_never_pass(self):
        for output in ("", "null", "{}", "not json", "[{}]", '[{"ignored": true}]'):
            with self.subTest(output=output), self.assertRaises(self.lint.LintError):
                self.check(subprocess.CompletedProcess([], 0, output, ""))
        for code in (0, 1, 2, 3, 10, 11, 12, 13, 15, -9):
            with self.subTest(code=code), self.assertRaises(self.lint.LintError):
                self.check(self.result(code=code))

    def test_ignored_foreign_or_unknown_schema_findings_fail_closed(self):
        for mutation in ("ignored", "foreign", "no-primary", "schema", "bad-location", "bad-determinations"):
            finding = copy.deepcopy(self.finding)
            if mutation == "ignored":
                finding["ignored"] = True
            elif mutation == "foreign":
                finding["locations"][0]["symbolic"]["key"]["Local"]["verbatim_path"] = "../foreign.yml"
            elif mutation == "no-primary":
                finding["locations"][0]["symbolic"]["kind"] = "Related"
            elif mutation == "schema":
                finding["future"] = True
            elif mutation == "bad-location":
                finding["locations"][0]["concrete"]["location"]["start_point"]["row"] = "0"
            else:
                finding["determinations"] = {}
            with self.subTest(mutation=mutation), self.assertRaises(self.lint.LintError):
                self.check(self.result([finding]))

    def test_actionlint_requires_valid_report_and_native_success(self):
        self.assertEqual(self.lint.check_actionlint(subprocess.CompletedProcess([], 0, "[]\n", "")), 0)
        for code, output, error in ((0, "", ""), (0, "null", ""), (1, "[]", ""),
                                    (0, "[]", "tool error"), (0, "[{}]", "")):
            with self.subTest(code=code, output=output), self.assertRaises(self.lint.LintError):
                self.lint.check_actionlint(subprocess.CompletedProcess([], code, output, error))

    def test_actionlint_diagnostics_fail_instead_of_being_baselined(self):
        report = [{"kind": "shellcheck", "filepath": self.path, "line": 1, "column": 1,
                   "message": "synthetic SC2086", "snippet": "synthetic", "end_column": 2}]
        with self.assertRaises(self.lint.LintError):
            self.lint.check_actionlint(subprocess.CompletedProcess([], 1, json.dumps(report), ""))

    def test_scan_commands_enable_shellcheck_and_explicit_zizmor_contract(self):
        commands = self.lint.scanner_commands(Path("tools"), [self.path, ".github/workflows/new.yaml"])
        actionlint = commands["actionlint"]
        self.assertEqual(actionlint[actionlint.index("-shellcheck") + 1], "tools/shellcheck")
        self.assertEqual(actionlint[-2:], [self.path, ".github/workflows/new.yaml"])
        self.assertNotIn("-ignore", actionlint)
        zizmor = commands["zizmor"]
        for flag in ("--offline", "--no-config", "--no-ignores", "--strict-collection",
                     "--format=json-v1", "--persona=regular"):
            self.assertIn(flag, zizmor)
        self.assertNotIn("--no-exit-codes", zizmor)
        self.assertEqual(zizmor[-2:], actionlint[-2:])

    def test_failed_actionlint_exposes_locations_but_not_native_content(self):
        report = [{"kind": "shellcheck", "filepath": self.path, "line": 1, "column": 1,
                   "message": "SC2086 SYNTHETIC_PRIVATE_MESSAGE", "snippet": "SYNTHETIC_PRIVATE_SOURCE"}]
        result = subprocess.CompletedProcess([], 1, json.dumps(report), "SYNTHETIC_PRIVATE_STDERR")
        self.baseline()
        output = io.StringIO()
        with patch.object(self.lint, "check_versions"), patch.object(
            self.lint.subprocess, "run", side_effect=[result, self.result()]
        ), redirect_stdout(output), self.assertRaises(self.lint.LintError):
            self.lint.scan(self.root, self.root / "tools", self.root / "reports")
        diagnostics = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(diagnostics, [{"scanner": "actionlint", "rule": "shellcheck/SC2086",
                                       "file": self.path, "line": 1, "column": 1}])
        self.assertIn("actionlint: native exit 1", output.getvalue())
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())

    def test_failed_zizmor_exposes_locations_but_not_native_content(self):
        finding = copy.deepcopy(self.finding)
        finding.update(ident="template-injection", desc="SYNTHETIC_PRIVATE_DESCRIPTION",
                       url="SYNTHETIC_PRIVATE_URL")
        finding["locations"][0]["concrete"]["feature"] = "SYNTHETIC_PRIVATE_SOURCE"
        result = self.result([finding])
        result.stderr = "SYNTHETIC_PRIVATE_STDERR"
        self.baseline()
        output = io.StringIO()
        with patch.object(self.lint, "check_versions"), patch.object(
            self.lint.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "[]", ""), result]
        ), redirect_stdout(output), self.assertRaises(self.lint.LintError):
            self.lint.scan(self.root, self.root / "tools", self.root / "reports")
        diagnostics = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(diagnostics, [{"scanner": "zizmor", "rule": "template-injection",
                                       "file": self.path, "line": 1, "column": 1}])
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())

    def test_diagnostic_metadata_rejects_malformed_or_foreign_rows(self):
        for report in ("not-json", "{}", "[{}]", json.dumps([{
            "kind": "shellcheck", "filepath": "../SYNTHETIC_PRIVATE", "line": 1, "column": 1,
            "message": "SC2086 SYNTHETIC_PRIVATE",
        }])):
            output = io.StringIO()
            with self.subTest(report=report), redirect_stdout(output):
                self.lint.emit_safe_diagnostics(self.root, "actionlint",
                    subprocess.CompletedProcess([], 1, report, "SYNTHETIC_PRIVATE"), [self.path])
            self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())
            self.assertNotIn('{"', output.getvalue())

    def test_tool_version_mismatch_or_execution_error_fails(self):
        with patch.object(self.lint.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "wrong version", "")):
            with self.assertRaises(self.lint.LintError):
                self.lint.check_versions(Path("tools"))
        with patch.object(self.lint.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(self.lint.LintError):
                self.lint.check_versions(Path("tools"))

    def test_acquisition_checks_digest_before_extracting(self):
        destination = self.root / "tools"
        with patch.object(self.lint.urllib.request, "urlopen") as download:
            download.return_value.__enter__.return_value.read.return_value = b"wrong archive"
            with self.assertRaises(self.lint.LintError):
                self.lint.install_tools(destination)
        self.assertFalse((destination / "actionlint").exists())


if __name__ == "__main__":
    unittest.main()
