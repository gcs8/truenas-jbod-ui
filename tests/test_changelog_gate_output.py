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
import json
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

    def _record_git_calls(self, *argv: str) -> tuple[int, str, list[list[str]]]:
        calls: list[list[str]] = []
        real_run = subprocess.run

        def record(command, *args, **kwargs):
            calls.append(list(command))
            return real_run(command, *args, **kwargs)

        with mock.patch.object(gate.subprocess, "run", side_effect=record):
            code, output = self._main(*argv)
        return code, output, calls


def _run_script(script: Path, *argv: str, path: str | None = None) -> subprocess.CompletedProcess[bytes]:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONIOENCODING"] = "utf-8:strict"
    if path is not None:
        environment["PATH"] = path
    return subprocess.run([sys.executable, str(script), *argv], check=False, capture_output=True, env=environment)


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

    def test_printable_escapes_line_breaking_and_undecodable_characters(self) -> None:
        self.assertEqual(fragments.printable("changelog.d/42.fixed.md: café, tab\tend"), "changelog.d/42.fixed.md: café, tab\\tend")
        self.assertEqual(fragments.printable("a\nb\rc\x00d\x7fe\x85f\u2028g\u2029h\udcffi"), "a\\nb\\rc\\x00d\\x7fe\\x85f\\u2028g\\u2029h\\udcffi")
        self.assertEqual(fragments.printable(fragments.printable("a\nb")), "a\\nb")

    def test_printable_escapes_format_characters(self) -> None:
        # Bidi controls and zero-width characters can hide or reorder text.
        self.assertEqual(
            fragments.printable("a\u202eb\u202cc\u2066d\u2069e\u200bf\u200dg\ufeffh"),
            "a\\u202eb\\u202cc\\u2066d\\u2069e\\u200bf\\u200dg\\ufeffh",
        )
        # A backslash is kept as it is: the escaped and the literal text read the same.
        self.assertEqual(fragments.printable("a\\u202eb"), "a\\u202eb")

    def test_event_read_error_is_printed_on_one_line(self) -> None:
        missing = self.scratch / f"event\n{INJECTED}.json"
        unparsable = self.scratch / f"bad\n{INJECTED}.json"
        unparsable.write_text("{not json", encoding="utf-8")
        for path in (missing, unparsable):
            with self.subTest(path=path.name):
                escaped = path.name.replace("\n", "\\n")

                code, output = self._main("--base", "main", "--head", "topic", "--event", str(path))

                self.assertEqual(code, 2, output)
                self.assertEqual(len(output.splitlines()), 1, output)
                self.assertTrue(output.startswith(f"could not read event payload {path.parent}/{escaped}: "), output)

    def test_fragment_text_with_an_escaped_character_is_refused(self) -> None:
        self._write("changelog.d/42.fixed.md", f"Fixed a\x1b{INJECTED} thing.\n")
        self._commit("docs: add fragment")

        code, output = self._main("--base", "main", "--head", "topic", "--pr", "42")

        self.assertEqual(code, 1, output)
        self.assertNotIn("\x1b", output)
        self.assertIn("changelog.d/42.fixed.md: contains a control or formatting character (\\x1b)", output)


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

    def test_sub_directory_name_is_printed_escaped(self) -> None:
        (self.root / "changelog.d" / f"sub\n{INJECTED}").mkdir()

        code, output = self._main("check")

        self.assertEqual(code, 1, output)
        self.assertNoMarkerLine(output)
        self.assertIn(f"changelog.d/sub\\n{INJECTED}/: fragments live directly in changelog.d/, not in a subdirectory", output)

    def assertNoMarkerLine(self, output: str) -> None:
        self.assertEqual([line for line in output.splitlines() if line.lstrip().startswith("::")], [], output)


@unittest.skipIf(os.name == "nt", "the fragment tool tests use POSIX file names")
class FragmentTextTests(unittest.TestCase):
    """Fragment text never holds a character that printable() would escape, so
    preview and collect can write it as it is."""

    ESCAPED = (
        ("\r", "\\r"),
        ("\x1b", "\\x1b"),
        ("\t", "\\t"),
        ("\x0b", "\\x0b"),
        ("\x0c", "\\x0c"),
        ("\x85", "\\x85"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
        ("\u202e", "\\u202e"),
        ("\u200b", "\\u200b"),
        ("\ufeff", "\\ufeff"),
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.changelog = b"# Changelog\n\n## Unreleased\n\n## v0.1.0 - 2026-01-01\n\n- Initial release.\n"
        (self.root / "CHANGELOG.md").write_bytes(self.changelog)
        (self.root / "changelog.d").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _main(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fragments.main(["--repo", str(self.root), *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_refuses_text_with_an_escaped_character_and_names_it(self) -> None:
        fragment = self.root / "changelog.d" / "42.fixed.md"
        for character, escaped in self.ESCAPED:
            with self.subTest(character=escaped):
                fragment.write_bytes(f"Fixed a{character}{INJECTED} thing.\n".encode())
                problem = f"changelog.d/42.fixed.md: contains a control or formatting character ({escaped})"

                code, stdout, stderr = self._main("check")
                self.assertEqual((code, stderr), (1, ""), stdout)
                self.assertTrue(stdout.startswith(problem), stdout)
                self.assertNotIn(character, stdout)

                code, stdout, stderr = self._main("preview")
                self.assertEqual(code, 1, stdout + stderr)
                self.assertEqual(stdout, "## Unreleased\n")
                self.assertTrue(stderr.startswith(f"note: invalid fragment {problem}"), stderr)
                self.assertNotIn(character, stderr)

                code, stdout, _stderr = self._main("collect", "--version", "0.2.0", "--date", "2026-02-01")
                self.assertEqual(code, 1, stdout)
                self.assertIn(problem, stdout)
                self.assertNotIn(character, stdout)
                self.assertEqual((self.root / "CHANGELOG.md").read_bytes(), self.changelog)

    def test_ordinary_line_breaks_are_still_accepted(self) -> None:
        (self.root / "changelog.d" / "42.fixed.md").write_bytes(b"Fixed a thing\r\nover two lines.\r\n")
        (self.root / "changelog.d" / "43.fixed.md").write_bytes("Fixed caf\u00e9 \u2014 \u00fcber.\n".encode())

        self.assertEqual(self._main("check"), (0, "2 changelog fragment(s) valid.\n", ""))
        code, stdout, stderr = self._main("preview")

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("- Fixed a thing\n  over two lines (#42).\n- Fixed caf\u00e9 \u2014 \u00fcber (#43).\n", stdout)

    def test_existing_release_heading_is_printed_escaped(self) -> None:
        (self.root / "changelog.d" / "42.fixed.md").write_text("Fixed a thing.\n", encoding="utf-8")
        for character, escaped in (("\u2028", "\\u2028"), ("\x1b", "\\x1b")):
            with self.subTest(character=escaped):
                text = f"# Changelog\n\n## Unreleased\n\n## v0.2.0 - 2026-02-01{character}{INJECTED}\n"
                (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")

                code, stdout, _stderr = self._main("collect", "--version", "0.2.0", "--date", "2026-02-01")

                self.assertEqual(code, 1, stdout)
                self.assertNotIn(character, stdout)
                self.assertTrue(
                    stdout.startswith(f"CHANGELOG.md already has '## v0.2.0 - 2026-02-01{escaped}{INJECTED}'; refusing"),
                    stdout,
                )
                # The message keeps its own line breaks.
                self.assertIn("\n  changelog.d/42.fixed.md\n", stdout)

    def test_cleanup_error_text_is_printed_escaped(self) -> None:
        (self.root / "changelog.d" / "42.fixed.md").write_text("Fixed a thing.\n", encoding="utf-8")

        def refuse(path: Path) -> None:
            raise OSError(f"synthetic\n{INJECTED}")

        with mock.patch.object(fragments, "_remove_fragment", side_effect=refuse):
            code, stdout, _stderr = self._main("collect", "--version", "0.2.0", "--date", "2026-02-01")

        self.assertEqual(code, 1, stdout)
        self.assertEqual([line for line in stdout.splitlines() if line.startswith("::")], [], stdout)
        self.assertIn(
            f"\n  changelog.d/42.fixed.md: still present (could not check and delete it: OSError: synthetic\\n{INJECTED}); "
            "its text is already in '## v0.2.0 - 2026-02-01'\n",
            stdout,
        )


@unittest.skipIf(os.name == "nt", "the program name test uses a POSIX file name")
class ParserOutputTests(unittest.TestCase):
    """What the argument parser writes to stderr from the command line is escaped."""

    FRAGMENT_SCRIPT = ROOT / "scripts" / "changelog_fragments.py"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_escaped_like_ordinary(self, script: Path, before: tuple[str, ...], value: str, escaped: str) -> None:
        ordinary = _run_script(script, *before, "ordinary-value")
        crafted = _run_script(script, *before, value)

        self.assertEqual(crafted.returncode, 2, crafted.stderr)
        self.assertEqual(crafted.stdout, b"")
        stderr = crafted.stderr.decode("utf-8")
        self.assertEqual([line for line in stderr.splitlines() if line.startswith("::")], [], stderr)
        # Usage and message are byte-identical to an ordinary value's, except for the value itself.
        self.assertEqual(stderr, ordinary.stderr.decode("utf-8").replace("ordinary-value", escaped))

    def test_unrecognized_arguments_are_printed_escaped(self) -> None:
        cases = (
            (GATE_SCRIPT, ("--base", "main", "--pr", "42")),
            (self.FRAGMENT_SCRIPT, ("check",)),
            (self.FRAGMENT_SCRIPT, ("collect", "--version", "0.2.0", "--date", "2026-02-01")),
        )
        for script, before in cases:
            for character, escaped in (("\n", "\\n"), ("\r", "\\r"), ("\x1b", "\\x1b"), ("\u2028", "\\u2028")):
                with self.subTest(script=script.name, before=before[0], character=escaped):
                    self._assert_escaped_like_ordinary(
                        script, before, f"bad{character}{INJECTED}", f"bad{escaped}{INJECTED}"
                    )

    def test_ambiguous_option_is_printed_escaped(self) -> None:
        # --he matches both --head and --help.
        ordinary = _run_script(GATE_SCRIPT, "--base", "main", "--he=ordinary-value")
        crafted = _run_script(GATE_SCRIPT, "--base", "main", f"--he=bad\n{INJECTED}")

        self.assertEqual((ordinary.returncode, crafted.returncode), (2, 2), crafted.stderr)
        self.assertIn(b"ambiguous option: --he=ordinary-value could match", ordinary.stderr)
        self.assertEqual(
            crafted.stderr.decode("utf-8"),
            ordinary.stderr.decode("utf-8").replace("ordinary-value", f"bad\\n{INJECTED}"),
        )

    def test_program_name_is_printed_escaped(self) -> None:
        for script, argv in ((GATE_SCRIPT, ("--bogus",)), (self.FRAGMENT_SCRIPT, ("collect",))):
            with self.subTest(script=script.name):
                link = self.scratch / f"{script.stem}\n{INJECTED}.py"
                link.symlink_to(script)

                completed = _run_script(link, *argv)

                self.assertEqual(completed.returncode, 2, completed.stderr)
                stderr = completed.stderr.decode("utf-8")
                self.assertEqual([line for line in stderr.splitlines() if line.startswith("::")], [], stderr)
                self.assertTrue(stderr.startswith(f"usage: {script.stem}\\n{INJECTED}.py"), stderr)
                self.assertIn(f"\n{script.stem}\\n{INJECTED}.py", stderr)


class BadInputTests(_Repository):
    """Input of the wrong kind ends with a fixed message, never a traceback."""

    def test_event_payload_of_the_wrong_shape_is_refused(self) -> None:
        self._write("changelog.d/42.fixed.md", "Fixed a thing.\n")
        self._commit("docs: add fragment")
        payloads = {
            "list root": [],
            "number pull request": {"pull_request": 5},
            "string pull request": {"pull_request": "42"},
            "list number": {"pull_request": {"number": [42]}},
            "object number": {"pull_request": {"number": {"value": 42}}},
            "number labels": {"pull_request": {"number": 42, "labels": 7}},
            "null labels": {"pull_request": {"number": 42, "labels": None}},
        }
        for name, payload in payloads.items():
            with self.subTest(payload=name):
                event = self.scratch / "event.json"
                event.write_text(json.dumps(payload), encoding="utf-8")

                completed = _run_script(GATE_SCRIPT, "--repo", str(self.repo), "--base", "main", "--head", "topic", "--event", str(event))

                self.assertEqual((completed.returncode, completed.stderr), (2, b""), completed.stderr)
                self.assertEqual(
                    completed.stdout.decode("utf-8"),
                    f"could not read event payload {event}: it is not the payload of a pull request event\n",
                )

        event = self.scratch / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 42, "labels": [{"name": "bug"}]}}), encoding="utf-8")
        completed = _run_script(GATE_SCRIPT, "--repo", str(self.repo), "--base", "main", "--head", "topic", "--event", str(event))
        self.assertEqual((completed.returncode, completed.stderr), (0, b""), completed.stderr)
        self.assertEqual(completed.stdout, b"Changelog fragment for #42: changelog.d/42.fixed.md (Fixed).\n")

    def test_missing_git_is_reported_without_a_traceback(self) -> None:
        empty = self.scratch / "no-git"
        empty.mkdir()

        completed = _run_script(GATE_SCRIPT, "--repo", str(self.repo), "--base", "main", "--head", "topic", "--pr", "42", path=str(empty))

        self.assertEqual((completed.returncode, completed.stderr), (2, b""), completed.stderr)
        self.assertEqual(completed.stdout, b"could not run git; install git and put it on PATH\n")

    def test_changelog_that_is_not_utf8_is_refused(self) -> None:
        script = ROOT / "scripts" / "changelog_fragments.py"
        root = self.scratch / "tree"
        (root / "changelog.d").mkdir(parents=True)
        changelog = b"# Changelog\n\n## Unreleased\n\n- Caf\xe9.\n"
        (root / "CHANGELOG.md").write_bytes(changelog)
        (root / "changelog.d" / "42.fixed.md").write_text("Fixed a thing.\n", encoding="utf-8")

        preview = _run_script(script, "--repo", str(root), "preview")
        collect = _run_script(script, "--repo", str(root), "collect", "--version", "0.2.0", "--date", "2026-02-01")

        self.assertEqual((preview.returncode, preview.stdout, preview.stderr), (1, b"", b"CHANGELOG.md is not UTF-8 text\n"))
        self.assertEqual((collect.returncode, collect.stdout, collect.stderr), (1, b"CHANGELOG.md is not UTF-8 text\n", b""))
        self.assertEqual((root / "CHANGELOG.md").read_bytes(), changelog)
        self.assertTrue((root / "changelog.d" / "42.fixed.md").exists())


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

                code, output, calls = self._record_git_calls(
                    f"--base={values['--base']}", f"--head={values['--head']}", "--pr", "42", "--labels", "no-changelog"
                )

                self.assertEqual(code, 2, output)
                self.assertEqual(output, f"{option} --output={target} does not name a commit\n")
                self.assertFalse(target.exists())
                # Refused before any git process saw the value, not by git itself.
                self.assertEqual([call for call in calls if any("--output=" in argument for argument in call)], [])

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

    def test_resolver_output_that_is_not_a_full_commit_id_is_refused(self) -> None:
        commit = _git(self.repo, "rev-parse", "main").strip()
        real_run = subprocess.run
        for answer in (commit[:12], commit.upper(), f"{commit}\n{commit}", f"{commit}0", "--output=x"):
            with self.subTest(answer=answer):
                calls: list[list[str]] = []

                def answer_rev_parse(command, *args, **kwargs):
                    calls.append(list(command))
                    if command[3] == "rev-parse":
                        return subprocess.CompletedProcess(command, 0, stdout=f"{answer}\n", stderr="")
                    return real_run(command, *args, **kwargs)

                with mock.patch.object(gate.subprocess, "run", side_effect=answer_rev_parse):
                    with self.assertRaisesRegex(gate.GateError, "^--base main does not name a commit$"):
                        gate.evaluate(self.repo, base="main", head="topic", pr_number=42, labels=set())

                self.assertEqual([call[3] for call in calls], ["rev-parse"])


class GitCallFenceTests(_Repository):
    """Every git call the gate makes receives commit ids, never a raw value."""

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
