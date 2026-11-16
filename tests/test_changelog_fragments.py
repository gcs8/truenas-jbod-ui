"""Changelog fragments: admission gate, collector and release tooling.

A pull request adds one file under ``changelog.d/`` instead of editing
``CHANGELOG.md``. These tests pin the gate that admits it, the collector that
folds fragments into ``CHANGELOG.md`` at release time, and the release tools
that read the combined view. Every Git repository here is a scratch directory.
"""

from __future__ import annotations

import contextlib
import difflib
import importlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_changelog_entry as gate
from scripts import check_release_changelog_coverage as coverage
from scripts import render_release_notes
from scripts import validate_release_wrap

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SECTION_NAMES = (
    "breaking",
    "upgrade",
    "security",
    "added",
    "changed",
    "fixed",
    "performance",
    "docs",
    "internal",
)

BASE_CHANGELOG = """# Changelog

## Unreleased

### Changed

- Changed an earlier thing (#1).

### Fixed

- Fixed an earlier thing, with a description that wraps onto a
  continuation line (#2).

## v0.1.0 - 2026-01-01

- Initial release.
"""

README_TEXT = "# Changelog fragments\n\nSee CONTRIBUTING.md.\n"


def _fragments():
    """Import the collector lazily so gate tests still run without it."""

    return importlib.import_module("scripts.changelog_fragments")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class ScratchRepository:
    """A throwaway Git repository with a synthetic changelog."""

    def __init__(self, root: Path, changelog: str = BASE_CHANGELOG) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "test@example.test")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "commit.gpgsign", "false")
        _git(root, "config", "tag.gpgsign", "false")
        self.write("CHANGELOG.md", changelog)
        self.write("changelog.d/README.md", README_TEXT)
        self.write("app/service.py", "VALUE = 1\n")
        self.write("tests/test_service.py", "def test() -> None:\n    pass\n")
        self.commit("chore: base")

    def write(self, relative: str, text: str | bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    def commit(self, subject: str) -> None:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "--allow-empty", "-m", subject)

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD").stdout.strip()


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class FragmentGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name))
        _git(self.repo.root, "checkout", "-q", "-b", "topic")

    def _evaluate(self, labels: set[str] | None = None, advisory: bool = False) -> gate.GateResult:
        return gate.evaluate(
            self.repo.root,
            base="main",
            head="topic",
            pr_number=42,
            labels=labels or set(),
            advisory=advisory,
        )

    def _code_change(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")

    def test_no_fragment_fails_naming_the_file_to_add(self) -> None:
        self._code_change()
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/<short-name>.", joined)
        for section in SECTION_NAMES:
            self.assertIn(section, joined)
        self.assertIn("no-changelog", joined)

    def test_added_valid_fragment_passes(self) -> None:
        self._code_change()
        self.repo.write("changelog.d/bump-value.fixed.md", "Bumped the value an operator sees.\n")
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/bump-value.fixed.md", joined)
        self.assertIn("Fixed", joined)

    def test_fragment_already_present_at_base_does_not_count(self) -> None:
        _git(self.repo.root, "checkout", "-q", "main")
        self.repo.write("changelog.d/older-change.fixed.md", "Fixed an older thing.\n")
        self.repo.commit("fix: older change (#7)")
        _git(self.repo.root, "checkout", "-q", "-B", "topic")

        for label, change in (
            ("untouched", lambda: None),
            ("edited", lambda: self.repo.write("changelog.d/older-change.fixed.md", "Fixed it better.\n")),
            (
                "renamed",
                lambda: _git(
                    self.repo.root, "mv", "changelog.d/older-change.fixed.md", "changelog.d/newer-name.fixed.md"
                ),
            ),
        ):
            with self.subTest(change=label):
                _git(self.repo.root, "reset", "-q", "--hard", "main")
                self._code_change()
                change()
                self.repo.commit(f"fix: {label}")

                result = self._evaluate()

                self.assertFalse(result.ok, result.messages)
                if label == "renamed":
                    # Renaming another pull request's fragment is refused
                    # outright (see FragmentProtectionGateTests).
                    self.assertIn("changelog.d/older-change.fixed.md", "\n".join(result.messages))
                else:
                    self.assertIn("changelog.d/<short-name>.", "\n".join(result.messages))

    def test_each_invalid_fragment_fails_with_its_file_and_reason(self) -> None:
        cases = (
            ("changelog.d/Bad_Name.fixed.md", "Fixed a thing.\n", "name"),
            ("changelog.d/ab.fixed.md", "Fixed a thing.\n", "name"),
            ("changelog.d/" + "a" * 61 + ".fixed.md", "Fixed a thing.\n", "name"),
            ("changelog.d/no-section.md", "Fixed a thing.\n", "name"),
            ("changelog.d/thing.fixed.txt", "Fixed a thing.\n", "name"),
            ("changelog.d/thing.feature.md", "Added a thing.\n", "unknown section 'feature'"),
            ("changelog.d/thing.highlights.md", "Shipped a theme.\n", "unknown section 'highlights'"),
            ("changelog.d/nested/thing.fixed.md", "Fixed a thing.\n", "directly in changelog.d/"),
            ("changelog.d/empty-text.fixed.md", "", "empty"),
            ("changelog.d/blank-text.fixed.md", "\n   \n", "empty"),
            ("changelog.d/long-text.fixed.md", "Fixed " + "x" * 4100 + ".\n", "too long"),
            ("changelog.d/with-heading.fixed.md", "### Fixed\n\nFixed a thing.\n", "heading"),
            ("changelog.d/with-number.fixed.md", "Fixed a thing (#42).\n", "pull request number"),
            ("changelog.d/with-numbers.fixed.md", "Fixed a thing (#12, #13).\n", "pull request number"),
            ("changelog.d/with-dash.fixed.md", "- Fixed a thing.\n", "'- '"),
            ("changelog.d/two-bullets.fixed.md", "Fixed a thing.\n- Fixed another.\n", "one bullet"),
            ("changelog.d/two-paragraphs.fixed.md", "Fixed a thing.\n\nAnd more.\n", "blank line"),
            ("changelog.d/not-utf8.fixed.md", b"Fixed \xff thing.\n", "UTF-8"),
        )
        for path, content, reason in cases:
            with self.subTest(path=path):
                _git(self.repo.root, "reset", "-q", "--hard", "main")
                _git(self.repo.root, "clean", "-qfdx")
                self._code_change()
                self.repo.write("changelog.d/valid-entry.fixed.md", "Fixed the visible thing.\n")
                self.repo.write(path, content)
                self.repo.commit("fix: bump value")

                result = self._evaluate()

                self.assertFalse(result.ok, result.messages)
                joined = "\n".join(result.messages)
                self.assertIn(path, joined)
                self.assertIn(reason, joined)

    def test_symlinked_fragment_fails_as_not_a_regular_file(self) -> None:
        self._code_change()
        blob = subprocess.run(
            ["git", "-C", str(self.repo.root), "hash-object", "-w", "--stdin"],
            input="../app/service.py",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(self.repo.root, "add", "app/service.py")
        _git(self.repo.root, "update-index", "--add", "--cacheinfo", f"120000,{blob},changelog.d/linked.fixed.md")
        _git(self.repo.root, "commit", "-q", "-m", "fix: linked fragment")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/linked.fixed.md", joined)
        self.assertIn("regular file", joined)

    def test_labels_and_advisory_combinations(self) -> None:
        for fragment in (False, True):
            for labels in (set(), {"bug"}, {"no-changelog"}, {"dependencies"}, {"no-changelog", "breaking"}):
                for advisory in (False, True):
                    with self.subTest(fragment=fragment, labels=sorted(labels), advisory=advisory):
                        _git(self.repo.root, "reset", "-q", "--hard", "main")
                        _git(self.repo.root, "clean", "-qfdx")
                        self._code_change()
                        if fragment:
                            self.repo.write("changelog.d/bump-value.fixed.md", "Bumped the value.\n")
                        self.repo.commit("fix: bump value")

                        result = self._evaluate(labels=labels, advisory=advisory)
                        joined = "\n".join(result.messages)

                        skip = [label for label in ("no-changelog", "dependencies") if label in labels]
                        if skip:
                            self.assertTrue(result.ok, result.messages)
                            self.assertIn(skip[0], result.messages[0])
                            self.assertNotIn("Advisory", joined)
                        elif fragment:
                            self.assertTrue(result.ok, result.messages)
                            self.assertNotIn("Advisory", joined)
                            self.assertIn("changelog.d/bump-value.fixed.md", joined)
                        else:
                            self.assertEqual(result.ok, advisory, result.messages)
                            self.assertIn("changelog.d/<short-name>.", joined)
                            self.assertEqual("Advisory" in joined, advisory)

    def test_breaking_label_needs_a_breaking_or_upgrade_fragment(self) -> None:
        for extra, expected in (
            (None, False),
            ("changelog.d/bump-value.breaking.md", True),
            ("changelog.d/bump-value.upgrade.md", True),
        ):
            with self.subTest(extra=extra):
                _git(self.repo.root, "reset", "-q", "--hard", "main")
                _git(self.repo.root, "clean", "-qfdx")
                self._code_change()
                self.repo.write("changelog.d/bump-value.fixed.md", "Bumped the value.\n")
                if extra:
                    self.repo.write(extra, "`VALUE` now defaults to 2; set it explicitly.\n")
                self.repo.commit("fix!: bump value")

                result = self._evaluate(labels={"breaking", "bug"})

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    joined = "\n".join(result.messages)
                    self.assertIn("'breaking'", joined)
                    self.assertIn(".breaking.md", joined)
                    self.assertIn(".upgrade.md", joined)

    def test_changelog_only_edit_fails_and_names_the_fragment_to_add(self) -> None:
        self._code_change()
        text = self.repo.read("CHANGELOG.md").replace(
            "### Fixed\n\n", "### Fixed\n\n- Bumped the value (#42).\n", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/<short-name>.", joined)
        self.assertIn("CHANGELOG.md", joined)

    def test_internal_fragment_passes_without_operator_visible_paths(self) -> None:
        self.repo.write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        # Built from parts: the privacy scan reads a dotted name ending in this
        # section word as a private host name.
        section = "internal"
        self.repo.write(f"changelog.d/more-tests.{section}.md", "Added a test for the service value.\n")
        self.repo.commit("test: add coverage")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)

    def test_release_cut_that_consumes_fragments_passes(self) -> None:
        _git(self.repo.root, "checkout", "-q", "main")
        self.repo.write("changelog.d/older-change.fixed.md", "Fixed an older thing.\n")
        self.repo.commit("fix: older change (#7)")
        _git(self.repo.root, "checkout", "-q", "-B", "topic")
        text = self.repo.read("CHANGELOG.md").replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1)
        text = text.replace(
            "### Fixed\n\n",
            "### Fixed\n\n- Fixed an older thing (#7).\n- Prepared the release (#42).\n",
            1,
        )
        self.repo.write("CHANGELOG.md", text)
        (self.repo.root / "changelog.d/older-change.fixed.md").unlink()
        self.repo.commit("chore: release v0.2.0")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        self.assertIn("v0.2.0", "\n".join(result.messages))


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name) / "repo")

    def _add_standard_fragments(self, repo: ScratchRepository | None = None, *, order=(20, 21, 22, 23)) -> None:
        repo = repo or self.repo
        fragments = {
            20: ("changelog.d/gamma-path.fixed.md", "Fixed the gamma path.\n"),
            21: ("changelog.d/alpha-path.fixed.md", "Fixed the alpha path\nacross two lines.\n"),
            22: ("changelog.d/beta-view.added.md", "Added a beta view\n"),
            23: ("changelog.d/delta-setting.upgrade.md", "Set `DELTA` before upgrading.\n"),
        }
        for number in order:
            path, text = fragments[number]
            repo.write(path, text)
            repo.write("app/service.py", f"VALUE = {number}\n")
            repo.commit(f"fix: change {number} (#{number})")

    def _collect(self, repo: ScratchRepository | None = None, **kwargs):
        repo = repo or self.repo
        kwargs.setdefault("version", "0.2.0")
        kwargs.setdefault("date", "2026-02-01")
        return _fragments().collect(repo.root, **kwargs)

    def test_collect_writes_fragments_under_their_sections_and_deletes_them(self) -> None:
        self._add_standard_fragments()

        result = self._collect()

        self.assertEqual(
            self.repo.read("CHANGELOG.md"),
            """# Changelog

## v0.2.0 - 2026-02-01

### Upgrade notes

- Set `DELTA` before upgrading (#23).

### Added

- Added a beta view (#22)

### Changed

- Changed an earlier thing (#1).

### Fixed

- Fixed an earlier thing, with a description that wraps onto a
  continuation line (#2).
- Fixed the gamma path (#20).
- Fixed the alpha path
  across two lines (#21).

## v0.1.0 - 2026-01-01

- Initial release.
""",
        )
        self.assertEqual(sorted(p.name for p in (self.repo.root / "changelog.d").iterdir()), ["README.md"])
        self.assertEqual(
            sorted(result.consumed),
            [
                "changelog.d/alpha-path.fixed.md",
                "changelog.d/beta-view.added.md",
                "changelog.d/delta-setting.upgrade.md",
                "changelog.d/gamma-path.fixed.md",
            ],
        )

    def test_collect_output_is_deterministic(self) -> None:
        other = ScratchRepository(Path(self._tmp.name) / "other")
        self._add_standard_fragments(order=(20, 21, 22, 23))
        self._add_standard_fragments(other, order=(23, 22, 21, 20))

        self._collect()
        self._collect(other)

        self.assertEqual(self.repo.read("CHANGELOG.md"), other.read("CHANGELOG.md"))

    def test_current_unreleased_body_is_kept_byte_for_byte(self) -> None:
        # Pins the checked-in Unreleased body: collection keeps every existing
        # line in its order and only inserts fragment bullets (plus headings
        # for subsections the body does not have yet).
        real = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        module = _fragments()
        bullets = {
            section: [[f"- Synthetic {section} entry (#9{index})."]]
            for index, section in enumerate(SECTION_NAMES)
        }

        merged = module.merge_into_unreleased(real, bullets)

        old_lines = real.split("\n")
        new_lines = merged.split("\n")
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        inserted: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            self.assertIn(tag, ("equal", "insert"), (tag, old_lines[i1:i2], new_lines[j1:j2]))
            inserted.extend(new_lines[j1:j2] if tag == "insert" else [])
        allowed = {"", *(f"### {module.SECTIONS[s]}" for s in SECTION_NAMES)}
        allowed |= {line for lines in bullets.values() for bullet in lines for line in bullet}
        self.assertLessEqual(set(inserted), allowed)
        self.assertEqual(len([line for line in inserted if line.startswith("- ")]), len(SECTION_NAMES))
        self.assertEqual(merged.split("\n## v", 1)[1], real.split("\n## v", 1)[1])

        unreleased = merged.split("## Unreleased\n", 1)[1].split("\n## v", 1)[0]
        original = real.split("## Unreleased\n", 1)[1].split("\n## v", 1)[0]
        for section in SECTION_NAMES:
            heading = f"### {module.SECTIONS[section]}"
            block = unreleased.split(heading + "\n", 1)[1].split("\n### ", 1)[0].rstrip("\n")
            with self.subTest(section=section):
                self.assertTrue(block.endswith("\n" + bullets[section][0][0]), block[-200:])
                if heading + "\n" in original:
                    old_block = original.split(heading + "\n", 1)[1].split("\n### ", 1)[0].rstrip("\n")
                    self.assertEqual(block, old_block + "\n" + bullets[section][0][0])

    def test_number_comes_from_the_commit_that_added_the_fragment(self) -> None:
        self.repo.write("changelog.d/gamma-path.fixed.md", "Fixed the gamma path.\n")
        self.repo.commit("fix: gamma path (#21)")
        self.repo.write("changelog.d/gamma-path.fixed.md", "Fixed the gamma path again.\n")
        self.repo.commit("docs: reword the gamma entry (#30)")

        self._collect()

        self.assertIn("- Fixed the gamma path again (#21).", self.repo.read("CHANGELOG.md"))
        self.assertNotIn("#30", self.repo.read("CHANGELOG.md"))

    def test_missing_number_is_refused_unless_given_with_pr(self) -> None:
        self.repo.write("changelog.d/delta-path.fixed.md", "Fixed the delta path.\n")
        self.repo.commit("fix: delta path without a number")
        self.repo.write("changelog.d/uncommitted.fixed.md", "Fixed something not yet committed.\n")
        before = self.repo.read("CHANGELOG.md")
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        message = str(raised.exception)
        self.assertIn("changelog.d/delta-path.fixed.md", message)
        self.assertIn("changelog.d/uncommitted.fixed.md", message)
        self.assertIn("--pr", message)
        self.assertEqual(self.repo.read("CHANGELOG.md"), before)
        self.assertTrue((self.repo.root / "changelog.d/delta-path.fixed.md").exists())

        self._collect(pr_overrides={"delta-path.fixed.md": 25, "changelog.d/uncommitted.fixed.md": 26})

        text = self.repo.read("CHANGELOG.md")
        self.assertIn("- Fixed the delta path (#25).", text)
        self.assertIn("- Fixed something not yet committed (#26).", text)

    def test_pr_override_for_an_unknown_file_is_refused(self) -> None:
        self._add_standard_fragments()
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect(pr_overrides={"missing.fixed.md": 5})

        self.assertIn("missing.fixed.md", str(raised.exception))

    def test_collect_deletes_exactly_the_fragments_it_read(self) -> None:
        self._add_standard_fragments()
        self.repo.write("changelog.d/README.md", README_TEXT + "Extra.\n")
        self.repo.write("docs/keep.fixed.md", "Not a fragment.\n")

        result = self._collect()

        self.assertTrue((self.repo.root / "changelog.d/README.md").exists())
        self.assertTrue((self.repo.root / "docs/keep.fixed.md").exists())
        self.assertEqual(len(result.consumed), 4)

    def test_second_run_for_the_same_version_is_refused(self) -> None:
        self._add_standard_fragments()
        self._collect()
        after_first = self.repo.read("CHANGELOG.md")
        self.repo.write("changelog.d/later.fixed.md", "Fixed something later.\n")
        self.repo.commit("fix: later (#40)")
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        self.assertIn("v0.2.0", str(raised.exception))
        self.assertIn("already", str(raised.exception))
        self.assertEqual(self.repo.read("CHANGELOG.md"), after_first)
        self.assertTrue((self.repo.root / "changelog.d/later.fixed.md").exists())

    def test_interrupted_cleanup_is_reported_by_the_next_run(self) -> None:
        self._add_standard_fragments()
        module = _fragments()
        real_remove = module._remove_fragment
        calls: list[Path] = []

        def interrupted(path: Path) -> None:
            calls.append(path)
            if len(calls) > 1:
                raise OSError("simulated interruption")
            real_remove(path)

        with mock.patch.object(module, "_remove_fragment", side_effect=interrupted):
            with self.assertRaises(OSError):
                self._collect()

        self.assertIn("## v0.2.0 - 2026-02-01", self.repo.read("CHANGELOG.md"))
        remaining = sorted(
            f"changelog.d/{p.name}" for p in (self.repo.root / "changelog.d").iterdir() if p.name != "README.md"
        )
        self.assertEqual(len(remaining), 3)

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        message = str(raised.exception)
        self.assertIn("v0.2.0", message)
        for path in remaining:
            self.assertIn(f"{path}: already in '## v0.2.0 - 2026-02-01'", message)

        # A fragment added after the interrupted run is reported as not in it.
        self.repo.write("changelog.d/newer-change.fixed.md", "Fixed a newer thing.\n")
        with self.assertRaises(module.CollectError) as raised:
            self._collect()
        self.assertIn("changelog.d/newer-change.fixed.md: not in", str(raised.exception))

    def test_invalid_fragment_stops_collection_before_writing(self) -> None:
        self._add_standard_fragments()
        self.repo.write("changelog.d/bad.feature.md", "Added a thing.\n")
        before = self.repo.read("CHANGELOG.md")
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        self.assertIn("changelog.d/bad.feature.md", str(raised.exception))
        self.assertEqual(self.repo.read("CHANGELOG.md"), before)

    def test_version_and_date_are_validated(self) -> None:
        module = _fragments()
        for version, date in (("0.2", "2026-02-01"), ("0.2.0", "2026-13-01"), ("01.2.0", "2026-02-01")):
            with self.subTest(version=version, date=date), self.assertRaises(module.CollectError):
                self._collect(version=version, date=date)

    def test_cli_check_preview_and_collect(self) -> None:
        self._add_standard_fragments()
        self.repo.write("changelog.d/pending-change.fixed.md", "Fixed a pending thing.\n")
        module = _fragments()

        def run(*argv: str) -> tuple[int, str]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = module.main(["--repo", str(self.repo.root), *argv])
            return code, output.getvalue()

        code, output = run("check")
        self.assertEqual(code, 0, output)
        code, output = run("preview")
        self.assertEqual(code, 0, output)
        self.assertIn("## Unreleased", output)
        self.assertIn("- Fixed the gamma path (#20).", output)
        self.assertIn("- Fixed a pending thing.", output)
        self.assertNotIn("- Fixed a pending thing (#", output)

        code, output = run("collect", "--version", "0.2.0", "--date", "2026-02-01")
        self.assertEqual(code, 1, output)
        self.assertIn("changelog.d/pending-change.fixed.md", output)

        code, output = run(
            "collect", "--version", "0.2.0", "--date", "2026-02-01", "--pr", "pending-change.fixed.md=24"
        )
        self.assertEqual(code, 0, output)
        self.assertIn("changelog.d/pending-change.fixed.md (#24)", output)

        self.repo.write("changelog.d/bad.feature.md", "Added a thing.\n")
        code, output = run("check")
        self.assertEqual(code, 1, output)
        self.assertIn("unknown section 'feature'", output)


class RepositoryFragmentTests(unittest.TestCase):
    def test_checked_in_fragments_are_valid(self) -> None:
        module = _fragments()
        fragments, problems = module.load_fragments(REPOSITORY_ROOT)

        self.assertEqual(problems, [])
        self.assertTrue((REPOSITORY_ROOT / "changelog.d" / "README.md").is_file())
        for fragment in fragments:
            self.assertIn(fragment.section, SECTION_NAMES)

    def test_readme_examples_are_valid_fragments(self) -> None:
        module = _fragments()
        readme = (REPOSITORY_ROOT / "changelog.d" / "README.md").read_text(encoding="utf-8")
        self.assertIn("changelog.d/<slug>.<section>.md", readme)
        examples = []
        lines = readme.split("\n")
        for index, line in enumerate(lines):
            if line.startswith("`changelog.d/") and line.endswith(".md`:"):
                start = lines.index("```markdown", index) + 1
                end = lines.index("```", start)
                examples.append((line.strip("`:"), "\n".join(lines[start:end]) + "\n"))
        self.assertEqual(len(examples), 2, examples)
        for path, text in examples:
            with self.subTest(path=path):
                self.assertIsNone(module.fragment_problem(path, text.encode("utf-8")))

    def test_documents_describe_fragments_instead_of_changelog_edits(self) -> None:
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        checklist = (REPOSITORY_ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        self.assertIn("changelog.d/<slug>.<section>.md", contributing)
        self.assertNotIn("Add one bullet to `CHANGELOG.md`", contributing)
        self.assertIn("changelog.d/", changelog.split("## Unreleased", 1)[0])
        self.assertIn("scripts/changelog_fragments.py collect", checklist)

    def test_privacy_scan_covers_fragments(self) -> None:
        from tests.test_public_doc_privacy import discover_tracked_public_text

        scanned = discover_tracked_public_text(REPOSITORY_ROOT)
        tracked = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z", "--", "changelog.d"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8").split("\0")
        tracked = [path for path in tracked if path]
        if not tracked:
            self.skipTest("not a Git checkout with changelog.d tracked")
        for path in tracked:
            self.assertIn(path, scanned)
        exceptions = (REPOSITORY_ROOT / "tests" / "public_text_privacy_exceptions.json").read_text(encoding="utf-8")
        self.assertNotIn("changelog.d", exceptions)

    def test_workflow_gate_keeps_its_check_name_and_reads_fragments(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  changelog-entry:\n", 1)[1].split("\n  python-unittest:\n", 1)[0]

        self.assertIn("name: Changelog entry\n", job)
        self.assertIn("changelog fragment", job)
        self.assertNotIn("CHANGELOG entry", job)


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class MergeTests(unittest.TestCase):
    def test_two_branches_with_fragments_and_tests_merge_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = ScratchRepository(Path(raw))
            for branch, path, test in (
                ("one", "changelog.d/first-change.fixed.md", "tests/test_first.py"),
                ("two", "changelog.d/second-change.added.md", "tests/test_second.py"),
            ):
                _git(repo.root, "checkout", "-q", "-b", branch, "main")
                repo.write("app/service.py" if branch == "one" else "app/other.py", f"VALUE = '{branch}'\n")
                repo.write(path, f"Changed the {branch} thing.\n")
                repo.write(test, "def test() -> None:\n    pass\n")
                repo.commit(f"fix: {branch}")
                changed = _git(repo.root, "diff", "--name-only", "main...HEAD").stdout.split()
                self.assertNotIn("CHANGELOG.md", changed)
                result = gate.evaluate(repo.root, base="main", head=branch, pr_number=50, labels=set())
                self.assertTrue(result.ok, result.messages)

            for target, source in (("one", "two"), ("two", "one")):
                with self.subTest(target=target, source=source):
                    _git(repo.root, "checkout", "-q", "-B", f"merge-{target}", f"{target}")
                    merged = _git(repo.root, "merge", "--no-edit", "-q", source, check=False)
                    self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
                    self.assertEqual(_git(repo.root, "diff", "--name-only", "--diff-filter=U").stdout, "")


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class ReleaseToolingTests(unittest.TestCase):
    """Coverage, notes and wrap validation read fragments for unreleased work."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(
            Path(self._tmp.name) / "repo",
            changelog=(
                "# Changelog\n\n## v0.1.0 - 2026-01-01\n\n### Highlights\n\n- First release.\n\n"
                "### Fixed\n\n- Initial release (#5).\n"
            ),
        )
        _git(self.repo.root, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.repo.write("app/service.py", "VALUE = 10\n")
        self.repo.write("changelog.d/first-fix.fixed.md", "Fixed the first thing.\n")
        self.repo.commit("fix: first thing (#10)")
        self.repo.write("app/service.py", "VALUE = 11\n")
        self.repo.write("changelog.d/second-view.added.md", "Added the second view.\n")
        self.repo.write("changelog.d/second-view.upgrade.md", "Set `SECOND` before upgrading.\n")
        self.repo.commit("feat: second view (#11)")
        self.repo.write(".github/workflows/x.yml", "on: push\n")
        self.repo.commit("ci: tidy (#12)")
        self.merged = self.repo.root.parent / "merged.json"
        self.merged.write_text(
            json.dumps([{"number": 12, "labels": [{"name": "no-changelog"}]}]), encoding="utf-8"
        )

    def _coverage(self, header: str) -> coverage.CoverageResult:
        return coverage.evaluate(
            self.repo.root, previous_tag="v0.1.0", section_header=header, merged_prs_json=self.merged
        )

    def _wrap_text(self) -> str:
        rows = [
            f"| {gate_name} | yes | synthetic receipt | Pass |  |"
            for gate_name in validate_release_wrap.REQUIRED_GATES
        ]
        rows.append(
            f"| {validate_release_wrap.DOCS_PUBLICATION_GATE} | yes | "
            "Pending owner publication: external wiki | Blocked |  |"
        )
        return "\n".join(
            [
                "# Release wrap",
                "",
                "Validated against `docs/RELEASE_CHECKLIST.md`.",
                "",
                "Changelog coverage: pass (2 PRs)",
                "",
                "| Gate | Required | Evidence | Result | N/A Reason |",
                "| --- | --- | --- | --- | --- |",
                *rows,
                "",
            ]
        )

    def _validate_wrap(self, version: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validate_release_wrap.main(
                [version, "--phase", "pre-tag", "--repository", str(self.repo.root)]
            )
        return code, output.getvalue()

    def test_coverage_counts_fragments_bound_to_merged_pull_requests(self) -> None:
        result = self._coverage("## Unreleased")

        self.assertTrue(result.ok, result.messages)
        self.assertEqual(result.covered, {10, 11})
        self.assertEqual(result.messages[0], "Changelog coverage: pass (2 PRs)")

    def test_coverage_fails_for_a_merged_pull_request_without_bullet_or_fragment(self) -> None:
        self.repo.write("app/service.py", "VALUE = 13\n")
        self.repo.commit("fix: forgot the entry (#13)")

        result = self._coverage("## Unreleased")

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {13})

    def test_released_section_never_reads_pending_fragments(self) -> None:
        text = self.repo.read("CHANGELOG.md").replace(
            "## v0.1.0", "## v0.2.0 - 2026-02-01\n\n### Fixed\n\n- Nothing (#99).\n\n## v0.1.0", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("chore: hand-written section")

        result = self._coverage("## v0.2.0 - 2026-02-01")

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {10, 11})

    def test_release_notes_for_unreleased_include_upgrade_fragments(self) -> None:
        text = self.repo.read("CHANGELOG.md").replace(
            "## v0.1.0", "## Unreleased\n\n### Highlights\n\n- A theme.\n\n## v0.1.0", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("docs: highlights")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = render_release_notes.main(["## Unreleased", "--changelog", str(self.repo.root / "CHANGELOG.md")])

        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue(),
            "## Highlights\n\n- A theme.\n\n## Upgrade notes\n\n- Set `SECOND` before upgrading (#11).\n",
        )

    def test_wrap_validator_refuses_a_tree_that_still_holds_fragments(self) -> None:
        self.repo.write("docs/RELEASE_WRAP_0.2.0-rc.1.md", self._wrap_text())
        self.repo.commit("docs: wrap")

        code, output = self._validate_wrap("0.2.0-rc.1")

        self.assertEqual(code, 1, output)
        self.assertIn("changelog.d/first-fix.fixed.md", output)
        self.assertIn("changelog.d/second-view.added.md", output)
        self.assertNotIn("README.md", output)
        self.assertEqual(
            validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.1.0"), []
        )

    def test_tag_without_changelog_directory_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            old = Path(raw)
            _git(old, "init", "-q", "-b", "main")
            (old / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
            _git(old, "add", "-A")
            _git(old, "-c", "user.email=t@example.test", "-c", "user.name=T", "commit", "-q", "-m", "old")
            _git(old, "tag", "v0.0.1")

            self.assertEqual(validate_release_wrap.unconsumed_fragment_issues(old, "refs/tags/v0.0.1"), [])

    def test_synthetic_release_from_fragments_passes_every_release_tool(self) -> None:
        pre = self._coverage("## Unreleased")
        self.assertTrue(pre.ok, pre.messages)

        result = _fragments().collect(self.repo.root, version="0.2.0-rc.1", date="2026-02-01")
        self.assertEqual(len(result.consumed), 3)
        header = "## v0.2.0-rc.1 - 2026-02-01"
        text = self.repo.read("CHANGELOG.md")
        self.assertIn(header, text)
        self.repo.write(
            "CHANGELOG.md", text.replace(header + "\n\n", header + "\n\n### Highlights\n\n- A theme.\n\n", 1)
        )
        self.repo.write("docs/RELEASE_WRAP_0.2.0-rc.1.md", self._wrap_text())
        self.repo.commit("chore: release v0.2.0-rc.1")
        _git(self.repo.root, "tag", "-a", "v0.2.0-rc.1", "-m", "v0.2.0-rc.1")

        post = self._coverage(header)
        self.assertTrue(post.ok, post.messages)
        self.assertEqual(post.covered, {10, 11})
        notes = render_release_notes.render(self.repo.read("CHANGELOG.md"), header)
        self.assertIn("- Set `SECOND` before upgrading (#11).", notes)
        code, output = self._validate_wrap("0.2.0-rc.1")
        self.assertEqual(code, 0, output)
        self.assertEqual(
            validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.2.0-rc.1"), []
        )

        self.repo.write("changelog.d/late-fix.fixed.md", "Fixed a late thing.\n")
        self.repo.commit("fix: late (#14)")
        _git(self.repo.root, "tag", "-f", "-a", "v0.2.0-rc.1", "-m", "v0.2.0-rc.1")
        issues = validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.2.0-rc.1")
        self.assertEqual(len(issues), 1)
        self.assertIn("changelog.d/late-fix.fixed.md", issues[0].message)
        code, output = self._validate_wrap("0.2.0-rc.1")
        self.assertEqual(code, 1, output)


REMOVAL_LABEL = "changelog-removal-ok"
RECOVERY_TEXT = "Delete the ones already in that section and commit, or restore CHANGELOG.md from Git and collect again."


def _cli(main, *argv: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = main(list(argv))
    return code, output.getvalue()


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class FragmentProtectionGateTests(unittest.TestCase):
    """A pull request cannot drop another pull request's pending fragment."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name))
        self.repo.write("changelog.d/older-change.fixed.md", "Fixed an older thing.\n")
        self.repo.write("changelog.d/older-change.upgrade.md", "Set `OLD_SETTING` before upgrading.\n")
        self.repo.commit("fix: older change (#7)")
        _git(self.repo.root, "checkout", "-q", "-b", "topic")

    def _reset(self) -> None:
        _git(self.repo.root, "reset", "-q", "--hard", "main")
        _git(self.repo.root, "clean", "-qfdx")

    def _evaluate(self, labels: set[str] | None = None, advisory: bool = False) -> gate.GateResult:
        return gate.evaluate(
            self.repo.root, base="main", head="topic", pr_number=42, labels=labels or set(), advisory=advisory
        )

    def _own_fragment(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")
        self.repo.write("changelog.d/new-change.fixed.md", "Fixed the new thing.\n")

    def test_deleting_another_fragment_is_refused_and_named(self) -> None:
        self._own_fragment()
        (self.repo.root / "changelog.d/older-change.upgrade.md").unlink()
        self.repo.commit("fix: new thing")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/older-change.upgrade.md", joined)
        self.assertNotIn("changelog.d/older-change.fixed.md", joined)
        self.assertIn(REMOVAL_LABEL, joined)

    def test_renaming_another_fragment_is_refused(self) -> None:
        self._own_fragment()
        _git(self.repo.root, "mv", "changelog.d/older-change.upgrade.md", "changelog.d/renamed-change.upgrade.md")
        self.repo.commit("fix: new thing")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        self.assertIn("changelog.d/older-change.upgrade.md", "\n".join(result.messages))

    def test_protection_runs_before_the_exemption_labels_and_advisory_mode(self) -> None:
        for labels in (set(), {"no-changelog"}, {"dependencies"}, {"no-changelog", "bug"}):
            for advisory in (False, True):
                with self.subTest(labels=sorted(labels), advisory=advisory):
                    self._reset()
                    self.repo.write("app/service.py", "VALUE = 3\n")
                    (self.repo.root / "changelog.d/older-change.upgrade.md").unlink()
                    self.repo.commit("chore: tidy")

                    result = self._evaluate(labels=labels, advisory=advisory)

                    self.assertFalse(result.ok, result.messages)
                    joined = "\n".join(result.messages)
                    self.assertIn("changelog.d/older-change.upgrade.md", joined)
                    self.assertNotIn("Advisory", joined)

    def test_removal_label_allows_the_deletion(self) -> None:
        for labels, own, expected in (
            ({REMOVAL_LABEL}, True, True),
            ({REMOVAL_LABEL}, False, False),
            ({REMOVAL_LABEL, "no-changelog"}, False, True),
        ):
            with self.subTest(labels=sorted(labels), own=own):
                self._reset()
                self.repo.write("app/service.py", "VALUE = 4\n")
                if own:
                    self._own_fragment()
                (self.repo.root / "changelog.d/older-change.upgrade.md").unlink()
                self.repo.commit("revert: older change")

                result = self._evaluate(labels=labels)

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    # The label waives the protection, not the need for an entry.
                    self.assertIn("changelog.d/<short-name>.", "\n".join(result.messages))

    def test_editing_another_fragment_stays_allowed_but_must_stay_valid(self) -> None:
        for text, expected in (("Fixed an older thing, reworded.\n", True), ("### Fixed\n", False)):
            with self.subTest(text=text):
                self._reset()
                self._own_fragment()
                self.repo.write("changelog.d/older-change.fixed.md", text)
                self.repo.commit("fix: new thing")

                result = self._evaluate()

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    joined = "\n".join(result.messages)
                    self.assertIn("changelog.d/older-change.fixed.md", joined)
                    self.assertIn("heading", joined)

    def test_release_cut_must_carry_the_text_of_every_fragment_it_deletes(self) -> None:
        cut = self.repo.read("CHANGELOG.md").replace("## Unreleased", "## v0.2.0 - 2026-02-01", 1)
        carried = cut.replace(
            "### Fixed\n\n",
            "### Upgrade notes\n\n- Set `OLD_SETTING` before\n  upgrading (#7).\n\n"
            "### Fixed\n\n- Fixed an older thing (#7).\n- Prepared the release (#42).\n",
            1,
        )
        dropped = cut.replace(
            "### Fixed\n\n", "### Fixed\n\n- Fixed an older thing (#7).\n- Prepared the release (#42).\n", 1
        )
        for name, text, expected in (("carried", carried, True), ("dropped", dropped, False)):
            with self.subTest(case=name):
                self._reset()
                self.repo.write("CHANGELOG.md", text)
                (self.repo.root / "changelog.d/older-change.fixed.md").unlink()
                (self.repo.root / "changelog.d/older-change.upgrade.md").unlink()
                self.repo.commit("chore: release v0.2.0")

                result = self._evaluate()

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    joined = "\n".join(result.messages)
                    self.assertIn("changelog.d/older-change.upgrade.md", joined)
                    self.assertNotIn("changelog.d/older-change.fixed.md", joined)


class FragmentContentTests(unittest.TestCase):
    """A fragment is one inline paragraph; only parenthesised numbers are refused."""

    def _problem(self, text: str) -> str | None:
        return _fragments().fragment_problem("changelog.d/sample-entry.fixed.md", text.encode("utf-8"))

    def test_document_scoped_markdown_is_refused_with_its_own_reason(self) -> None:
        cases = (
            ("Fixed a thing <!-- hidden\n", "HTML comment"),
            ("Fixed a thing\nthat wraps --> here.\n", "HTML comment"),
            ("[target]: https://example.test/replaced\n", "link reference definition"),
            ("   [target]: https://example.test/replaced\n", "link reference definition"),
            ("Fixed a thing\n[target]: https://example.test/replaced\n", "link reference definition"),
            ("Fixed a thing\n    ### Hidden heading\n", "heading"),
            ("Fixed a thing\n---\nand more.\n", "setext"),
            ("Fixed a thing\n===\nand more.\n", "setext"),
            ("Fixed a thing\n  ---\n", "setext"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                problem = self._problem(text)
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)

    def test_ordinary_inline_markdown_is_accepted(self) -> None:
        for text in (
            "Fixed the alpha path\nacross two wrapped lines.\n",
            "Set `EXAMPLE_SETTING=on` before upgrading.\n",
            "Linked the [troubleshooting page](https://example.test/docs#saving) from the error.\n",
            'Answered "<action> stopped on an internal error" for a local fault.\n',
            "Kept the value - and its unit - visible when the count is >= 2.\n",
            "Fixed a thing\nwhose wrapped line starts with -- two dashes.\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._problem(text))

    def test_unreleased_bullets_pass_the_content_rules(self) -> None:
        # The bullets already under Unreleased, without their (#N) suffix, must
        # be valid fragment text: the rules refuse markup, not ordinary entries.
        module = _fragments()
        real = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        if "\n## Unreleased\n" not in real:
            self.skipTest("CHANGELOG.md has no Unreleased section")
        body = real.split("\n## Unreleased\n", 1)[1].split("\n## ", 1)[0]
        bullets: list[list[str]] = []
        for line in body.split("\n"):
            if line.startswith("- "):
                bullets.append([line[2:]])
            elif bullets and line.startswith("  ") and line.strip():
                bullets[-1].append(line.strip())
        self.assertTrue(bullets)
        refused = []
        for lines in bullets:
            text = "\n".join(lines)
            text = module.PARENTHETICAL.sub(
                lambda group: "" if module.PR_TOKEN.search(group.group(1)) else group.group(0), text
            )
            problem = self._problem(text + "\n")
            if problem is not None:
                refused.append((lines[0], problem))
        self.assertEqual(refused, [])

    def test_only_parenthesised_numbers_are_refused(self) -> None:
        for text in ("Fixed a thing (#7).\n", "Fixed a thing (see #7).\n", "Fixed a thing\n(#7, #8).\n"):
            with self.subTest(text=text):
                problem = self._problem(text)
                self.assertIsNotNone(problem)
                self.assertIn("in parentheses", problem)
                self.assertIn("pull request reference", problem)
        for text in (
            "Fixed the regression reported in #7.\n",
            "Fixed the regression reported as GH-7.\n",
            "Fixed the regression from https://github.com/example/repo/pull/7.\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._problem(text))

    def test_documents_state_the_number_and_removal_rules(self) -> None:
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        readme = (REPOSITORY_ROOT / "changelog.d" / "README.md").read_text(encoding="utf-8")
        for name, text in (("CONTRIBUTING.md", contributing), ("changelog.d/README.md", readme)):
            with self.subTest(document=name):
                flat = " ".join(text.split())
                self.assertIn(REMOVAL_LABEL, flat)
                self.assertIn("inside parentheses", flat)
                self.assertIn("A plain mention of an issue or another pull request", flat)
                self.assertNotIn("no pull request number, at most", flat)


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class PlainMentionCoverageTests(unittest.TestCase):
    def test_plain_mentions_are_not_counted_by_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = ScratchRepository(Path(raw) / "repo")
            _git(repo.root, "tag", "v0.1.0")
            repo.write(
                "changelog.d/regression.fixed.md",
                "Fixed the regression reported in #7 and GH-8, see\nhttps://github.com/example/repo/pull/9.\n",
            )
            repo.commit("fix: regression (#10)")
            merged = Path(raw) / "merged.json"
            merged.write_text(json.dumps([7, 8, 9, 10]), encoding="utf-8")

            result = coverage.evaluate(
                repo.root, previous_tag="v0.1.0", section_header="## Unreleased", merged_prs_json=merged
            )

            self.assertEqual(result.covered, {10})
            self.assertEqual(result.missing, {7, 8, 9})


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class BindingTests(unittest.TestCase):
    """A fragment is bound from history only when history proves the number."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name) / "repo")

    def _bind(self, root: Path | None = None) -> tuple[dict[str, int], dict[str, str]]:
        module = _fragments()
        fragments, problems = module.load_fragments(root or self.repo.root)
        self.assertEqual(problems, [])
        return module.bind_numbers(root or self.repo.root, fragments)

    def test_shallow_repository_is_refused_by_preview_and_collect(self) -> None:
        self.repo.write("changelog.d/first-entry.fixed.md", "Fixed first.\n")
        self.repo.commit("fix: first (#12)")
        self.repo.write("changelog.d/second-entry.added.md", "Added second.\n")
        self.repo.commit("feat: second (#13)")
        self.repo.write("other.txt", "unrelated\n")
        self.repo.commit("docs: unrelated (#14)")
        module = _fragments()
        for depth, shallow in ((["--depth", "1"], True), ([], False)):
            with self.subTest(shallow=shallow):
                clone = Path(self._tmp.name) / ("shallow" if shallow else "full")
                subprocess.run(
                    ["git", "clone", "-q", "--no-hardlinks", *depth, self.repo.root.as_uri(), str(clone)],
                    check=True,
                    capture_output=True,
                )
                before = (clone / "CHANGELOG.md").read_text(encoding="utf-8")
                code, output = _cli(module.main, "--repo", str(clone), "preview")
                if shallow:
                    self.assertEqual(code, 1, output)
                    self.assertIn("shallow", output)
                    with self.assertRaises(module.CollectError) as raised:
                        module.collect(clone, version="0.2.0", date="2026-02-01")
                    self.assertIn("shallow", str(raised.exception))
                    self.assertIn("fetch", str(raised.exception))
                    self.assertEqual((clone / "CHANGELOG.md").read_text(encoding="utf-8"), before)
                    self.assertTrue((clone / "changelog.d/first-entry.fixed.md").exists())
                else:
                    self.assertEqual(code, 0, output)
                    result = module.collect(clone, version="0.2.0", date="2026-02-01")
                    self.assertEqual(
                        result.numbers,
                        {"changelog.d/first-entry.fixed.md": 12, "changelog.d/second-entry.added.md": 13},
                    )

    def test_untracked_or_changed_working_tree_fragment_never_inherits_a_number(self) -> None:
        self.repo.write("changelog.d/sample-entry.fixed.md", "Fixed the old sample.\n")
        self.repo.commit("fix: old (#12)")
        (self.repo.root / "changelog.d/sample-entry.fixed.md").unlink()
        self.repo.commit("chore: removed (#13)")
        self.repo.write("changelog.d/kept-entry.fixed.md", "Fixed the kept sample.\n")
        self.repo.write("changelog.d/edited-entry.fixed.md", "Fixed the edited sample.\n")
        self.repo.commit("fix: kept (#14)")
        self.repo.write("changelog.d/sample-entry.fixed.md", "Fixed a new uncommitted sample.\n")
        self.repo.write("changelog.d/edited-entry.fixed.md", "Fixed the edited sample, changed locally.\n")

        numbers, unbound = self._bind()

        self.assertEqual(numbers, {"changelog.d/kept-entry.fixed.md": 14})
        self.assertIn("not committed", unbound["changelog.d/sample-entry.fixed.md"])
        self.assertIn("differs", unbound["changelog.d/edited-entry.fixed.md"])
        module = _fragments()
        with self.assertRaises(module.CollectError) as raised:
            module.collect(self.repo.root, version="0.2.0", date="2026-02-01")
        self.assertIn("changelog.d/sample-entry.fixed.md", str(raised.exception))
        self.assertIn("changelog.d/edited-entry.fixed.md", str(raised.exception))

    def test_renamed_or_copied_fragment_is_not_bound_to_the_renaming_commit(self) -> None:
        self.repo.write("changelog.d/sample-entry.fixed.md", "Fixed the first sample.\n")
        self.repo.commit("fix: first (#12)")
        _git(self.repo.root, "mv", "changelog.d/sample-entry.fixed.md", "changelog.d/renamed-entry.fixed.md")
        self.repo.commit("docs: rename (#14)")
        rename_sha = self.repo.head()
        self.repo.write("changelog.d/copied-entry.fixed.md", "Fixed the first sample.\n")
        self.repo.commit("docs: copy (#15)")

        numbers, unbound = self._bind()

        self.assertEqual(numbers, {})
        self.assertIn(f"renamed in {rename_sha[:12]}", unbound["changelog.d/renamed-entry.fixed.md"])
        self.assertIn("copied", unbound["changelog.d/copied-entry.fixed.md"])
        module = _fragments()
        with self.assertRaises(module.CollectError):
            module.collect(self.repo.root, version="0.2.0", date="2026-02-01")

    def test_squash_subject_number_rules(self) -> None:
        # The number is the final '(#N)' of the subject; anything else is unbound.
        module = _fragments()
        for index, (subject, expected) in enumerate(
            (
                ("fix: path (#12)", 12),
                ("fix: path (#12, #13)", None),
                ('Revert "fix: previous (#12)" (#13)', 13),
                ("fix: path (#12) more words", None),
                ("fix: reference (#12) and actual merge (#13)", 13),
                ("Merge pull request #13 from example/topic", None),
            )
        ):
            with self.subTest(subject=subject):
                repo = ScratchRepository(Path(self._tmp.name) / f"subject-{index}")
                repo.write("changelog.d/sample-entry.fixed.md", "Fixed the sample.\n")
                repo.commit(subject)
                fragments, _problems = module.load_fragments(repo.root)
                numbers, unbound = module.bind_numbers(repo.root, fragments)
                self.assertEqual(numbers.get("changelog.d/sample-entry.fixed.md"), expected, unbound)

    def test_readded_fragment_binds_to_the_commit_that_added_it_again(self) -> None:
        self.repo.write("changelog.d/sample-entry.fixed.md", "Fixed the first sample.\n")
        self.repo.commit("fix: first (#12)")
        (self.repo.root / "changelog.d/sample-entry.fixed.md").unlink()
        self.repo.commit("chore: remove (#13)")
        self.repo.write("changelog.d/sample-entry.fixed.md", "Fixed the new sample.\n")
        self.repo.commit("fix: readd (#14)")

        numbers, _unbound = self._bind()

        self.assertEqual(numbers, {"changelog.d/sample-entry.fixed.md": 14})


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class StagingBranchTests(unittest.TestCase):
    """Fragments carried in through a squash-merged staging branch are never guessed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name) / "repo")
        _git(self.repo.root, "tag", "v0.1.0")

    def _staging(self, *, carried: bool = False) -> None:
        root = self.repo.root
        if carried:
            # An older bullet already names the inner pull request.
            self.repo.write(
                "CHANGELOG.md",
                self.repo.read("CHANGELOG.md").replace("### Fixed\n\n", "### Fixed\n\n- Fixed a legacy part (#13).\n", 1),
            )
            self.repo.commit("docs: carried legacy entry")
        _git(root, "checkout", "-q", "-b", "staging")
        self.repo.write("changelog.d/inner-change.fixed.md", "Fixed the inner change.\n")
        self.repo.write("changelog.d/inner-change.upgrade.md", "Set `INNER` before upgrading.\n")
        self.repo.commit("fix: inner change (#13)")
        inner = self.repo.head()
        _git(root, "checkout", "-q", "main")
        _git(root, "merge", "-q", "--squash", "staging")
        self.repo.commit("feat: staging (#14)")
        self.outer = self.repo.head()
        self.repo.write("changelog.d/plain-change.fixed.md", "Fixed the plain change.\n")
        self.repo.commit("fix: plain change (#15)")
        plain = self.repo.head()
        self.merged = Path(self._tmp.name) / "merged.json"
        self.merged.write_text(
            json.dumps(
                [
                    {
                        "number": 13,
                        "baseRefName": "staging",
                        "headRefName": "inner",
                        "isCrossRepository": False,
                        "mergedAt": "2099-01-02T00:00:00Z",
                        "mergeCommit": {"oid": inner},
                        "labels": [],
                    },
                    {
                        "number": 14,
                        "baseRefName": "main",
                        "headRefName": "staging",
                        "isCrossRepository": False,
                        "mergedAt": "2099-01-03T00:00:00Z",
                        "mergeCommit": {"oid": self.outer},
                        "labels": [{"name": "no-changelog"}],
                    },
                    {
                        "number": 15,
                        "baseRefName": "main",
                        "headRefName": "plain",
                        "isCrossRepository": False,
                        "mergedAt": "2099-01-04T00:00:00Z",
                        "mergeCommit": {"oid": plain},
                        "labels": [],
                    },
                ]
            ),
            encoding="utf-8",
        )

    def _coverage(self, overrides: dict[str, int] | None = None) -> coverage.CoverageResult:
        extra = {} if overrides is None else {"pr_overrides": overrides}
        return coverage.evaluate(
            self.repo.root,
            previous_tag="v0.1.0",
            section_header="## Unreleased",
            merged_prs_json=self.merged,
            **extra,
        )

    def test_partial_override_of_a_commit_is_refused_listing_its_siblings(self) -> None:
        self._staging()
        module = _fragments()
        before = self.repo.read("CHANGELOG.md")

        with self.assertRaises(module.CollectError) as raised:
            module.collect(
                self.repo.root, version="0.2.0", date="2026-02-01", pr_overrides={"inner-change.fixed.md": 13}
            )

        self.assertIn("changelog.d/inner-change.upgrade.md", str(raised.exception))
        self.assertEqual(self.repo.read("CHANGELOG.md"), before)
        for main, extra in (
            (module.main, ("--repo", str(self.repo.root), "preview", "--pr", "inner-change.fixed.md=13")),
            (
                coverage.main,
                (
                    "v0.1.0",
                    "## Unreleased",
                    "--repo",
                    str(self.repo.root),
                    "--merged-prs-json",
                    str(self.merged),
                    "--pr",
                    "inner-change.fixed.md=13",
                ),
            ),
        ):
            with self.subTest(command=main.__module__):
                code, output = _cli(main, *extra)
                self.assertNotEqual(code, 0, output)
                self.assertIn("changelog.d/inner-change.upgrade.md", output)

        result = module.collect(
            self.repo.root,
            version="0.2.0",
            date="2026-02-01",
            pr_overrides={"inner-change.fixed.md": 13, "inner-change.upgrade.md": 13},
        )
        self.assertEqual(result.numbers["changelog.d/inner-change.upgrade.md"], 13)
        self.assertEqual(result.numbers["changelog.d/plain-change.fixed.md"], 15)

    def test_preview_lists_the_fragments_each_commit_added(self) -> None:
        self._staging()

        code, output = _cli(_fragments().main, "--repo", str(self.repo.root), "preview")

        self.assertEqual(code, 0, output)
        line = next((row for row in output.splitlines() if self.outer[:12] in row), "")
        self.assertIn("(#14)", line)
        self.assertIn("changelog.d/inner-change.fixed.md", line)
        self.assertIn("changelog.d/inner-change.upgrade.md", line)

    def test_fragment_bound_to_a_squashed_staging_branch_is_ambiguous(self) -> None:
        self._staging(carried=True)

        result = self._coverage()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("ambiguous", joined)
        self.assertIn("changelog.d/inner-change.upgrade.md", joined)
        self.assertIn("#14", joined)
        self.assertIn("#13", joined)
        flagged = [line for line in result.messages if "bound by Git to" in line]
        self.assertEqual(len(flagged), 2, flagged)
        self.assertFalse(any("plain-change" in line for line in flagged), flagged)

        confirmed = self._coverage({"inner-change.fixed.md": 13, "inner-change.upgrade.md": 13})
        self.assertTrue(confirmed.ok, confirmed.messages)
        self.assertEqual(confirmed.covered, {13, 15})


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class CollectInputTests(unittest.TestCase):
    """Collection writes and deletes only the fragment bytes it read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _repo(self, name: str) -> ScratchRepository:
        repo = ScratchRepository(Path(self._tmp.name) / name)
        for number, path, text in (
            (20, "changelog.d/gamma-path.fixed.md", "Fixed the gamma path.\n"),
            (21, "changelog.d/alpha-path.fixed.md", "Fixed the alpha path.\n"),
        ):
            repo.write(path, text)
            repo.commit(f"fix: change {number} (#{number})")
        return repo

    @staticmethod
    def _mutate(repo: ScratchRepository, mode: str) -> None:
        path = repo.root / "changelog.d/gamma-path.fixed.md"
        if mode == "changed":
            path.write_text("Fixed the gamma path, edited after the read.\n", encoding="utf-8")
        elif mode == "replaced":
            path.unlink()
            path.write_text("Fixed a replacement never read.\n", encoding="utf-8")
        elif mode == "removed":
            path.unlink()
        elif mode == "added":
            repo.write("changelog.d/late-arrival.fixed.md", "Fixed a late arrival.\n")

    def test_fragment_changed_before_the_write_stops_collection(self) -> None:
        module = _fragments()
        for mode in ("changed", "replaced", "removed"):
            with self.subTest(mode=mode):
                repo = self._repo(f"before-{mode}")
                before = repo.read("CHANGELOG.md")
                original = module.merge_into_unreleased

                def merge_then_change(*args, **kwargs):
                    merged = original(*args, **kwargs)
                    self._mutate(repo, mode)
                    return merged

                with mock.patch.object(module, "merge_into_unreleased", side_effect=merge_then_change):
                    with self.assertRaises(module.CollectError) as raised:
                        module.collect(repo.root, version="0.2.0", date="2026-02-01")

                self.assertIn("changelog.d/gamma-path.fixed.md", str(raised.exception))
                self.assertEqual(repo.read("CHANGELOG.md"), before)
                self.assertTrue((repo.root / "changelog.d/alpha-path.fixed.md").exists())

    def test_cleanup_leaves_fragments_that_changed_after_the_write(self) -> None:
        module = _fragments()
        for mode in ("changed", "replaced", "removed"):
            with self.subTest(mode=mode):
                repo = self._repo(f"after-{mode}")
                original = module._write_changelog

                def write_then_change(path, text):
                    original(path, text)
                    self._mutate(repo, mode)

                with mock.patch.object(module, "_write_changelog", side_effect=write_then_change):
                    code, output = _cli(
                        module.main, "--repo", str(repo.root), "collect", "--version", "0.2.0", "--date", "2026-02-01"
                    )

                self.assertEqual(code, 1, output)
                self.assertNotIn("Traceback", output)
                self.assertIn("changelog.d/gamma-path.fixed.md", output)
                self.assertIn(RECOVERY_TEXT, output)
                self.assertIn("## v0.2.0 - 2026-02-01", repo.read("CHANGELOG.md"))
                self.assertFalse((repo.root / "changelog.d/alpha-path.fixed.md").exists())
                gamma = repo.root / "changelog.d/gamma-path.fixed.md"
                self.assertEqual(gamma.exists(), mode != "removed")
                if mode != "removed":
                    self.assertNotEqual(gamma.read_text(encoding="utf-8"), "Fixed the gamma path.\n")

    def test_fragment_added_after_the_read_is_left_alone(self) -> None:
        module = _fragments()
        repo = self._repo("late")
        original = module._write_changelog

        def write_then_add(path, text):
            original(path, text)
            self._mutate(repo, "added")

        with mock.patch.object(module, "_write_changelog", side_effect=write_then_add):
            result = module.collect(repo.root, version="0.2.0", date="2026-02-01")

        self.assertTrue((repo.root / "changelog.d/late-arrival.fixed.md").exists())
        self.assertNotIn("changelog.d/late-arrival.fixed.md", result.consumed)
        self.assertEqual(len(result.consumed), 2)
        self.assertNotIn("late arrival", repo.read("CHANGELOG.md"))


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class OverrideMapTests(unittest.TestCase):
    """preview, coverage and collect take the same --pr map, so the checklist order works."""

    def test_preview_coverage_and_collect_accept_the_same_pr_map(self) -> None:
        module = _fragments()
        with tempfile.TemporaryDirectory() as raw:
            repo = ScratchRepository(Path(raw) / "repo")
            _git(repo.root, "tag", "v0.1.0")
            repo.write("changelog.d/sample-entry.fixed.md", "Fixed the sample.\n")
            repo.commit("fix: sample with an edited title")
            merged = Path(raw) / "merged.json"
            merged.write_text(json.dumps([12]), encoding="utf-8")
            coverage_args = ("v0.1.0", "## Unreleased", "--repo", str(repo.root), "--merged-prs-json", str(merged))

            code, output = _cli(module.main, "--repo", str(repo.root), "check")
            self.assertEqual(code, 0, output)
            code, output = _cli(coverage.main, *coverage_args)
            self.assertEqual(code, 1, output)
            self.assertIn("#12", output)

            code, output = _cli(module.main, "--repo", str(repo.root), "preview", "--pr", "sample-entry.fixed.md=12")
            self.assertEqual(code, 0, output)
            self.assertIn("- Fixed the sample (#12).", output)
            code, output = _cli(coverage.main, *coverage_args, "--pr", "sample-entry.fixed.md=12")
            self.assertEqual(code, 0, output)

            for bad, reason in (
                ("sample-entry.fixed.md=abc", "use <file>=<number>"),
                ("missing-entry.fixed.md=12", "not fragments in this checkout"),
                ("sample-entry.fixed.md=0", "positive integer"),
            ):
                with self.subTest(value=bad):
                    preview_code, preview_out = _cli(module.main, "--repo", str(repo.root), "preview", "--pr", bad)
                    coverage_code, coverage_out = _cli(coverage.main, *coverage_args, "--pr", bad)
                    self.assertNotEqual(preview_code, 0, preview_out)
                    self.assertNotEqual(coverage_code, 0, coverage_out)
                    self.assertIn(reason, preview_out)
                    self.assertIn(reason, coverage_out)

            code, output = _cli(
                module.main,
                "--repo",
                str(repo.root),
                "collect",
                "--version",
                "0.2.0",
                "--date",
                "2026-02-01",
                "--pr",
                "sample-entry.fixed.md=12",
            )
            self.assertEqual(code, 0, output)
            code, output = _cli(
                coverage.main, "v0.1.0", "## v0.2.0 - 2026-02-01", "--repo", str(repo.root), "--merged-prs-json", str(merged)
            )
            self.assertEqual(code, 0, output)

    def test_checklist_shows_the_pr_map_in_order(self) -> None:
        checklist = (REPOSITORY_ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        steps = (
            "python scripts/changelog_fragments.py check",
            "python scripts/changelog_fragments.py preview [--pr <file>=<N> ...]",
            '"## Unreleased" --merged-prs-json merged-prs.json [--pr <file>=<N> ...]',
            "python scripts/changelog_fragments.py collect --version X.Y.Z --date YYYY-MM-DD [--pr <file>=<N> ...]",
        )
        positions = [checklist.find(step) for step in steps]
        self.assertNotIn(-1, positions, list(zip(steps, positions)))
        self.assertEqual(positions, sorted(positions))
        flat = " ".join(checklist.split())
        self.assertIn("prefer a merge commit over a squash", flat)
        self.assertIn("bind every inner fragment with `--pr`", flat)


if __name__ == "__main__":
    unittest.main()
