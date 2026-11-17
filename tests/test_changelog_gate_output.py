"""The changelog gate's printed text and the revisions it hands to git.

Every file name, git argument and git error the gate prints stays on one line,
so a crafted name can never start a log line of its own (a line that starts
with ``::warning::`` is a workflow command in GitHub Actions). An explicit
``--base`` or ``--head`` is resolved to a commit id before any comparison, so
no value reaches git as an option. The repositories are synthetic and live in
temporary directories.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import changelog_fragments as fragments
from scripts import check_changelog_entry as gate

ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "check_changelog_entry.py"
INJECTED = "::warning::SYNTHETIC_INJECTION"
COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
LITERAL_GIT_TOKENS = {
    "diff",
    "show",
    "ls-tree",
    "cat-file",
    "rev-list",
    "rev-parse",
    "blob",
    "--name-only",
    "--name-status",
    "--unified=0",
    "-z",
    "--no-renames",
    "--parents",
    "--max-count=1",
    "--verify",
    "--quiet",
}


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, input=stdin)
    return completed.stdout.decode("utf-8")


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class _Repository(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = Path(self._tmp.name)
        self.repo = self.scratch / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.test")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        self._write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## v0.1.0 - 2026-01-01\n\n- Initial release.\n")
        self._write("app/service.py", "VALUE = 1\n")
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

    def _commit_index_entry(self, path: bytes, data: bytes, subject: str) -> None:
        """Commit a file whose name the file system may not allow, through the index."""

        blob = _git(self.repo, "hash-object", "-w", "--stdin", stdin=data).strip()
        _git(self.repo, "update-index", "-z", "--index-info", stdin=b"100644 blob " + blob.encode() + b"\t" + path + b"\0")
        _git(self.repo, "commit", "-q", "-m", subject)

    def _main(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gate.main(["--repo", str(self.repo), *argv])
        return code, output.getvalue()

    def assertNoLineStartsWith(self, output: str, prefix: str) -> None:
        offending = [line for line in output.splitlines() if line.lstrip().startswith(prefix)]
        self.assertEqual(offending, [], output)


class PrintedTextStaysOnOneLineTests(_Repository):
    def test_fragment_path_with_a_line_break_is_printed_escaped(self) -> None:
        # One name is refused as named for another pull request, the other as
        # an invalid fragment name: two different print paths.
        for number in (43, 42):
            with self.subTest(number=number):
                _git(self.repo, "checkout", "-q", "-B", "topic", "main")
                name = f"changelog.d/{number}.fixed.md\n{INJECTED}"
                self._commit_index_entry(name.encode(), b"Fixed a thing.\n", "docs: add fragment")

                code, output = self._main("--base", "main", "--head", "topic", "--pr", "42")

                self.assertEqual(code, 1, output)
                self.assertNoLineStartsWith(output, "::")
                self.assertIn(f"changelog.d/{number}.fixed.md\\n{INJECTED}", output)

    def test_fragment_path_with_another_control_character_is_printed_escaped(self) -> None:
        for character, escaped in (("\r", "\\r"), ("\x1b", "\\x1b"), ("\x85", "\\x85"), ("\u2028", "\\u2028")):
            with self.subTest(character=escaped):
                _git(self.repo, "checkout", "-q", "-B", "topic", "main")
                name = f"changelog.d/43.fixed.md{character}{INJECTED}"
                self._commit_index_entry(name.encode(), b"Fixed a thing.\n", "docs: add fragment")

                code, output = self._main("--base", "main", "--head", "topic", "--pr", "42")

                self.assertEqual(code, 1, output)
                self.assertNotIn(character, output)
                self.assertEqual(len(output.splitlines()), len(output.split("\n")) - 1, output)
                self.assertIn(f"changelog.d/43.fixed.md{escaped}{INJECTED}", output)

    def test_undecodable_fragment_name_is_printed_escaped_instead_of_crashing(self) -> None:
        self._commit_index_entry(b"changelog.d/43.fixed.md\xff", b"Fixed a thing.\n", "docs: add fragment")
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment["PYTHONIOENCODING"] = "utf-8:strict"

        completed = subprocess.run(
            [sys.executable, str(GATE_SCRIPT), "--repo", str(self.repo), "--base", "main", "--head", "topic", "--pr", "42"],
            check=False,
            capture_output=True,
            env=environment,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        self.assertIn(b"changelog.d/43.fixed.md\\udcff: name it changelog.d/42.fixed.md", completed.stdout)

    def test_git_error_text_is_printed_on_one_line(self) -> None:
        missing = self.scratch / f"missing\n{INJECTED}"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gate.main(["--repo", str(missing), "--base", "main", "--head", "topic", "--pr", "42"])

        self.assertEqual(code, 2, output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1, output.getvalue())
        self.assertIn(f"missing\\n{INJECTED}", output.getvalue())

    def test_revision_with_a_line_break_is_printed_on_one_line(self) -> None:
        for option in ("--base", "--head"):
            with self.subTest(option=option):
                values = {"--base": "main", "--head": "topic", option: f"main\n{INJECTED}"}

                code, output = self._main("--base", values["--base"], "--head", values["--head"], "--pr", "42")

                self.assertEqual(code, 2, output)
                self.assertNoLineStartsWith(output, "::")
                self.assertEqual(output, f"{option} main\\n{INJECTED} does not name a commit\n")

    def test_ordinary_names_print_unchanged(self) -> None:
        self._write("changelog.d/43.fixed.md", "Fixed a thing.\n")
        self._commit("docs: add fragment")

        code, output = self._main("--base", "main", "--head", "topic", "--pr", "42")

        self.assertEqual(code, 1, output)
        self.assertIn("\n  changelog.d/43.fixed.md: name it changelog.d/42.fixed.md\n", output)

    def test_printable_escapes_only_line_breaking_and_undecodable_characters(self) -> None:
        self.assertEqual(fragments.printable("changelog.d/42.fixed.md: café, tab\tend"), "changelog.d/42.fixed.md: café, tab\\tend")
        self.assertEqual(fragments.printable("a\nb\rc\x00d\x7fe\x85f\u2028g\u2029h\udcffi"), "a\\nb\\rc\\x00d\\x7fe\\x85f\\u2028g\\u2029h\\udcffi")
        self.assertEqual(fragments.printable(fragments.printable("a\nb")), "a\\nb")


@unittest.skipIf(os.name == "nt", "Windows file names cannot hold a line break")
class FragmentToolOutputTests(unittest.TestCase):
    """``changelog_fragments.py`` prints names from the directory listing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
        (self.root / "changelog.d").mkdir()
        (self.root / "changelog.d" / "42.fixed.md").write_text("Fixed a thing.\n", encoding="utf-8")
        (self.root / "changelog.d" / f"43.fixed.md\n{INJECTED}").write_text("Fixed a thing.\n", encoding="utf-8")
        (self.root / "changelog.d" / f"44.fix\r{INJECTED}.md").write_text("Fixed a thing.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _main(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = fragments.main(["--repo", str(self.root), *argv])
        return code, output.getvalue()

    def test_check_and_collect_print_names_escaped(self) -> None:
        for argv in (("check",), ("preview",), ("collect", "--version", "0.2.0", "--date", "2026-02-01")):
            with self.subTest(command=argv[0]):
                code, output = self._main(*argv)

                self.assertEqual(code, 1, output)
                self.assertNotIn("\r", output)
                self.assertEqual([line for line in output.splitlines() if line.lstrip().startswith("::")], [], output)
                self.assertIn(f"changelog.d/43.fixed.md\\n{INJECTED}: bad name", output)
                self.assertIn(f"changelog.d/44.fix\\r{INJECTED}.md: unknown section 'fix\\r{INJECTED}'", output)


class ExplicitRevisionTests(_Repository):
    def setUp(self) -> None:
        super().setUp()
        self._write("app/service.py", "VALUE = 2\n")
        self._write("changelog.d/42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")

    def test_option_like_values_are_refused_before_git_reads_them(self) -> None:
        for option in ("--base", "--head"):
            with self.subTest(option=option):
                target = self.scratch / f"output{option}"
                values = {"--base": "main", "--head": "topic", option: f"--output={target}"}

                code, output = self._main(
                    f"--base={values['--base']}", f"--head={values['--head']}", "--pr", "42", "--labels", "no-changelog"
                )

                self.assertEqual(code, 2, output)
                self.assertEqual(output, f"{option} --output={target} does not name a commit\n")
                self.assertFalse(target.exists())

    def test_values_that_name_no_commit_are_refused(self) -> None:
        tree = _git(self.repo, "rev-parse", "main^{tree}").strip()
        blob = _git(self.repo, "rev-parse", "main:app/service.py").strip()
        missing = "0" * len(tree)
        for value in (tree, blob, missing, f"{tree[:12]}", "main..topic", "no-such-branch", ""):
            for option in ("--base", "--head"):
                with self.subTest(option=option, value=value):
                    values = {"--base": "main", "--head": "topic", option: value}

                    code, output = self._main(f"--base={values['--base']}", f"--head={values['--head']}", "--pr", "42")

                    self.assertEqual(code, 2, output)
                    self.assertTrue(output.startswith(f"{option} {value} does not name a commit"), output)
                    self.assertEqual(len(output.splitlines()), 1, output)

    def test_names_that_resolve_to_a_commit_still_pass(self) -> None:
        _git(self.repo, "tag", "-a", "-m", "base", "base-tag", "main")
        base = _git(self.repo, "rev-parse", "main").strip()
        for base_value in ("main", "base-tag", base, base[:12], "topic~1"):
            with self.subTest(base=base_value):
                code, output = self._main("--base", base_value, "--head", "HEAD", "--pr", "42")

                self.assertEqual(code, 0, output)
                self.assertEqual(output, "Changelog fragment for #42: changelog.d/42.fixed.md (Fixed).\n")

    def test_evaluate_refuses_an_option_like_base(self) -> None:
        target = self.scratch / "output"

        with self.assertRaisesRegex(gate.GateError, "^--base --output=.* does not name a commit$"):
            gate.evaluate(self.repo, base=f"--output={target}", head="topic", pr_number=42, labels={"no-changelog"})

        self.assertFalse(target.exists())


class GitCallFenceTests(_Repository):
    """Every git call the gate makes receives commit ids, never a raw value."""

    def _record_git_calls(self, *argv: str) -> tuple[int, str, list[list[str]]]:
        calls: list[list[str]] = []
        real_run = subprocess.run

        def record(command, *args, **kwargs):
            calls.append(list(command))
            return real_run(command, *args, **kwargs)

        with mock.patch.object(gate.subprocess, "run", side_effect=record):
            code, output = self._main(*argv)
        return code, output, calls

    def _assert_fenced(self, calls: list[list[str]]) -> None:
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call[:3], ["git", "-C", str(self.repo)], call)
            arguments = call[3:]
            for index, argument in enumerate(arguments):
                fenced = "--" in arguments[:index] or "--end-of-options" in arguments[:index]
                if fenced or argument in LITERAL_GIT_TOKENS or argument in {"--", "--end-of-options"}:
                    continue
                with self.subTest(call=call, argument=argument):
                    self.assertRegex(argument, r"^[0-9a-f]{40}(?:[0-9a-f]{24})?(?:\.\.\.[0-9a-f]{40}(?:[0-9a-f]{24})?|:CHANGELOG\.md)?$")

    def test_every_git_call_receives_commit_ids_for_explicit_base_and_head(self) -> None:
        # A fragment and a release cut, so every git call of the gate runs.
        self._write("changelog.d/42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")
        code, output, calls = self._record_git_calls("--base", "main", "--head", "topic", "--pr", "42")
        self.assertEqual(code, 0, output)
        self._assert_fenced(calls)

        _git(self.repo, "checkout", "-q", "-B", "topic", "main")
        text = (self.repo / "CHANGELOG.md").read_text(encoding="utf-8")
        self._write("CHANGELOG.md", text.replace("## Unreleased\n", "## v0.2.0 - 2026-02-01\n\n### Fixed\n\n- Cut the release (#42).\n", 1))
        self._commit("chore: cut release")
        code, output, more_calls = self._record_git_calls("--base", "main", "--head", "topic", "--pr", "42")
        self.assertEqual(code, 0, output)
        self._assert_fenced(more_calls)

        subcommands = {call[3] for call in calls + more_calls}
        self.assertEqual(subcommands, {"rev-parse", "diff", "ls-tree", "cat-file", "show"})

    def test_every_git_call_is_fenced_for_a_merged_head(self) -> None:
        self._write("changelog.d/42.fixed.md", "Bumped the value.\n")
        self._commit("fix: bump value")
        pull_request_head = _git(self.repo, "rev-parse", "topic").strip()
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "topic")

        code, output, calls = self._record_git_calls("--merged-head", pull_request_head, "--head", "HEAD", "--pr", "42")

        self.assertEqual(code, 0, output)
        self._assert_fenced(calls)
        self.assertIn("rev-list", {call[3] for call in calls})


if __name__ == "__main__":
    unittest.main()
