"""Offline policy tests and native, synthetic full-history scanner regressions."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / ".github/scripts/secret_scan.py"
FINGERPRINT = "a" * 40 + ":tests/fixture.py:generic-api-key:12"
NOTE = "# Reviewed tests/fixture.py: synthetic offline scanner fixture, not a credential.\n"


def load_helper():
    if not HELPER.is_file():
        raise AssertionError("the maintained secret-scan helper is missing")
    spec = importlib.util.spec_from_file_location("secret_scan", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IgnorePolicyTests(unittest.TestCase):
    def setUp(self):
        self.scan = load_helper()

    def test_reviewed_commit_fingerprint_and_separate_comment(self):
        self.assertEqual(self.scan.validate_ignore(NOTE + FINGERPRINT + "\n"), [FINGERPRINT])
        self.assertEqual(self.scan.validate_ignore("# No reviewed exceptions.\n"), [])

    def test_refuses_global_malformed_and_inline_commented_entries(self):
        for entry in (
            "tests/fixture.py:generic-api-key:12",
            FINGERPRINT + " # reviewed",
            FINGERPRINT.replace("a" * 40, "a" * 39),
            FINGERPRINT.replace(":12", ":0"),
            FINGERPRINT.replace("tests/fixture.py", "../fixture.py"),
            FINGERPRINT.replace("tests/fixture.py", "/fixture.py"),
            FINGERPRINT.replace("tests/fixture.py", "tests//fixture.py"),
            FINGERPRINT.replace("generic-api-key", "*"),
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.scan.validate_ignore(NOTE + entry + "\n")

    def test_requires_an_immediately_preceding_specific_note(self):
        for note in ("", "# reviewed\n", "# Reviewed unrelated/file.py: synthetic test fixture.\n", NOTE + "\n"):
            with self.subTest(note=note), self.assertRaises(ValueError):
                self.scan.validate_ignore(note + FINGERPRINT + "\n")

    def test_duplicate_fingerprints_are_refused(self):
        with self.assertRaises(ValueError):
            self.scan.validate_ignore((NOTE + FINGERPRINT + "\n") * 2)

    def test_checked_in_ignore_file_obeys_the_same_policy(self):
        self.assertTrue((ROOT / ".gitleaksignore").is_file())
        self.scan.validate_ignore((ROOT / ".gitleaksignore").read_text(encoding="utf-8"))

    def test_release_checksum_and_member_validation_fail_closed(self):
        with self.assertRaises(ValueError):
            self.scan.extract_binary(b"unchecked download")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo("../gitleaks")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        data = stream.getvalue()
        with patch.object(self.scan, "ARCHIVE_SHA256", self.scan.hashlib.sha256(data).hexdigest()):
            with self.assertRaises(ValueError):
                self.scan.extract_binary(data)

    def test_scanner_failure_is_not_a_clean_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ignore = root / "ignore"
            ignore.write_text("", encoding="utf-8")
            result = subprocess.CompletedProcess([], 2, b"", b"private diagnostic")
            with (
                patch.object(self.scan, "verify_binary"),
                patch.object(self.scan, "git", return_value="false"),
                patch.object(self.scan.subprocess, "run", return_value=result),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = self.scan.scan(root / "gitleaks", root, ignore)
            self.assertEqual(status, 2)
            self.assertNotIn("private diagnostic", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "scanner_error")

    def test_missing_or_malformed_report_is_not_a_clean_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ignore = root / "ignore"
            ignore.write_text("", encoding="utf-8")
            for content in (None, "{}", "not json"):

                def fake_run(command, **kwargs):
                    if content is not None:
                        Path(command[command.index("--report-path") + 1]).write_text(content)
                    return subprocess.CompletedProcess(command, 0, b"", b"")

                with (
                    self.subTest(content=content),
                    patch.object(self.scan, "verify_binary"),
                    patch.object(self.scan, "git", return_value="false"),
                    patch.object(self.scan.subprocess, "run", side_effect=fake_run),
                ):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(self.scan.scan(root / "gitleaks", root, ignore), 2)


class NativeHistoryTests(unittest.TestCase):
    """The workflow sets GITLEAKS_BINARY; no native regression is silently skipped."""

    def setUp(self):
        self.scan = load_helper()
        self.binary = Path(os.environ["GITLEAKS_BINARY"])
        self.scan.verify_binary(self.binary)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Scanner fixture")
        self.git("config", "user.email", "scanner@example.test")
        self.ignore = self.root / "ignore"
        self.ignore.write_text("", encoding="utf-8")
        # Assembled only at runtime. It is deliberately not a usable provider token.
        self.secret = "SYNTHETIC_" + "aB3dE5fG7hI9jK2mN4pQ6rS8tU0vW"

    def git(self, *args):
        return subprocess.check_output(
            ["git", "--no-replace-objects", *args], cwd=self.repo, stderr=subprocess.DEVNULL, text=True
        ).strip()

    def commit(self, text):
        (self.repo / "fixture.txt").write_text(text, encoding="utf-8")
        self.git("add", "fixture.txt")
        self.git("commit", "-m", "Synthetic scanner fixture")
        return self.git("rev-parse", "HEAD")

    def native(self):
        report = self.root / "report.json"
        config = self.root / "config.toml"
        config.write_text(self.scan.DEFAULT_CONFIG, encoding="utf-8")
        command = self.scan.scan_command(self.binary, self.repo, self.ignore, config, report)
        command.insert(2, "--verbose")
        result = subprocess.run(command, env=self.scan.scanner_env(self.root), capture_output=True, check=False)
        raw = report.read_bytes()
        self.assertNotIn(self.secret.encode(), result.stdout + result.stderr + raw)
        findings = json.loads(raw)
        self.assertTrue(all(row["Secret"] == "REDACTED" for row in findings))
        if findings:
            self.assertIn(b"REDACTED", result.stdout + result.stderr)
        return result.returncode, findings

    def test_deleted_secret_in_pr_ancestry_is_detected_and_fully_redacted(self):
        self.commit("clean\n")
        self.git("checkout", "-b", "pull-request")
        head = self.commit('api_key = "' + self.secret + '" # gitleaks:allow\n')
        self.commit("removed before merge\n")
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "pull-request", "-m", "Merge synthetic pull request")
        status, rows = self.native()
        self.assertEqual(status, 1)
        self.assertTrue(any(row["Commit"] == head for row in rows))
        self.assertTrue(all("REDACTED" in row["Match"] for row in rows))

    def test_exact_commented_fingerprint_ignores_only_the_reviewed_occurrence(self):
        self.commit('api_key = "' + self.secret + '"\n')
        status, rows = self.native()
        self.assertEqual(status, 1)
        self.assertEqual(len(rows), 1)
        fingerprint = rows[0]["Fingerprint"]
        note = "# Reviewed fixture.txt: unmistakably synthetic native regression.\n"
        self.ignore.write_text(note + fingerprint + "\n", encoding="utf-8")
        self.scan.validate_ignore(self.ignore.read_text())
        self.assertEqual(self.native(), (0, []))
        self.commit('api_key = "' + self.secret + '"\nsecond_api_key = "' + self.secret + '"\n')
        status, rows = self.native()
        self.assertEqual(status, 1)
        self.assertTrue(rows)
        self.assertTrue(all(row["Fingerprint"] != fingerprint for row in rows))

    def test_all_reachable_refs_are_scanned_not_just_head(self):
        self.commit("clean\n")
        self.git("checkout", "-b", "other-reachable")
        head = self.commit('api_key = "' + self.secret + '"\n')
        self.git("checkout", "main")
        status, rows = self.native()
        self.assertEqual(status, 1)
        self.assertTrue(any(row["Commit"] == head for row in rows))

    def test_shallow_checkout_and_native_operational_failure_are_refused(self):
        self.commit("clean\n")
        shallow = self.root / "shallow"
        subprocess.run(["git", "clone", "--depth=1", self.repo.as_uri(), str(shallow)], check=True, capture_output=True)
        for repo in (shallow, self.root / "not-a-repo"):
            with self.subTest(repo=repo.name), redirect_stdout(io.StringIO()):
                self.assertEqual(self.scan.scan(self.binary, repo, self.ignore), 2)


if __name__ == "__main__":
    unittest.main()
