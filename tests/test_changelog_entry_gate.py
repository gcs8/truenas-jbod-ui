from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
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

    def _add_fragment(self, name: str, text: str) -> None:
        self._write(f"changelog.d/{name}", text)

    def test_code_change_without_entry_fails_with_instructions(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("No changelog fragment added for #42", joined)
        self.assertIn("add changelog.d/42.fixed.md", joined)
        self.assertIn("fixed", joined)
        self.assertIn("no-changelog", joined)

    def test_code_change_with_entry_passes(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("Fixed", result.messages[0])

    def test_release_candidate_section_with_entry_passes_without_unreleased(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        text = text.replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1)
        text = text.replace(
            "### Fixed\n\n",
            "### Fixed\n\n- Prepared the release candidate (#42).\n",
            1,
        )
        self._write("CHANGELOG.md", text)
        self._commit("chore: prepare release")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("v0.2.0", result.messages[0])

    def test_release_candidate_section_accepts_prerelease_and_build_metadata(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        text = text.replace("## Unreleased", "## v1.2.3-rc.1+build.5 - 2026-02-01", 1)
        text = text.replace(
            "### Fixed\n\n",
            "### Fixed\n\n- Prepared the release candidate (#42).\n",
            1,
        )
        self._write("CHANGELOG.md", text)
        self._commit("chore: prepare release")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("v1.2.3-rc.1+build.5", result.messages[0])

    def test_entry_for_a_different_pr_number_fails(self) -> None:
        # A fragment is named for the pull request that adds it; one named
        # for another number is refused and the gate names the right file.
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("41.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")

        result = self._evaluate(pr_number=42)

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/41.fixed.md", joined)
        self.assertIn("changelog.d/42.fixed.md", joined)

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

    def test_bullet_appended_to_a_shipped_release_section_fails_with_the_fix(self) -> None:
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write("CHANGELOG.md", text.replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1))
        self._commit("chore: cut v0.2.0")
        _git(self.repo, "branch", "-f", "main", "topic")

        self._write("app/service.py", "VALUE = 2\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write(
            "CHANGELOG.md",
            text.replace("### Fixed\n\n", "### Fixed\n\n- Bumped the value (#42).\n", 1),
        )
        self._commit("fix: bump value")

        result = self._evaluate()

        # Editing CHANGELOG.md no longer admits a pull request, wherever the
        # bullet lands; the failure names the fragment to add instead.
        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("No changelog fragment added for #42", joined)
        self.assertIn("CHANGELOG.md", joined)
        self.assertIn("add changelog.d/42.fixed.md", joined)

    def test_missing_entry_names_the_absent_unreleased_section(self) -> None:
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write("CHANGELOG.md", text.replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1))
        self._commit("chore: cut v0.2.0")
        _git(self.repo, "branch", "-f", "main", "topic")

        self._write("app/service.py", "VALUE = 2\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        # Fragments need no Unreleased heading, so the message no longer asks
        # for one; it names the fragment to add.
        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("No changelog fragment added for #42", joined)
        self.assertNotIn("Insert '## Unreleased'", joined)

    def test_change_outside_operator_visible_paths_requires_escape_label(self) -> None:
        self._write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        self._write("README.md", "# readme\n")
        self._write("docs/RELEASE_WRAP_0.1.0.md", "wrap\n")
        self._commit("test: add coverage")

        result = self._evaluate()

        self.assertFalse(result.ok)
        self.assertIn("no-changelog", "\n".join(result.messages))

    def test_change_outside_operator_visible_paths_with_escape_label_passes(self) -> None:
        self._write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        self._commit("test: add coverage")

        result = self._evaluate(labels={"no-changelog"})

        self.assertTrue(result.ok, result.messages)
        self.assertIn("no-changelog", result.messages[0])

    def test_breaking_label_requires_breaking_or_upgrade_note_entry(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.fixed.md", "Bumped the value.\n")
        self._commit("fix!: bump value")

        result = self._evaluate(labels={"breaking", "bug"})

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/42.breaking.md", joined)
        self.assertIn("changelog.d/42.upgrade.md", joined)

    def test_breaking_label_passes_with_upgrade_note(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.fixed.md", "Bumped the value.\n")
        self._add_fragment("42.upgrade.md", "`VALUE` now defaults to 2; set it explicitly.\n")
        self._commit("fix!: bump value")

        result = self._evaluate(labels={"breaking", "bug"})

        self.assertTrue(result.ok, result.messages)

    def test_wrapped_pr_reference_on_continuation_line_passes(self) -> None:
        # A release cut still names its own pull request in CHANGELOG.md, and
        # the (#42) may sit on the wrapped continuation line of the bullet.
        self._write("docker-compose.yml", "services: {}\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        text = text.replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1)
        text = text.replace(
            "### Fixed\n\n",
            "### Fixed\n\n"
            "- Hardened the default Compose runtime with a long description that wraps\n"
            "  past eighty columns before the reference (#42).\n",
            1,
        )
        self._write("CHANGELOG.md", text)
        self._commit("chore: cut v0.2.0")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("v0.2.0", result.messages[0])

    def test_breaking_release_cut_with_wrapped_upgrade_reference_passes(self) -> None:
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        text = text.replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1)
        for upgrade, expected in ((True, True), (False, False)):
            with self.subTest(upgrade=upgrade):
                _git(self.repo, "reset", "-q", "--hard", "main")
                self._write("docker-compose.yml", "services: {}\n")
                body = "### Fixed\n\n"
                if upgrade:
                    body = (
                        "### Upgrade notes\n\n"
                        "- `VALUE` now defaults to 2; set it explicitly before upgrading, as\n"
                        "  the wrapped reference shows (#42).\n\n" + body
                    )
                else:
                    body += (
                        "- Hardened the default Compose runtime, with a reference that\n"
                        "  wraps (#42).\n"
                    )
                self._write("CHANGELOG.md", text.replace("### Fixed\n\n", body, 1))
                self._commit("chore: cut v0.2.0")

                result = self._evaluate(labels={"breaking"})

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    self.assertIn("'### Upgrade notes'", "\n".join(result.messages))

    def test_wrapped_fragment_text_passes(self) -> None:
        self._write("docker-compose.yml", "services: {}\n")
        self._add_fragment(
            "42.changed.md",
            "Hardened the default Compose runtime with a long description that wraps\n"
            "past eighty columns onto a continuation line.\n",
        )
        self._commit("fix: harden compose")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)

    def test_entry_under_disallowed_subsection_does_not_count(self) -> None:
        # Highlights are written at release time, so there is no highlights
        # fragment section.
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.highlights.md", "Bumped the value.\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)
        self.assertIn("unknown section 'highlights'", "\n".join(result.messages))

    def test_entry_added_below_unreleased_section_does_not_count(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write("CHANGELOG.md", text + "- Bumped the value (#42).\n")
        self._commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)


    def test_advisory_mode_turns_a_missing_entry_into_a_pass_with_guidance(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._commit("fix: bump value")

        result = gate.evaluate(
            self.repo,
            base="main",
            head="topic",
            pr_number=42,
            labels=set(),
            advisory=True,
        )

        self.assertTrue(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("Advisory", joined)
        self.assertIn("No changelog fragment added for #42", joined)
        self.assertIn("a maintainer", joined)

    def test_advisory_mode_still_reports_a_satisfied_gate_as_a_plain_pass(self) -> None:
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")

        result = gate.evaluate(
            self.repo,
            base="main",
            head="topic",
            pr_number=42,
            labels=set(),
            advisory=True,
        )

        self.assertTrue(result.ok, result.messages)
        self.assertNotIn("Advisory", "\n".join(result.messages))

    def test_advisory_mode_passes_a_tests_only_change_that_cannot_be_labelled(self) -> None:
        self._write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        self._commit("test: add a case")

        blocking = self._evaluate()
        advisory = gate.evaluate(
            self.repo,
            base="main",
            head="topic",
            pr_number=42,
            labels=set(),
            advisory=True,
        )

        self.assertFalse(blocking.ok)
        self.assertTrue(advisory.ok, advisory.messages)
        self.assertIn("no-changelog", "\n".join(advisory.messages))

    def _merged_pull_request(self, *, takes_main_and_drops_its_fragment: bool = False) -> tuple[str, str, str]:
        """Build the commit GitHub checks out for a pull request.

        Another pull request merges its fragment into ``main`` after this one
        opened. Returns ``(base when opened, pull request head, merge commit)``;
        the merge commit has ``main`` as first parent and the head as second.
        """

        base_when_opened = _git(self.repo, "rev-parse", "main").strip()
        self._write("app/service.py", "VALUE = 2\n")
        self._add_fragment("42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")
        _git(self.repo, "checkout", "-q", "main")
        self._write("app/other.py", "OTHER = 1\n")
        self._add_fragment("41.fixed.md", "Fixed another thing.\n")
        self._commit("fix: another pull request merged first")
        _git(self.repo, "checkout", "-q", "topic")
        if takes_main_and_drops_its_fragment:
            _git(self.repo, "merge", "-q", "--no-edit", "main")
            (self.repo / "changelog.d" / "41.fixed.md").unlink()
            self._commit("chore: drop a fragment")
        head = _git(self.repo, "rev-parse", "topic").strip()
        _git(self.repo, "checkout", "-q", "--detach", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "--no-edit", "topic")
        merge_commit = _git(self.repo, "rev-parse", "HEAD").strip()
        return base_when_opened, head, merge_commit

    def test_merge_commit_base_ignores_a_fragment_another_pull_request_merged_first(self) -> None:
        base_when_opened, head, merge_commit = self._merged_pull_request()

        base = gate.merge_commit_base(self.repo, head=merge_commit, pull_request_head=head)

        self.assertEqual(base, _git(self.repo, "rev-parse", "main").strip())
        self.assertNotEqual(base, base_when_opened)
        result = gate.evaluate(self.repo, base=base, head=merge_commit, pr_number=42, labels=set())
        self.assertTrue(result.ok, result.messages)
        # The base recorded when the pull request opened charges it with the
        # other pull request's fragment.
        stale = gate.evaluate(self.repo, base=base_when_opened, head=merge_commit, pr_number=42, labels=set())
        self.assertFalse(stale.ok)
        self.assertIn("changelog.d/41.fixed.md", "\n".join(stale.messages))

    def test_merge_commit_base_refuses_a_dropped_fragment_that_reached_the_base_later(self) -> None:
        _base_when_opened, head, merge_commit = self._merged_pull_request(takes_main_and_drops_its_fragment=True)

        base = gate.merge_commit_base(self.repo, head=merge_commit, pull_request_head=head)
        result = gate.evaluate(self.repo, base=base, head=merge_commit, pr_number=42, labels=set())

        self.assertFalse(result.ok)
        self.assertTrue(result.protected)
        joined = "\n".join(result.messages)
        self.assertIn("already on the base branch", joined)
        self.assertIn("changelog.d/41.fixed.md", joined)

    def test_merge_commit_base_refuses_a_commit_that_is_not_this_pull_request_merge(self) -> None:
        base_when_opened, head, merge_commit = self._merged_pull_request()

        cases = {
            "not a merge": (head, head),
            "second parent is another commit": (merge_commit, base_when_opened),
            "parents swapped": (merge_commit, _git(self.repo, "rev-parse", "main").strip()),
        }
        for label, (checked_out, pull_request_head) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(gate.GateError, "is not the merge of pull request head"):
                gate.merge_commit_base(self.repo, head=checked_out, pull_request_head=pull_request_head)

    def test_merge_commit_base_refuses_a_merge_with_more_than_two_parents(self) -> None:
        base_when_opened, head, _merge_commit = self._merged_pull_request()
        _git(self.repo, "checkout", "-q", "-b", "extra", base_when_opened)
        self._write("app/extra.py", "EXTRA = 1\n")
        self._commit("feat: a third line of history")
        _git(self.repo, "checkout", "-q", "--detach", "main")
        _git(self.repo, "merge", "-q", "--no-edit", "topic", "extra")
        three_parent_merge = _git(self.repo, "rev-parse", "HEAD").strip()
        parents = _git(self.repo, "rev-list", "--parents", "--max-count=1", three_parent_merge).split()[1:]
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[1], head)

        with self.assertRaisesRegex(gate.GateError, "is not the merge of pull request head"):
            gate.merge_commit_base(self.repo, head=three_parent_merge, pull_request_head=head)

    def test_merge_commit_base_checks_the_pull_request_head_before_running_git(self) -> None:
        _base_when_opened, head, merge_commit = self._merged_pull_request()

        cases = {
            "abbreviated": head[:12],
            "upper case": head.upper(),
            "one character short": head[:-1],
            "one character long": head + "0",
            "option-like": "--help",
            "ref name": "topic",
            "empty": "",
            "trailing newline": head + "\n",
            "leading space": " " + head,
        }
        for label, pull_request_head in cases.items():
            with (
                self.subTest(label),
                mock.patch.object(gate, "_git", side_effect=AssertionError("git must not run")),
                self.assertRaisesRegex(gate.GateError, "must be a full lowercase commit id"),
            ):
                gate.merge_commit_base(self.repo, head=merge_commit, pull_request_head=pull_request_head)

    def test_merge_commit_base_never_reads_the_checked_out_commit_as_an_option(self) -> None:
        _base_when_opened, head, _merge_commit = self._merged_pull_request()

        # Without the end-of-options marker, --all would list the merge commit
        # that is checked out and the gate would accept it.
        for option in ("--all", "--max-count=5", "--help", "-n1"):
            with self.subTest(option), self.assertRaisesRegex(gate.GateError, "bad revision"):
                gate.merge_commit_base(self.repo, head=option, pull_request_head=head)
            with self.subTest(f"command line {option}"), contextlib.redirect_stdout(io.StringIO()) as refused:
                code = gate.main(
                    ["--repo", str(self.repo), f"--head={option}", "--merged-head", head, "--pr", "42", "--labels", ""]
                )
            with self.subTest(f"command line {option}"):
                self.assertEqual(code, 2)
                self.assertIn("bad revision", refused.getvalue())

    def test_command_line_takes_the_base_from_the_merge_commit(self) -> None:
        base_when_opened, head, merge_commit = self._merged_pull_request()
        common = ["--repo", str(self.repo), "--head", merge_commit, "--pr", "42", "--labels", ""]

        with contextlib.redirect_stdout(io.StringIO()) as passed:
            self.assertEqual(gate.main([*common, "--merged-head", head]), 0)
        self.assertIn("Fixed", passed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as refused:
            self.assertEqual(gate.main([*common, "--merged-head", base_when_opened]), 2)
        self.assertIn("is not the merge of pull request head", refused.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main([*common, "--base", base_when_opened]), 1)
        for arguments in (common, [*common, "--base", "main", "--merged-head", head]):
            with self.subTest(arguments=arguments[8:]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    gate.main(arguments)
                self.assertEqual(stopped.exception.code, 2)


class ChangelogEntryParsingTests(unittest.TestCase):
    def test_release_heading_uses_strict_semver_core_and_identifier_grammar(self) -> None:
        for valid in (
            "## v0.0.0",
            "## v1.2.3-rc.1+build.5",
            "## v1.2.3-x-y-z+001 - 2026-02-01",
        ):
            with self.subTest(valid=valid):
                self.assertIsNotNone(gate.RELEASE_HEADING.fullmatch(valid))

        for invalid in (
            "## v01.2.3",
            "## v1.02.3",
            "## v1.2.03",
            "## v1.2.3-01",
            "## v1.2.3-rc..1",
            "## v1.2.3+build..5",
            "## v1١.2.3",
            "## v1.2.3-1١",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(gate.RELEASE_HEADING.fullmatch(invalid))

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
            "docs/archive/RELEASE_WRAP_0.22.2.md",
            "docs/archive/RELEASE_NOTES_0.22.2.md",
            "docs/archive/ROADMAP_HISTORY.md",
            "docs/archive/README.md",
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
