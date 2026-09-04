from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import check_changelog_entry as gate


BASE_CHANGELOG = """# Changelog

## Unreleased

### Fixed

- Fixed an earlier thing (#1).

## v0.1.0 - 2026-01-01

- Initial release.
"""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class ChangelogEntryGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.test")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        self._write("CHANGELOG.md", BASE_CHANGELOG)
        self._write("app/service.py", "VALUE = 1\n")
        self._write("tests/test_service.py", "def test() -> None:\n    pass\n")
        self._commit("chore: base")
        _git(self.repo, "checkout", "-q", "-b", "topic")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, subject: str) -> None:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", subject)

    def _evaluate(self, pr_number: int = 42, labels: set[str] | None = None) -> gate.GateResult:
        return gate.evaluate(
            self.repo,
            base="main",
            head="topic",
            pr_number=pr_number,
            labels=labels or set(),
        )

    def _add_entry(self, subsection: str, entry: str) -> None:
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        marker = f"### {subsection}\n\n"
        if marker in text:
            text = text.replace(marker, marker + entry + "\n", 1)
        else:
            text = text.replace("## Unreleased\n\n", f"## Unreleased\n\n{marker}{entry}\n\n", 1)
        self._write("CHANGELOG.md", text)

    def test_code_change_without_entry_fails_with_instructions(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("CHANGELOG.md needs an entry for #42", joined)
        self.assertIn("(#42)", joined)
        self.assertIn("'### Fixed'", joined)
        self.assertIn("no-changelog", joined)

    def test_code_change_with_entry_passes(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_entry("Fixed", "- Bumped the value (#42).")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("Fixed", result.messages[0])

    def test_entry_for_a_different_pr_number_fails(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_entry("Fixed", "- Bumped the value (#41).")
        self._commit("fix: bump value")

        result = self._evaluate(pr_number=42)

        self.assertFalse(result.ok)
        self.assertIn("(#42)", "\n".join(result.messages))

    def test_no_changelog_label_passes_without_entry(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._commit("refactor: rename")

        result = self._evaluate(labels={"no-changelog"})

        self.assertTrue(result.ok)
        self.assertIn("no-changelog", result.messages[0])

    def test_dependencies_label_passes_without_entry(self) -> None:
        self._write("requirements.txt", "fastapi==0.999.0" + chr(10))
        self._write("Dockerfile", "FROM python:3.12-slim" + chr(10))
        self._commit("chore(deps): bump fastapi")

        result = self._evaluate(labels={"dependencies", "python"})

        self.assertTrue(result.ok, result.messages)
        self.assertIn("dependencies", result.messages[0])

    def test_change_outside_operator_visible_paths_passes(self) -> None:
        self._write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        self._write("README.md", "# readme\n")
        self._write("docs/RELEASE_WRAP_0.1.0.md", "wrap\n")
        self._commit("test: add coverage")

        result = self._evaluate()

        self.assertTrue(result.ok)
        self.assertIn("No operator-visible paths changed", result.messages[0])

    def test_breaking_label_requires_breaking_or_upgrade_note_entry(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_entry("Fixed", "- Bumped the value (#42).")
        self._commit("fix!: bump value")

        result = self._evaluate(labels={"breaking", "bug"})

        self.assertFalse(result.ok)
        self.assertIn("'### Breaking changes' or '### Upgrade notes'", "\n".join(result.messages))

    def test_breaking_label_passes_with_upgrade_note(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_entry("Fixed", "- Bumped the value (#42).")
        self._add_entry("Upgrade notes", "- `VALUE` now defaults to 2; set it explicitly (#42).")
        self._commit("fix!: bump value")

        result = self._evaluate(labels={"breaking", "bug"})

        self.assertTrue(result.ok, result.messages)

    def test_wrapped_pr_reference_on_continuation_line_passes(self) -> None:
        self._write("docker-compose.yml", "services: {}\n")
        self._add_entry(
            "Changed",
            "- Hardened the default Compose runtime with a long description that wraps\n"
            "  past eighty columns before the reference (#42).",
        )
        self._commit("fix: harden compose")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)

    def test_entry_under_disallowed_subsection_does_not_count(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_entry("Highlights", "- Bumped the value (#42).")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)

    def test_entry_added_below_unreleased_section_does_not_count(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write("CHANGELOG.md", text + "- Bumped the value (#42).\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)


class ChangelogEntryParsingTests(unittest.TestCase):
    def test_relevant_path_classification(self) -> None:
        for path in (
            "app/main.py",
            "admin_service/main.py",
            "history_service/store.py",
            "scripts/check.py",
            "docker-compose.dev.yml",
            "Dockerfile.history",
            ".env.example",
            "config/config.example.yaml",
            "wiki/Home.md",
            "docs/ROADMAP.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(gate.is_relevant_path(path))
        for path in (
            "CHANGELOG.md",
            "docs/RELEASE_WRAP_0.22.3.md",
            "docs/RELEASE_NOTES_0.22.3.md",
            "tests/test_x.py",
            "README.md",
            ".github/workflows/ci.yml",
        ):
            with self.subTest(path=path):
                self.assertFalse(gate.is_relevant_path(path))

    def test_multi_number_parenthetical_names_each_pr(self) -> None:
        entry = gate.Entry("Fixed", 1, 1, "- Recovered restores (#234, #231, and #230).")

        self.assertTrue(entry.names_pr(231))
        self.assertTrue(entry.names_pr(230))
        self.assertFalse(entry.names_pr(23))
        self.assertFalse(entry.names_pr(2310))

    def test_event_payload_supplies_number_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = Path(raw) / "event.json"
            payload.write_text(
                '{"pull_request": {"number": 7, "labels": [{"name": "bug"}, {"name": "breaking"}]}}',
                encoding="utf-8",
            )
            number, labels = gate.labels_from_event(payload)

        self.assertEqual(number, 7)
        self.assertEqual(labels, {"bug", "breaking"})


if __name__ == "__main__":
    unittest.main()
