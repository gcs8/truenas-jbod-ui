"""Offline policy and native-report contracts. All advisory fixtures are synthetic."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/dependency_audit.py"
GHSA = "GHSA-2345-6789-cfgh"
OTHER_GHSA = "GHSA-jmpq-rvwx-2345"
CVE = "CVE-2099-12345"
TODAY = date(2099, 1, 1)


def entry(ecosystem="python", package="example-lib", advisory=GHSA, **changes):
    return {"ecosystem": ecosystem, "package": package, "advisory": advisory,
            "reason": "Temporary acceptance pending a compatible release.",
            "expires": "2099-01-02", **changes}


def python_report(vulnerable=False):
    vulns = [{"id": GHSA, "aliases": [CVE], "fix_versions": ["2.0"]}] if vulnerable else []
    return {"dependencies": [{"name": "example-lib", "version": "1.0", "vulns": vulns}], "fixes": []}


def advisory(name="leaf", severity="high", source=123, ghsa=GHSA):
    return {"source": source, "name": name, "dependency": name, "title": "Synthetic advisory",
            "url": f"https://github.com/advisories/{ghsa}", "severity": severity,
            "cwe": [], "cvss": {"score": 7.5, "vectorString": None}, "range": "<2"}


def npm_record(name, via, severity="high"):
    return {"name": name, "severity": severity, "isDirect": False, "via": via,
            "effects": [], "range": "<2", "nodes": [f"node_modules/{name}"], "fixAvailable": True}


def npm_report(records=None):
    records = records or {}
    counts: dict[str, int] = dict.fromkeys(("info", "low", "moderate", "high", "critical"), 0)
    for record in records.values():
        counts[record["severity"]] += 1
    counts["total"] = len(records)
    return {"auditReportVersion": 2, "vulnerabilities": records,
            "metadata": {"vulnerabilities": counts, "dependencies": {"total": 5}}}


def completed(report, rc=0):
    return subprocess.CompletedProcess([], rc, json.dumps(report), "")


class DependencyAuditTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "dependency audit runner must exist")
        spec = importlib.util.spec_from_file_location("dependency_audit", SCRIPT)
        assert spec is not None and spec.loader is not None
        self.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.audit)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".github").mkdir()
        self.policy = self.root / ".github/dependency-audit-ignore.json"
        self.write_policy([])

    def write_policy(self, entries):
        self.policy.write_text(json.dumps({"version": 1, "ignores": entries}), encoding="utf-8")

    def load_policy(self):
        return self.audit.load_policy(self.policy, today=TODAY)

    def run_reports(self, reports, ignores=()):
        runner = mock.Mock(side_effect=reports)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            result = self.audit.run_audits(self.root, ignores, runner=runner)
        return result, runner, output.getvalue()

    def test_empty_policy_and_canonical_python_package(self):
        self.assertEqual(self.load_policy(), [])
        self.write_policy([entry(package="Example.Lib_Name")])
        self.assertEqual(self.load_policy()[0]["package"], "example-lib-name")

    def test_policy_rejects_missing_unknown_and_duplicate_fields(self):
        for field in entry():
            value = entry()
            del value[field]
            self.write_policy([value])
            with self.subTest(missing=field), self.assertRaises(self.audit.AuditError):
                self.load_policy()
        for value in (entry(extra=True), entry(reason=" \t"), entry(reason=1), entry(ecosystem="all"),
                      entry(package="*"), entry(advisory="*"), entry(package="bad/name"),
                      entry(ecosystem="npm", package="@scope/*")):
            self.write_policy([value])
            with self.subTest(value=value), self.assertRaises(self.audit.AuditError):
                self.load_policy()
        self.write_policy([entry(package="Example_Lib"), entry(package="example-lib")])
        with self.assertRaises(self.audit.AuditError):
            self.load_policy()
        for raw in ('{"version":1,"ignores":[],"ignores":[]}',
                    '{"version":1,"ignores":[],"extra":0}', '{"ignores":[]}',
                    '{"version":true,"ignores":[]}', '{"version":2,"ignores":[]}',
                    '{"version":1,"ignores":{}}', '[]', 'not json'):
            self.policy.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(self.audit.AuditError):
                self.load_policy()

    def test_expiry_is_required_strict_iso_and_strictly_future(self):
        for value in (None, 20990102, "2099-01-01", "2098-12-31", "20990102", "2099-W01-5",
                      "2099-1-2", "2099-02-30", "2099-01-02T00:00:00", " 2099-01-02"):
            self.write_policy([entry(expires=value)])
            with self.subTest(expires=value), self.assertRaises(self.audit.AuditError):
                self.load_policy()

    def test_invalid_policy_prevents_any_scanner_call(self):
        self.write_policy([entry(expires="2000-01-01")])
        with mock.patch.object(self.audit, "ROOT", self.root), mock.patch.object(
            self.audit.subprocess, "run"
        ) as runner, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(self.audit.main([]), 2)
        runner.assert_not_called()

    def test_validate_only_does_not_run_scanners(self):
        with mock.patch.object(self.audit, "ROOT", self.root), mock.patch.object(
            self.audit.subprocess, "run"
        ) as runner:
            self.assertEqual(self.audit.main(["--validate-only"]), 0)
        runner.assert_not_called()

    def test_native_commands_keep_lock_exact_and_dev_resolution(self):
        result, runner, _ = self.run_reports([completed(python_report()), completed(python_report()),
                                            completed(npm_report())])
        self.assertEqual(result, 0)
        common = [sys.executable, "-m", "pip_audit", "--format", "json", "--aliases", "on",
                  "--desc", "off", "--progress-spinner", "off", "--strict"]
        self.assertEqual([call.args[0] for call in runner.call_args_list], [
            common + ["-r", "requirements.lock", "--no-deps", "--disable-pip"],
            common + ["-r", "requirements-dev.txt"],
            ["npm", "audit", "--package-lock-only", "--audit-level=high", "--json"],
        ])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["cwd"], self.root)
            self.assertTrue(call.kwargs["capture_output"])
            self.assertTrue(call.kwargs["text"])
            self.assertFalse(call.kwargs["check"])
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_findings_fail_and_all_three_audits_are_attempted(self):
        result, runner, output = self.run_reports([
            completed(python_report(True), 1), completed(python_report(True), 1),
            completed(npm_report({"leaf": npm_record("leaf", [advisory()])}), 1)])
        self.assertEqual(result, 1, "unaccepted findings must fail")
        self.assertEqual(runner.call_count, 3)
        self.assertIn("requirements.lock", output)
        self.assertIn("requirements-dev.txt", output)
        self.assertIn("npm", output)

    def test_package_specific_python_alias_ignore_does_not_hide_other_packages(self):
        self.write_policy([entry(advisory=CVE)])
        ignores = self.load_policy()
        reports = [completed(python_report(True), 1), completed(python_report()), completed(npm_report())]
        self.assertEqual(self.run_reports(reports, ignores)[0], 0)
        report = python_report(True)
        report["dependencies"].append({**report["dependencies"][0], "name": "another-lib"})
        reports[0] = completed(report, 1)
        self.assertEqual(self.run_reports(reports, ignores)[0], 1)
        self.write_policy([entry(ecosystem="npm", package="example-lib", advisory=GHSA)])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 1)

    def test_python_malformed_skipped_empty_and_exit_mismatch_fail_closed(self):
        bad_reports = [{}, [], {"dependencies": [], "fixes": []},
                       {"dependencies": [{"name": "example-lib", "skip_reason": "unavailable"}], "fixes": []},
                       {"dependencies": [{"name": "x", "version": "1"}], "fixes": []}]
        for key, value in (("aliases", None), ("id", ""), ("aliases", [5]), ("fix_versions", None)):
            report = python_report(True)
            report["dependencies"][0]["vulns"][0][key] = value
            bad_reports.append(report)
        for report in bad_reports:
            with self.subTest(report=report):
                result, runner, _ = self.run_reports([completed(report), completed(python_report()),
                                                     completed(npm_report())])
                self.assertEqual(result, 2)
                self.assertEqual(runner.call_count, 3)
        for report, rc in ((python_report(), 1), (python_report(True), 0), (python_report(), 2)):
            with self.subTest(rc=rc, report=report):
                self.assertEqual(self.run_reports([completed(report, rc), completed(python_report()),
                                                   completed(npm_report())])[0], 2)

    def test_tool_transport_timeout_and_non_json_are_not_clean(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("scanner", 1),
                        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid scanner output"),
                        completed({"error": "transport unavailable"}, 1),
                        subprocess.CompletedProcess([], 1, "not json", "private diagnostic")):
            with self.subTest(failure=failure):
                result, runner, output = self.run_reports([failure, completed(python_report()), completed(npm_report())])
                self.assertEqual(result, 2)
                self.assertEqual(runner.call_count, 3)
                self.assertNotIn("private diagnostic", output)

    def test_npm_high_and_critical_fail_but_lower_severities_pass(self):
        for severity in ("info", "low", "moderate", "high", "critical"):
            report = npm_report({"leaf": npm_record("leaf", [advisory(severity=severity)], severity)})
            result, _, _ = self.run_reports([completed(python_report()), completed(python_report()),
                                             completed(report, int(severity in {"high", "critical"}))])
            self.assertEqual(result, int(severity in {"high", "critical"}))

    def test_npm_ignore_follows_real_root_not_propagated_package(self):
        report = npm_report({"leaf": npm_record("leaf", [advisory()]),
                             "middle": npm_record("middle", ["leaf"]),
                             "top": npm_record("top", ["middle"])})
        reports = [completed(python_report()), completed(python_report()), completed(report, 1)]
        self.write_policy([entry("npm", "leaf")])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 0)
        self.write_policy([entry("npm", "top")])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 1)
        self.write_policy([entry("npm", "leaf", advisory="npm:123")])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 0)

    def test_npm_mixed_roots_keep_unignored_high_and_do_not_invent_low_findings(self):
        report = npm_report({"leaf": npm_record("leaf", [advisory(), advisory(severity="low", source=124,
                                                                                       ghsa=OTHER_GHSA)]),
                             "top": npm_record("top", ["leaf"])})
        reports = [completed(python_report()), completed(python_report()), completed(report, 1)]
        self.write_policy([entry("npm", "leaf")])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 0)
        report["vulnerabilities"]["leaf"]["via"][1]["severity"] = "high"
        reports[2] = completed(report, 1)
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 1)
        # A distinct high child must survive acceptance of its sibling.
        report = npm_report({"leaf": npm_record("leaf", [advisory()]),
                             "other": npm_record("other", [advisory("other", ghsa=OTHER_GHSA, source=124)]),
                             "top": npm_record("top", ["leaf", "other"])})
        reports[2] = completed(report, 1)
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 1)

    def test_npm_cycles_with_advisory_roots_remain_auditable(self):
        report = npm_report({"leaf": npm_record("leaf", [advisory(), "top"]),
                             "top": npm_record("top", ["leaf"])})
        reports = [completed(python_report()), completed(python_report()), completed(report, 1)]
        self.assertEqual(self.run_reports(reports)[0], 1)
        self.write_policy([entry("npm", "leaf")])
        self.assertEqual(self.run_reports(reports, self.load_policy())[0], 0)

    def test_npm_unknown_graph_schema_counts_and_exit_fail_closed(self):
        base = npm_report({"leaf": npm_record("leaf", [advisory()])})
        cases = [{}, {"auditReportVersion": 2}, {**base, "auditReportVersion": 3},
                 {**base, "error": {}}, {**base, "metadata": {}},
                 npm_report({"top": npm_record("top", ["missing"])}),
                 npm_report({"top": npm_record("top", ["leaf"]), "leaf": npm_record("leaf", ["top"])}),
                 npm_report({"leaf": npm_record("leaf", [advisory(severity="low")])})]
        for key, value in (("via", []), ("via", [7]), ("name", "different"), ("severity", "unknown"),
                           ("nodes", [])):
            report = copy.deepcopy(base)
            report["vulnerabilities"]["leaf"][key] = value
            cases.append(report)
        for key, value in (("source", None), ("url", "https://example.invalid/unknown"),
                           ("severity", "unknown"), ("name", "different")):
            report = copy.deepcopy(base)
            report["vulnerabilities"]["leaf"]["via"][0][key] = value
            cases.append(report)
        report = copy.deepcopy(base)
        report["metadata"]["vulnerabilities"]["high"] = 0
        cases.append(report)
        for report in cases:
            with self.subTest(report=report):
                self.assertEqual(self.run_reports([completed(python_report()), completed(python_report()),
                                                   completed(report, 1)])[0], 2)
        for report, rc in ((base, 0), (npm_report(), 1), (npm_report(), 2)):
            self.assertEqual(self.run_reports([completed(python_report()), completed(python_report()),
                                               completed(report, rc)])[0], 2)


if __name__ == "__main__":
    unittest.main()
