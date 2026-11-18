"""Changelog fragments: admission gate, collector and release tooling.

A pull request adds one file under ``changelog.d/``, named for its own pull
request number, instead of editing ``CHANGELOG.md``. These tests pin the gate
that admits it, the collector that folds fragments into ``CHANGELOG.md`` at
release time, and the release tools that read the combined view. Every Git
repository here is a scratch directory. Pull request numbers have at least
three digits so the fixtures also read as names on older revisions.
"""

from __future__ import annotations

import contextlib
import difflib
import importlib
import io
import json
import re
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
EDIT_LABEL = "changelog-edit-ok"
# Built from parts: the privacy scan reads a dotted name ending in this section
# word as a private host name.
INTERNAL_SECTION = "inter" + "nal"
OLD_REMOVAL_LABEL = "changelog-removal-ok"
RECOVERY_TEXT = (
    "Compare them with the section that was written, delete the ones already in it and commit, or restore "
    "CHANGELOG.md and changelog.d/ from Git together (git restore CHANGELOG.md changelog.d) and collect "
    "again; restoring CHANGELOG.md alone loses the fragments that were already deleted."
)
RELEASE_HEADING = "## v0.2.0 - 2026-02-01"

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


def _cli(main, *argv: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = main(list(argv))
    return code, output.getvalue()


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
    PR = 420

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
            pr_number=self.PR,
            labels=labels or set(),
            advisory=advisory,
        )

    def _reset(self) -> None:
        _git(self.repo.root, "reset", "-q", "--hard", "main")
        _git(self.repo.root, "clean", "-qfdx")

    def _code_change(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")

    def test_no_fragment_fails_naming_the_file_to_add(self) -> None:
        self._code_change()
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok)
        joined = "\n".join(result.messages)
        self.assertIn("add changelog.d/420.fixed.md", joined)
        for section in SECTION_NAMES:
            self.assertIn(section, joined)
        self.assertIn("no-changelog", joined)

    def test_added_valid_fragment_passes(self) -> None:
        self._code_change()
        self.repo.write("changelog.d/420.fixed.md", "Bumped the value an operator sees.\n")
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/420.fixed.md", joined)
        self.assertIn("Fixed", joined)

    def test_fragment_already_present_at_base_does_not_count(self) -> None:
        _git(self.repo.root, "checkout", "-q", "main")
        self.repo.write("changelog.d/700.fixed.md", "Fixed an older thing.\n")
        self.repo.commit("fix: older change (#700)")
        _git(self.repo.root, "checkout", "-q", "-B", "topic")

        for label, change in (
            ("untouched", lambda: None),
            ("edited", lambda: self.repo.write("changelog.d/700.fixed.md", "Fixed it better.\n")),
            ("renamed", lambda: _git(self.repo.root, "mv", "changelog.d/700.fixed.md", "changelog.d/700.fixed.2.md")),
        ):
            with self.subTest(change=label):
                self._reset()
                self._code_change()
                change()
                self.repo.commit(f"fix: {label}")

                result = self._evaluate()

                self.assertFalse(result.ok, result.messages)
                joined = "\n".join(result.messages)
                if label == "untouched":
                    self.assertIn("add changelog.d/420.fixed.md", joined)
                else:
                    # Changing another pull request's fragment is refused
                    # outright (see FragmentProtectionGateTests).
                    self.assertIn("changelog.d/700.fixed.md", joined)

    def test_each_invalid_fragment_fails_with_its_file_and_reason(self) -> None:
        cases = (
            ("changelog.d/420.Fixed.md", "Fixed a thing.\n", "unknown section 'Fixed'"),
            ("changelog.d/0420.fixed.md", "Fixed a thing.\n", "name"),
            ("changelog.d/420.fixed.txt", "Fixed a thing.\n", "name"),
            ("changelog.d/420.md", "Fixed a thing.\n", "name"),
            ("changelog.d/420.fixed.1.md", "Fixed a thing.\n", "name"),
            ("changelog.d/420.fixed.02.md", "Fixed a thing.\n", "name"),
            ("changelog.d/420.fixed.2.3.md", "Fixed a thing.\n", "name"),
            ("changelog.d/420.feature.md", "Added a thing.\n", "unknown section 'feature'"),
            ("changelog.d/420.highlights.md", "Shipped a theme.\n", "unknown section 'highlights'"),
            ("changelog.d/nested/420.fixed.md", "Fixed a thing.\n", "directly in changelog.d/"),
            ("changelog.d/420.fixed.md", "", "empty"),
            ("changelog.d/420.fixed.md", "\n   \n", "empty"),
            ("changelog.d/420.fixed.md", "Fixed " + "x" * 4100 + ".\n", "too long"),
            ("changelog.d/420.fixed.md", "### Fixed\n\nFixed a thing.\n", "heading"),
            ("changelog.d/420.fixed.md", "Fixed a thing (#42).\n", "pull request number"),
            ("changelog.d/420.fixed.md", "Fixed a thing (#12, #13).\n", "pull request number"),
            ("changelog.d/420.fixed.md", "- Fixed a thing.\n", "'- '"),
            ("changelog.d/420.fixed.md", "Fixed a thing.\n- Fixed another.\n", "one bullet"),
            ("changelog.d/420.fixed.md", "Fixed a thing.\n\nAnd more.\n", "blank line"),
            ("changelog.d/420.fixed.md", b"Fixed \xff thing.\n", "UTF-8"),
            ("changelog.d/420.fixed.md", "Fixed the `<details>` view.\n", "in words"),
        )
        for path, content, reason in cases:
            with self.subTest(path=path, content=content[:30]):
                self._reset()
                self._code_change()
                self.repo.write("changelog.d/420.changed.md", "Changed the visible thing.\n")
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
        _git(self.repo.root, "update-index", "--add", "--cacheinfo", f"120000,{blob},changelog.d/420.fixed.md")
        _git(self.repo.root, "commit", "-q", "-m", "fix: linked fragment")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/420.fixed.md", joined)
        self.assertIn("regular file", joined)

    def test_labels_and_advisory_combinations(self) -> None:
        for fragment in (False, True):
            for labels in (set(), {"bug"}, {"no-changelog"}, {"dependencies"}, {"no-changelog", "breaking"}):
                for advisory in (False, True):
                    with self.subTest(fragment=fragment, labels=sorted(labels), advisory=advisory):
                        self._reset()
                        self._code_change()
                        if fragment:
                            self.repo.write("changelog.d/420.fixed.md", "Bumped the value.\n")
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
                            self.assertIn("changelog.d/420.fixed.md", joined)
                        else:
                            self.assertEqual(result.ok, advisory, result.messages)
                            self.assertIn("add changelog.d/420.fixed.md", joined)
                            self.assertEqual("Advisory" in joined, advisory)

    def test_breaking_label_needs_a_breaking_or_upgrade_fragment(self) -> None:
        for extra, expected in (
            (None, False),
            ("changelog.d/420.breaking.md", True),
            ("changelog.d/420.upgrade.md", True),
        ):
            with self.subTest(extra=extra):
                self._reset()
                self._code_change()
                self.repo.write("changelog.d/420.fixed.md", "Bumped the value.\n")
                if extra:
                    self.repo.write(extra, "`VALUE` now defaults to 2; set it explicitly.\n")
                self.repo.commit("fix!: bump value")

                result = self._evaluate(labels={"breaking", "bug"})

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    joined = "\n".join(result.messages)
                    self.assertIn("'breaking'", joined)
                    self.assertIn("changelog.d/420.breaking.md", joined)
                    self.assertIn("changelog.d/420.upgrade.md", joined)

    def test_changelog_only_edit_fails_and_names_the_fragment_to_add(self) -> None:
        self._code_change()
        text = self.repo.read("CHANGELOG.md").replace(
            "### Fixed\n\n", "### Fixed\n\n- Bumped the value (#420).\n", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("fix: bump value")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("add changelog.d/420.fixed.md", joined)
        self.assertIn("CHANGELOG.md", joined)

    def test_internal_fragment_passes_without_operator_visible_paths(self) -> None:
        self.repo.write("tests/test_service.py", "def test_more() -> None:\n    pass\n")
        self.repo.write(f"changelog.d/420.{INTERNAL_SECTION}.md", "Added a test for the service value.\n")
        self.repo.commit("test: add coverage")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)

@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class NumberGateTests(unittest.TestCase):
    """Every fragment a pull request adds is named for that pull request."""

    PR = 420

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name))
        self.repo.write("changelog.d/700.fixed.md", "Fixed an older thing.\n")
        self.repo.commit("fix: older change (#700)")
        _git(self.repo.root, "checkout", "-q", "-b", "topic")

    def _reset(self) -> None:
        _git(self.repo.root, "reset", "-q", "--hard", "main")
        _git(self.repo.root, "clean", "-qfdx")

    def _evaluate(self, labels: set[str] | None = None, advisory: bool = False) -> gate.GateResult:
        return gate.evaluate(
            self.repo.root, base="main", head="topic", pr_number=self.PR, labels=labels or set(), advisory=advisory
        )

    def test_added_fragment_must_carry_this_pull_request_number(self) -> None:
        cases = (
            ("other number", {"changelog.d/410.fixed.md": "Fixed the value.\n"}, "changelog.d/410.fixed.md", "changelog.d/420.fixed.md"),
            (
                "other number beside own",
                {"changelog.d/420.fixed.md": "Fixed the value.\n", "changelog.d/410.upgrade.md": "Set `VALUE`.\n"},
                "changelog.d/410.upgrade.md",
                "changelog.d/420.upgrade.md",
            ),
            ("no number", {"changelog.d/topic-name.fixed.md": "Fixed the value.\n"}, "changelog.d/topic-name.fixed.md", "changelog.d/420.fixed.md"),
        )
        for name, files, wrong, right in cases:
            for labels in (set(), {"no-changelog"}, {"dependencies"}, {EDIT_LABEL}):
                for advisory in (False, True):
                    with self.subTest(case=name, labels=sorted(labels), advisory=advisory):
                        self._reset()
                        self.repo.write("app/service.py", "VALUE = 2\n")
                        for path, text in files.items():
                            self.repo.write(path, text)
                        self.repo.commit("fix: value")

                        result = self._evaluate(labels=labels, advisory=advisory)

                        self.assertFalse(result.ok, result.messages)
                        joined = "\n".join(result.messages)
                        self.assertIn(wrong, joined)
                        self.assertIn(right, joined)
                        self.assertNotIn("Advisory", joined)

    def test_copied_fragment_must_carry_this_pull_request_number(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")
        self.repo.write("changelog.d/420.fixed.md", "Fixed the value.\n")
        shutil.copyfile(self.repo.root / "changelog.d/700.fixed.md", self.repo.root / "changelog.d/700.fixed.2.md")
        self.repo.commit("fix: value")

        for labels in (set(), {"no-changelog"}):
            with self.subTest(labels=sorted(labels)):
                result = self._evaluate(labels=labels)

                self.assertFalse(result.ok, result.messages)
                joined = "\n".join(result.messages)
                self.assertIn("changelog.d/700.fixed.2.md", joined)
                self.assertIn("changelog.d/420.fixed.md", joined)

    def test_section_and_counter_names_are_accepted(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")
        self.repo.write("changelog.d/420.fixed.md", "Fixed the value.\n")
        self.repo.write("changelog.d/420.fixed.2.md", "Fixed the unit beside it.\n")
        self.repo.write("changelog.d/420.upgrade.md", "Set `VALUE` before upgrading.\n")
        self.repo.commit("fix: value")

        result = self._evaluate()

        self.assertTrue(result.ok, result.messages)
        joined = "\n".join(result.messages)
        for path in ("changelog.d/420.fixed.md", "changelog.d/420.fixed.2.md", "changelog.d/420.upgrade.md"):
            self.assertIn(path, joined)


STANDARD_FRAGMENTS = {
    200: ("changelog.d/200.fixed.md", "Fixed the gamma path.\n"),
    201: ("changelog.d/201.fixed.md", "Fixed the alpha path\nacross two lines.\n"),
    202: ("changelog.d/202.added.md", "Added a beta view\n"),
    203: ("changelog.d/203.upgrade.md", "Set `DELTA` before upgrading.\n"),
}


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name) / "repo")

    def _add_standard_fragments(self, repo: ScratchRepository | None = None, *, order=(200, 201, 202, 203)) -> None:
        repo = repo or self.repo
        for number in order:
            path, text = STANDARD_FRAGMENTS[number]
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

- Set `DELTA` before upgrading (#203).

### Added

- Added a beta view (#202)

### Changed

- Changed an earlier thing (#1).

### Fixed

- Fixed an earlier thing, with a description that wraps onto a
  continuation line (#2).
- Fixed the gamma path (#200).
- Fixed the alpha path
  across two lines (#201).

## v0.1.0 - 2026-01-01

- Initial release.
""",
        )
        self.assertEqual(sorted(p.name for p in (self.repo.root / "changelog.d").iterdir()), ["README.md"])
        self.assertEqual(
            sorted(result.consumed),
            [
                "changelog.d/200.fixed.md",
                "changelog.d/201.fixed.md",
                "changelog.d/202.added.md",
                "changelog.d/203.upgrade.md",
            ],
        )

    def test_collect_output_is_deterministic(self) -> None:
        other = ScratchRepository(Path(self._tmp.name) / "other")
        self._add_standard_fragments(order=(200, 201, 202, 203))
        self._add_standard_fragments(other, order=(203, 202, 201, 200))

        self._collect()
        self._collect(other)

        self.assertEqual(self.repo.read("CHANGELOG.md"), other.read("CHANGELOG.md"))

    def test_current_unreleased_body_text_is_kept(self) -> None:
        # Pins the checked-in Unreleased body: collection keeps the text of
        # every existing line in its order and only inserts fragment bullets
        # (plus headings for subsections the body does not have yet). Line
        # endings are written as LF, which .gitattributes requires.
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

    def test_number_comes_from_the_file_name(self) -> None:
        # Commit subjects are never read: an unrelated number in the subject,
        # a later rewording commit and an uncommitted fragment change nothing.
        self.repo.write("changelog.d/210.fixed.md", "Fixed the gamma path.\n")
        self.repo.commit("fix: gamma path (#999)")
        self.repo.write("changelog.d/210.fixed.md", "Fixed the gamma path again.\n")
        self.repo.commit("docs: reword the gamma entry (#300)")
        self.repo.write("changelog.d/211.fixed.md", "Fixed something not yet committed.\n")

        result = self._collect()

        text = self.repo.read("CHANGELOG.md")
        self.assertIn("- Fixed the gamma path again (#210).", text)
        self.assertIn("- Fixed something not yet committed (#211).", text)
        self.assertNotIn("#999", text)
        self.assertNotIn("#300", text)
        self.assertEqual(result.numbers, {"changelog.d/210.fixed.md": 210, "changelog.d/211.fixed.md": 211})

    def test_collector_needs_no_git(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        (plain / "changelog.d").mkdir(parents=True)
        (plain / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
        (plain / "changelog.d/README.md").write_text(README_TEXT, encoding="utf-8")
        (plain / "changelog.d/230.fixed.md").write_text("Fixed the plain path.\n", encoding="utf-8")
        module = _fragments()

        code, output = _cli(module.main, "--repo", str(plain), "preview")
        self.assertEqual(code, 0, output)
        self.assertIn("- Fixed the plain path (#230).", output)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("git was called")):
            result = module.collect(plain, version="0.2.0", date="2026-02-01")

        self.assertEqual(result.consumed, ["changelog.d/230.fixed.md"])
        self.assertIn("- Fixed the plain path (#230).", (plain / "CHANGELOG.md").read_text(encoding="utf-8"))

    def test_several_entries_in_one_section_are_ordered_by_counter(self) -> None:
        for name, text in (
            ("240.fixed.10.md", "Fixed the tenth part.\n"),
            ("240.fixed.2.md", "Fixed the second part.\n"),
            ("240.fixed.md", "Fixed the first part.\n"),
            ("239.fixed.md", "Fixed an earlier part.\n"),
        ):
            self.repo.write(f"changelog.d/{name}", text)

        self._collect()

        fixed = self.repo.read("CHANGELOG.md").split("### Fixed\n\n", 1)[1].split("\n\n## ", 1)[0]
        self.assertEqual(
            fixed.split("\n")[-4:],
            [
                "- Fixed an earlier part (#239).",
                "- Fixed the first part (#240).",
                "- Fixed the second part (#240).",
                "- Fixed the tenth part (#240).",
            ],
        )

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
        self.repo.write("changelog.d/400.fixed.md", "Fixed something later.\n")
        self.repo.commit("fix: later (#400)")
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        self.assertIn("v0.2.0", str(raised.exception))
        self.assertIn("already", str(raised.exception))
        self.assertEqual(self.repo.read("CHANGELOG.md"), after_first)
        self.assertTrue((self.repo.root / "changelog.d/400.fixed.md").exists())

    def test_interrupted_cleanup_is_reported_by_the_next_run(self) -> None:
        self._add_standard_fragments()
        module = _fragments()
        real_remove = module._remove_fragment
        calls: list[Path] = []

        def interrupted(path: Path) -> None:
            calls.append(path)
            if len(calls) > 1:
                raise KeyboardInterrupt
            real_remove(path)

        with mock.patch.object(module, "_remove_fragment", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self._collect()

        self.assertIn(RELEASE_HEADING, self.repo.read("CHANGELOG.md"))
        remaining = sorted(
            f"changelog.d/{p.name}" for p in (self.repo.root / "changelog.d").iterdir() if p.name != "README.md"
        )
        self.assertEqual(len(remaining), 3)

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        # Every file still present is listed; nothing guesses which ones the
        # written section already holds.
        self.repo.write("changelog.d/500.fixed.md", "Fixed a newer thing.\n")
        with self.assertRaises(module.CollectError) as raised:
            self._collect()
        message = str(raised.exception)
        self.assertIn("v0.2.0", message)
        listed = [line.strip() for line in message.splitlines() if line.startswith("  changelog.d/")]
        self.assertEqual(listed, [*remaining, "changelog.d/500.fixed.md"])
        self.assertIn(RECOVERY_TEXT, message)

    def test_invalid_fragment_stops_collection_before_writing(self) -> None:
        self._add_standard_fragments()
        self.repo.write("changelog.d/300.feature.md", "Added a thing.\n")
        before = self.repo.read("CHANGELOG.md")
        module = _fragments()

        with self.assertRaises(module.CollectError) as raised:
            self._collect()

        self.assertIn("changelog.d/300.feature.md", str(raised.exception))
        self.assertEqual(self.repo.read("CHANGELOG.md"), before)

    def test_changelog_directory_must_be_a_real_directory(self) -> None:
        module = _fragments()
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "250.fixed.md").write_text("Fixed a thing outside.\n", encoding="utf-8")
        before = self.repo.read("CHANGELOG.md")
        directory = self.repo.root / "changelog.d"
        for kind in ("symlink", "file"):
            with self.subTest(kind=kind):
                if directory.is_symlink():
                    directory.unlink()
                else:
                    shutil.rmtree(directory)
                if kind == "symlink":
                    directory.symlink_to(outside, target_is_directory=True)
                else:
                    directory.write_text("not a directory\n", encoding="utf-8")

                fragments, problems = module.load_fragments(self.repo.root)
                self.assertEqual(fragments, [])
                self.assertEqual(len(problems), 1)
                self.assertIn("not a directory", problems[0])
                for argv in (("check",), ("preview",), ("collect", "--version", "0.2.0", "--date", "2026-02-01")):
                    code, output = _cli(module.main, "--repo", str(self.repo.root), *argv)
                    self.assertEqual(code, 1, (argv, output))
                    self.assertIn("not a directory", output)
                self.assertEqual(self.repo.read("CHANGELOG.md"), before)
                self.assertTrue((outside / "250.fixed.md").exists())

    def test_version_and_date_are_validated(self) -> None:
        module = _fragments()
        for version, date in (("0.2", "2026-02-01"), ("0.2.0", "2026-13-01"), ("01.2.0", "2026-02-01")):
            with self.subTest(version=version, date=date), self.assertRaises(module.CollectError):
                self._collect(version=version, date=date)

    def test_cli_check_preview_and_collect(self) -> None:
        self._add_standard_fragments()
        self.repo.write("changelog.d/204.fixed.md", "Fixed a pending thing.\n")
        module = _fragments()

        def run(*argv: str) -> tuple[int, str]:
            return _cli(module.main, "--repo", str(self.repo.root), *argv)

        code, output = run("check")
        self.assertEqual(code, 0, output)
        code, output = run("preview")
        self.assertEqual(code, 0, output)
        self.assertIn("## Unreleased", output)
        self.assertIn("- Fixed the gamma path (#200).", output)
        self.assertIn("- Fixed a pending thing (#204).", output)

        code, output = run("collect", "--version", "0.2.0", "--date", "2026-02-01")
        self.assertEqual(code, 0, output)
        self.assertIn("changelog.d/204.fixed.md (#204)", output)
        self.assertIn("5 fragment(s) consumed", output)

        self.repo.write("changelog.d/300.feature.md", "Added a thing.\n")
        code, output = run("check")
        self.assertEqual(code, 1, output)
        self.assertIn("unknown section 'feature'", output)


class FragmentNameTests(unittest.TestCase):
    """The file name carries the pull request number, the section and a counter."""

    def test_valid_and_invalid_names(self) -> None:
        module = _fragments()
        for name in ("1.fixed.md", "420.fixed.md", "420.fixed.2.md", "420.fixed.10.md", "1322.upgrade.md"):
            with self.subTest(name=name):
                self.assertIsNone(module.name_problem(f"changelog.d/{name}"))
        for name, reason in (
            ("0.fixed.md", "name"),
            ("0420.fixed.md", "name"),
            ("420.fixed.1.md", "name"),
            ("420.fixed.0.md", "name"),
            ("420.fixed.02.md", "name"),
            ("420.fixed.x.md", "name"),
            ("topic-name.fixed.md", "name"),
            ("420.fixed.md.md", "name"),
            ("420..md", "section"),
            ("-420.fixed.md", "name"),
            ("420.Fixed.md", "unknown section 'Fixed'"),
            ("420.feature.md", "unknown section 'feature'"),
        ):
            with self.subTest(name=name):
                problem = module.name_problem(f"changelog.d/{name}")
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)
        self.assertIn("directly in changelog.d/", module.name_problem("changelog.d/nested/420.fixed.md"))


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
        self.assertIn("changelog.d/<N>.<section>.md", readme)
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

        self.assertIn("changelog.d/<N>.<section>.md", contributing)
        self.assertNotIn("Add one bullet to `CHANGELOG.md`", contributing)
        self.assertIn("changelog.d/<N>.<section>.md", changelog.split("## Unreleased", 1)[0])
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
        self.assertIn(EDIT_LABEL, job)
        self.assertNotIn(OLD_REMOVAL_LABEL, job)

    def test_documents_describe_the_number_in_the_name_and_no_history_binding(self) -> None:
        documents = {
            "CONTRIBUTING.md": REPOSITORY_ROOT / "CONTRIBUTING.md",
            "changelog.d/README.md": REPOSITORY_ROOT / "changelog.d" / "README.md",
            "scripts/README.md": REPOSITORY_ROOT / "scripts" / "README.md",
            "docs/RELEASE_CHECKLIST.md": REPOSITORY_ROOT / "docs" / "RELEASE_CHECKLIST.md",
            "CHANGELOG.md introduction": REPOSITORY_ROOT / "CHANGELOG.md",
            "ci.yml": REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml",
        }
        for name, path in documents.items():
            text = path.read_text(encoding="utf-8")
            if name == "CHANGELOG.md introduction":
                text = text.split("## Unreleased", 1)[0]
            flat = " ".join(text.split())
            with self.subTest(document=name):
                self.assertNotIn(OLD_REMOVAL_LABEL, flat)
                self.assertNotIn("--pr <file>=<N>", flat)
                self.assertNotIn("<slug>", flat)
                self.assertNotIn("unshallow", flat)
                self.assertNotIn("squash subject ends", flat)
                self.assertNotIn("commit that added it", flat)
                self.assertNotIn("without a pull request number", flat)
        for name in ("CONTRIBUTING.md", "changelog.d/README.md"):
            flat = " ".join(documents[name].read_text(encoding="utf-8").split())
            with self.subTest(document=name, rule="order"):
                self.assertIn("gh pr view --json number --jq .number", flat)
                self.assertIn(EDIT_LABEL, flat)
        checklist = " ".join(documents["docs/RELEASE_CHECKLIST.md"].read_text(encoding="utf-8").split())
        self.assertIn("python scripts/changelog_fragments.py collect --version X.Y.Z --date YYYY-MM-DD", checklist)
        self.assertNotIn("prefer a merge commit over a squash", checklist)

    def test_documents_say_what_advisory_mode_softens(self) -> None:
        sentence = "Advisory mode softens only a missing or invalid entry"
        for name, text in (
            ("CONTRIBUTING.md", (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")),
            ("changelog.d/README.md", (REPOSITORY_ROOT / "changelog.d" / "README.md").read_text(encoding="utf-8")),
            ("gate docstring", gate.__doc__ or ""),
            ("gate evaluate docstring", gate.evaluate.__doc__ or ""),
        ):
            with self.subTest(document=name):
                self.assertIn(sentence, " ".join(text.split()))
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  changelog-entry:\n", 1)[1].split("\n  python-unittest:\n", 1)[0]
        flat_job = " ".join(line.strip().lstrip("#").strip() for line in job.splitlines())
        self.assertIn("advisory mode softens only a missing or invalid entry", flat_job.lower())


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class MergeTests(unittest.TestCase):
    def test_two_branches_with_fragments_and_tests_merge_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = ScratchRepository(Path(raw))
            for branch, number, path, test in (
                ("one", 501, "changelog.d/501.fixed.md", "tests/test_first.py"),
                ("two", 502, "changelog.d/502.fixed.md", "tests/test_second.py"),
            ):
                _git(repo.root, "checkout", "-q", "-b", branch, "main")
                repo.write("app/service.py" if branch == "one" else "app/other.py", f"VALUE = '{branch}'\n")
                repo.write(path, f"Changed the {branch} thing.\n")
                repo.write(test, "def test() -> None:\n    pass\n")
                repo.commit(f"fix: {branch}")
                changed = _git(repo.root, "diff", "--name-only", "main...HEAD").stdout.split()
                self.assertNotIn("CHANGELOG.md", changed)
                result = gate.evaluate(repo.root, base="main", head=branch, pr_number=number, labels=set())
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
        self.repo.write("changelog.d/100.fixed.md", "Fixed the first thing.\n")
        self.repo.commit("fix: first thing (#100)")
        self.repo.write("app/service.py", "VALUE = 11\n")
        self.repo.write("changelog.d/110.added.md", "Added the second view.\n")
        self.repo.write("changelog.d/110.upgrade.md", "Set `SECOND` before upgrading.\n")
        self.repo.commit("feat: second view (#110)")
        self.repo.write(".github/workflows/x.yml", "on: push\n")
        self.repo.commit("ci: tidy (#120)")
        self.merged = self.repo.root.parent / "merged.json"
        self.merged.write_text(
            json.dumps([{"number": 120, "labels": [{"name": "no-changelog"}]}]), encoding="utf-8"
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
        self.assertEqual(result.covered, {100, 110})
        self.assertEqual(result.messages[0], "Changelog coverage: pass (2 PRs)")

    def test_coverage_fails_for_a_merged_pull_request_without_bullet_or_fragment(self) -> None:
        self.repo.write("app/service.py", "VALUE = 13\n")
        self.repo.commit("fix: forgot the entry (#130)")

        result = self._coverage("## Unreleased")

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {130})

    def test_fragment_for_a_pull_request_that_did_not_merge_fails_coverage(self) -> None:
        # Only this release's pull requests count: one that never merged, or
        # that was labelled out in an earlier release, fails, committed or not;
        # one labelled out in this release passes.
        released = _git(self.repo.root, "rev-list", "-n", "1", "v0.1.0").stdout.strip()
        records = [{"number": 120, "labels": ["no-changelog"], "mergeCommit": {"oid": self.repo.head()}}]
        records.append({"number": 980, "labels": ["no-changelog"], "mergeCommit": {"oid": released}})
        self.merged.write_text(json.dumps(records), encoding="utf-8")
        self.repo.write(f"changelog.d/120.{INTERNAL_SECTION}.md", "Tidied the workflow.\n")
        self.repo.commit("chore: labelled fragment")
        base = self.repo.head()
        self.assertTrue(self._coverage("## Unreleased").ok)
        for number, committed in ((990, True), (980, True), (980, False)):
            with self.subTest(number=number, committed=committed):
                _git(self.repo.root, "reset", "-q", "--hard", base)
                self.repo.write(f"changelog.d/{number}.fixed.md", "Fixed a thing outside this release.\n")
                if committed:
                    self.repo.commit("chore: stray fragment")

                result = self._coverage("## Unreleased")

                self.assertFalse(result.ok, result.messages)
                self.assertIn(f"changelog.d/{number}.fixed.md (#{number})", "\n".join(result.messages))
                self.assertEqual(result.covered, {100, 110})
                self.assertEqual(result.missing, set())
                (self.repo.root / f"changelog.d/{number}.fixed.md").unlink(missing_ok=True)

    def test_squashed_staging_branch_needs_no_changelog_on_its_inner_pull_requests(self) -> None:
        _git(self.repo.root, "checkout", "-q", "-b", "staging")
        self.repo.write("app/service.py", "VALUE = 31\n")
        self.repo.commit("fix: inner work (#310)")
        inner = self.repo.head()
        _git(self.repo.root, "checkout", "-q", "main")
        _git(self.repo.root, "merge", "-q", "--squash", "staging")
        self.repo.write("changelog.d/320.fixed.md", "Fixed the staged work.\n")
        self.repo.commit("fix: staged work (#320)")
        records = [
            {"number": 120, "labels": ["no-changelog"]},
            {"number": 310, "labels": [], "mergedAt": "2099-01-02T00:00:00Z", "mergeCommit": {"oid": inner},
             "baseRefName": "staging", "headRefName": "inner", "isCrossRepository": False},
            {"number": 320, "labels": [], "mergedAt": "2099-01-03T00:00:00Z", "mergeCommit": {"oid": self.repo.head()},
             "baseRefName": "main", "headRefName": "staging", "isCrossRepository": False},
        ]
        for labelled in (False, True):
            with self.subTest(inner_labelled=labelled):
                records[1]["labels"] = ["no-changelog"] if labelled else []
                self.merged.write_text(json.dumps(records), encoding="utf-8")

                result = self._coverage("## Unreleased")

                self.assertEqual(result.ok, labelled, result.messages)
                self.assertEqual(result.missing, set() if labelled else {310})
                self.assertIn(320, result.covered)

    def test_released_section_never_reads_pending_fragments(self) -> None:
        text = self.repo.read("CHANGELOG.md").replace(
            "## v0.1.0", "## v0.2.0 - 2026-02-01\n\n### Fixed\n\n- Nothing (#99).\n\n## v0.1.0", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("chore: hand-written section")

        result = self._coverage(RELEASE_HEADING)

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {100, 110})

    def test_release_notes_for_unreleased_include_upgrade_fragments(self) -> None:
        text = self.repo.read("CHANGELOG.md").replace(
            "## v0.1.0", "## Unreleased\n\n### Highlights\n\n- A theme.\n\n## v0.1.0", 1
        )
        self.repo.write("CHANGELOG.md", text)
        self.repo.commit("docs: highlights")
        self.repo.write("changelog.d/150.upgrade.md", "### Set THIRD\n")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = render_release_notes.main(["## Unreleased", "--changelog", str(self.repo.root / "CHANGELOG.md")])

        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue(),
            "## Highlights\n\n- A theme.\n\n## Upgrade notes\n\n- Set `SECOND` before upgrading (#110).\n",
        )
        self.assertIn("changelog.d/150.upgrade.md: contains a heading line", errors.getvalue())

    def test_wrap_validator_refuses_a_tree_that_still_holds_fragments(self) -> None:
        self.repo.write("docs/RELEASE_WRAP_0.2.0-rc.1.md", self._wrap_text())
        self.repo.commit("docs: wrap")

        code, output = self._validate_wrap("0.2.0-rc.1")

        self.assertEqual(code, 1, output)
        self.assertIn("changelog.d/100.fixed.md", output)
        self.assertIn("changelog.d/110.added.md", output)
        self.assertNotIn("README.md", output)
        self.assertEqual(
            validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.1.0"), []
        )

    def test_wrap_validator_refuses_a_linked_changelog_directory(self) -> None:
        shutil.rmtree(self.repo.root / "changelog.d")
        (self.repo.root / "changelog.d").symlink_to("docs", target_is_directory=True)
        self.repo.commit("chore: link the fragment directory")

        issues = validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "HEAD")

        self.assertEqual(len(issues), 1)
        self.assertIn("changelog.d", issues[0].message)

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
        self.assertEqual(post.covered, {100, 110})
        notes = render_release_notes.render(self.repo.read("CHANGELOG.md"), header)
        self.assertIn("- Set `SECOND` before upgrading (#110).", notes)
        code, output = self._validate_wrap("0.2.0-rc.1")
        self.assertEqual(code, 0, output)
        self.assertEqual(
            validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.2.0-rc.1"), []
        )

        self.repo.write("changelog.d/140.fixed.md", "Fixed a late thing.\n")
        self.repo.commit("fix: late (#140)")
        _git(self.repo.root, "tag", "-f", "-a", "v0.2.0-rc.1", "-m", "v0.2.0-rc.1")
        issues = validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "refs/tags/v0.2.0-rc.1")
        self.assertEqual(len(issues), 1)
        self.assertIn("changelog.d/140.fixed.md", issues[0].message)
        code, output = self._validate_wrap("0.2.0-rc.1")
        self.assertEqual(code, 1, output)


    def _collect_with_one_failed_delete(self) -> str:
        """Collect while deleting changelog.d/100.fixed.md fails; the others are deleted."""

        module = _fragments()
        original_remove = module._remove_fragment

        def remove(path: Path) -> None:
            if path.name == "100.fixed.md":
                raise PermissionError(13, "Permission denied", str(path))
            original_remove(path)

        with mock.patch.object(module, "_remove_fragment", side_effect=remove):
            code, output = _cli(
                module.main, "--repo", str(self.repo.root), "collect", "--version", "0.2.0-rc.1", "--date", "2026-02-01"
            )
        self.assertEqual(code, 1, output)
        self.assertTrue((self.repo.root / "changelog.d/100.fixed.md").exists())
        self.assertFalse((self.repo.root / "changelog.d/110.added.md").exists())
        self.assertFalse((self.repo.root / "changelog.d/110.upgrade.md").exists())
        return output

    def test_failed_delete_recovery_by_comparing_and_deleting_completes_the_release(self) -> None:
        header = "## v0.2.0-rc.1 - 2026-02-01"
        output = self._collect_with_one_failed_delete()
        listed = [line.strip().split(":", 1)[0] for line in output.splitlines() if line.startswith("  changelog.d/")]
        self.assertEqual(listed, ["changelog.d/100.fixed.md"])

        # First route: each listed file whose text is already in the written section is deleted.
        section = _fragments().section_text(self.repo.read("CHANGELOG.md"), header)
        for path in listed:
            text = (self.repo.root / path).read_text(encoding="utf-8").strip().rstrip(".")
            self.assertIn(text, section)
            (self.repo.root / path).unlink()
        self.repo.commit("chore: release v0.2.0-rc.1")

        post = self._coverage(header)
        self.assertTrue(post.ok, post.messages)
        self.assertEqual(post.covered, {100, 110})
        self.assertEqual(validate_release_wrap.unconsumed_fragment_issues(self.repo.root, "HEAD"), [])

    def test_failed_delete_recovery_by_restoring_and_collecting_again_keeps_every_entry(self) -> None:
        header = "## v0.2.0-rc.1 - 2026-02-01"
        output = self._collect_with_one_failed_delete()

        # Restoring CHANGELOG.md alone loses the fragments that were deleted:
        # the second section holds only the entry left behind.
        _git(self.repo.root, "restore", "CHANGELOG.md")
        _fragments().collect(self.repo.root, version="0.2.0-rc.1", date="2026-02-01")
        partial = self._coverage(header)
        self.assertFalse(partial.ok, partial.messages)
        self.assertIn("110", "\n".join(partial.messages))

        # The guidance names every path to restore, so following it keeps every entry.
        command = re.search(r"\(git restore ([^)]+)\)", output)
        self.assertIsNotNone(command, f"the recovery text does not say to restore changelog.d/ as well:\n{output}")
        paths = command.group(1).split()
        self.assertIn("CHANGELOG.md", paths)
        self.assertIn("changelog.d", paths)
        _git(self.repo.root, "restore", *paths)
        result = _fragments().collect(self.repo.root, version="0.2.0-rc.1", date="2026-02-01")
        self.assertEqual(len(result.consumed), 3)
        full = self._coverage(header)
        self.assertTrue(full.ok, full.messages)
        self.assertEqual(full.covered, {100, 110})

        checklist = " ".join((REPOSITORY_ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8").split())
        self.assertIn(f"`git restore {command.group(1)}`", checklist)

@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class FragmentProtectionGateTests(unittest.TestCase):
    """A pull request cannot change another pull request's pending fragment."""

    PR = 420
    OLDER_UPGRADE = "Set `OLD_SETTING` before upgrading.\n"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = ScratchRepository(Path(self._tmp.name))
        self.repo.write("changelog.d/700.fixed.md", "Fixed an older thing.\n")
        self.repo.write("changelog.d/700.upgrade.md", self.OLDER_UPGRADE)
        self.repo.commit("fix: older change (#700)")
        _git(self.repo.root, "checkout", "-q", "-b", "topic")

    def _reset(self) -> None:
        _git(self.repo.root, "reset", "-q", "--hard", "main")
        _git(self.repo.root, "clean", "-qfdx")

    def _evaluate(self, labels: set[str] | None = None, advisory: bool = False) -> gate.GateResult:
        return gate.evaluate(
            self.repo.root, base="main", head="topic", pr_number=self.PR, labels=labels or set(), advisory=advisory
        )

    def _own_fragment(self) -> None:
        self.repo.write("app/service.py", "VALUE = 2\n")
        self.repo.write("changelog.d/420.fixed.md", "Fixed the new thing.\n")

    def _change(self, mode: str) -> None:
        """Apply one kind of change to changelog.d/700.upgrade.md and commit."""

        root = self.repo.root
        path = "changelog.d/700.upgrade.md"
        if mode == "delete":
            (root / path).unlink()
        elif mode == "rename":
            _git(root, "mv", path, "changelog.d/420.upgrade.md")
        elif mode == "content":
            self.repo.write(path, "Set `OLD_SETTING` before upgrading, reworded.\n")
        elif mode == "directory":
            (root / path).unlink()
            self.repo.write(path + "/child.md", self.OLDER_UPGRADE)
        _git(root, "add", "-A")
        if mode == "mode":
            _git(root, "update-index", "--chmod=+x", path)
        elif mode == "type":
            blob = subprocess.run(
                ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                input="700.fixed.md",
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _git(root, "update-index", "--cacheinfo", f"120000,{blob},{path}")
        _git(root, "commit", "-q", "-m", f"fix: {mode}")

    def test_deleting_another_fragment_is_refused_and_named(self) -> None:
        self._own_fragment()
        (self.repo.root / "changelog.d/700.upgrade.md").unlink()
        self.repo.commit("fix: new thing")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        joined = "\n".join(result.messages)
        self.assertIn("changelog.d/700.upgrade.md", joined)
        self.assertNotIn("changelog.d/700.fixed.md", joined)
        self.assertIn(EDIT_LABEL, joined)

    def test_renaming_another_fragment_is_refused(self) -> None:
        self._own_fragment()
        _git(self.repo.root, "mv", "changelog.d/700.upgrade.md", "changelog.d/420.upgrade.md")
        self.repo.commit("fix: new thing")

        result = self._evaluate()

        self.assertFalse(result.ok, result.messages)
        self.assertIn("changelog.d/700.upgrade.md", "\n".join(result.messages))

    def test_protection_runs_before_the_exemption_labels_and_advisory_mode(self) -> None:
        for labels in (set(), {"no-changelog"}, {"dependencies"}, {"no-changelog", "bug"}):
            for advisory in (False, True):
                with self.subTest(labels=sorted(labels), advisory=advisory):
                    self._reset()
                    self.repo.write("app/service.py", "VALUE = 3\n")
                    (self.repo.root / "changelog.d/700.upgrade.md").unlink()
                    self.repo.commit("chore: tidy")

                    result = self._evaluate(labels=labels, advisory=advisory)

                    self.assertFalse(result.ok, result.messages)
                    joined = "\n".join(result.messages)
                    self.assertIn("changelog.d/700.upgrade.md", joined)
                    self.assertNotIn("Advisory", joined)

    def test_changing_another_fragment_is_refused(self) -> None:
        for mode in ("delete", "rename", "content", "mode", "type", "directory"):
            for labels in (set(), {"no-changelog"}, {"dependencies"}, {OLD_REMOVAL_LABEL}):
                for advisory in (False, True):
                    with self.subTest(mode=mode, labels=sorted(labels), advisory=advisory):
                        self._reset()
                        self._own_fragment()
                        self._change(mode)

                        result = self._evaluate(labels=labels, advisory=advisory)

                        self.assertFalse(result.ok, result.messages)
                        joined = "\n".join(result.messages)
                        self.assertIn("changelog.d/700.upgrade.md", joined)
                        self.assertIn(EDIT_LABEL, joined)
                        self.assertNotIn("Advisory", joined)

    def test_edit_label_waives_only_the_protection(self) -> None:
        for mode, own, labels, expected in (
            ("delete", True, {EDIT_LABEL}, True),
            ("content", True, {EDIT_LABEL}, True),
            ("mode", True, {EDIT_LABEL}, True),
            ("delete", False, {EDIT_LABEL}, False),
            ("delete", False, {EDIT_LABEL, "no-changelog"}, True),
        ):
            with self.subTest(mode=mode, own=own, labels=sorted(labels)):
                self._reset()
                self.repo.write("app/service.py", "VALUE = 4\n")
                if own:
                    self._own_fragment()
                self._change(mode)

                result = self._evaluate(labels=labels)

                self.assertEqual(result.ok, expected, result.messages)
                if not expected:
                    # The label waives the protection, not the need for an entry.
                    self.assertIn("add changelog.d/420.fixed.md", "\n".join(result.messages))

    def test_release_cut_deleting_base_fragments_needs_the_edit_label(self) -> None:
        text = _git(self.repo.root, "show", "main:CHANGELOG.md").stdout.replace("## Unreleased", RELEASE_HEADING, 1)
        own = "- Fixed an older thing (#700).\n- Prepared the release (#420).\n"
        for labels, advisory, entry, expected in (
            (set(), False, True, False),
            ({"no-changelog"}, False, True, False),
            ({"dependencies"}, False, True, False),
            ({"bug", "breaking"}, False, True, False),
            (set(), True, True, False),
            ({EDIT_LABEL}, False, True, True),
            ({EDIT_LABEL}, False, False, False),
        ):
            with self.subTest(labels=sorted(labels), advisory=advisory, entry=entry):
                self._reset()
                self.repo.write("CHANGELOG.md", text.replace("### Fixed\n\n", "### Fixed\n\n" + own, 1) if entry else text)
                (self.repo.root / "changelog.d/700.fixed.md").unlink()
                (self.repo.root / "changelog.d/700.upgrade.md").unlink()
                self.repo.commit("chore: release v0.2.0")

                result = self._evaluate(labels=labels, advisory=advisory)

                self.assertEqual(result.ok, expected, result.messages)
                joined = "\n".join(result.messages)
                if expected:
                    self.assertIn("v0.2.0", joined)
                elif entry:
                    self.assertIn("changelog.d/700.fixed.md", joined)
                    self.assertIn("changelog.d/700.upgrade.md", joined)
                    self.assertIn(EDIT_LABEL, joined)
                    self.assertNotIn("Advisory", joined)
                else:
                    self.assertIn("add changelog.d/420.fixed.md", joined)

    def test_linked_changelog_directory_is_refused_before_labels(self) -> None:
        shutil.rmtree(self.repo.root / "changelog.d")
        (self.repo.root / "changelog.d").symlink_to("app", target_is_directory=True)
        self.repo.commit("chore: link the fragment directory")
        for labels, advisory in ((set(), False), ({"no-changelog", EDIT_LABEL}, False), (set(), True)):
            with self.subTest(labels=sorted(labels), advisory=advisory):
                result = self._evaluate(labels=labels, advisory=advisory)

                self.assertFalse(result.ok, result.messages)
                joined = "\n".join(result.messages)
                self.assertIn("  changelog.d: name it changelog.d/420.<section>.md", joined)
                self.assertNotIn("Advisory", joined)


class FragmentContentTests(unittest.TestCase):
    """A fragment is one inline paragraph; only parenthesised numbers are refused."""

    def _problem(self, text: str) -> str | None:
        return _fragments().text_lines(text.encode("utf-8"))[1]

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
            ("Fixed this.\n1. Extra item\n", "ordered list"),
            ("Fixed this.\n  2) Extra item\n", "ordered list"),
            ("1. Fixed this.\n", "ordered list"),
            ("Fixed this.\n> Extra quotation\n", "blockquote"),
            ("Fixed this.\n   >Extra quotation\n", "blockquote"),
            ("> Fixed this.\n", "blockquote"),
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
            'Answered "`{action}` stopped on an internal error" for a local fault.\n',
            "Kept the value - and its unit - visible when the count is >= 2.\n",
            "Fixed a thing\nwhose wrapped line starts with -- two dashes.\n",
            "Raised the limit to\n1,024 disks per expander.\n",
            "Fixed step 1. Then step 2) ran, and a > b held.\n",
            "Kept version\n0.25 visible.\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._problem(text))

    def test_raw_html_is_refused_even_in_code_spans(self) -> None:
        for text in (
            "Fixed a small thing.</li></ul><details><summary>More changes</summary>\n",
            "Fixed a small thing.\n</li></ul><details><summary>More changes</summary>\n",
            "<details><summary>Fixed a small thing.</summary></details>\n",
            "Fixed the <kbd>Enter</kbd> shortcut.\n",
            'Answered "<action> stopped on an internal error" for a local fault.\n',
            "Fixed the <div>layout.\n",
            "Fixed <!DOCTYPE handling.\n",
            "Fixed <?xml handling.\n",
            "Fixed `code` and then <b>bold</b> text.\n",
            "Fixed `an open code span and <i>this</i>.\n",
            "Fixed ``one `<b>` span`` and <b>this</b>.\n",
            "Set `<details>` in the template.\n",
            "Used ``a `<b>` c`` in the text.\n",
            "Kept `<kbd>Enter</kbd>`\nwrapped across lines.\n",
            "Fixed a thing. \\`</li></ul><details><summary>More changes</summary>\\`\n",
            "Fixed `a span that\n</li></ul><details>` breaks.\n",
        ):
            with self.subTest(text=text):
                problem = self._problem(text)
                self.assertIsNotNone(problem)
                self.assertIn("HTML", problem)
                self.assertIn("`NAME` or `{name}`", problem)
        for text in (
            "Kept a < b visible.\n",
            "Kept x <= 3 and y <2 visible.\n",
            "Set `NAME` or `{name}` in the template.\n",
            "Kept the arrow -> and <- visible.\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self._problem(text))

    def test_control_character_refusal_names_the_remedy(self) -> None:
        cases = (
            ("\t", "\\t", "replace the tab with spaces"),
            ("\u00ad", "\\xad", "remove the soft hyphen"),
            ("\u200d", "\\u200d", "remove the zero-width joiner"),
            ("\u200c", "\\u200c", "remove the zero-width non-joiner"),
            ("\u200e", "\\u200e", "remove the direction mark"),
            ("\u200f", "\\u200f", "remove the direction mark"),
        )
        for character, escaped, remedy in cases:
            with self.subTest(character=escaped):
                problem = self._problem(f"Fixed a{character}thing.\n")
                self.assertIsNotNone(problem)
                self.assertIn(f"control or formatting character ({escaped})", problem)
                self.assertIn(remedy, problem)
        # A character nobody types by accident keeps the general advice.
        problem = self._problem("Fixed a\x1bthing.\n")
        self.assertIsNotNone(problem)
        self.assertIn("control or formatting character (\\x1b)", problem)
        self.assertIn("write plain text on ordinary lines", problem)

    def test_unreleased_bullets_pass_the_content_rules_except_raw_html(self) -> None:
        # The bullets already under Unreleased, without their (#N) suffix, are
        # ordinary entries: only the raw-HTML rule, which is newer than some
        # of them, may refuse one. Existing bullets are not re-validated.
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
            if problem is not None and "raw HTML" not in problem:
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
                self.assertIn(EDIT_LABEL, flat)
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
                "changelog.d/110.fixed.md",
                "Fixed the regression reported in #7 and GH-8, see\nhttps://github.com/example/repo/pull/9.\n",
            )
            repo.commit("fix: regression (#110)")
            merged = Path(raw) / "merged.json"
            merged.write_text(json.dumps([7, 8, 9, 110]), encoding="utf-8")

            result = coverage.evaluate(
                repo.root, previous_tag="v0.1.0", section_header="## Unreleased", merged_prs_json=merged
            )

            self.assertEqual(result.covered, {110})
            self.assertEqual(result.missing, {7, 8, 9})


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class CollectInputTests(unittest.TestCase):
    """Collection writes and deletes only the fragment bytes it read."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _repo(self, name: str, numbers=(200, 201)) -> ScratchRepository:
        repo = ScratchRepository(Path(self._tmp.name) / name)
        for number in numbers:
            path, text = STANDARD_FRAGMENTS[number]
            repo.write(path, text)
            repo.commit(f"fix: change {number} (#{number})")
        return repo

    @staticmethod
    def _mutate(repo: ScratchRepository, mode: str) -> None:
        path = repo.root / "changelog.d/200.fixed.md"
        if mode == "changed":
            path.write_text("Fixed the gamma path, edited after the read.\n", encoding="utf-8")
        elif mode == "replaced":
            path.unlink()
            path.write_text("Fixed a replacement never read.\n", encoding="utf-8")
        elif mode == "removed":
            path.unlink()
        elif mode == "added":
            repo.write("changelog.d/600.fixed.md", "Fixed a late arrival.\n")

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

                self.assertIn("changelog.d/200.fixed.md", str(raised.exception))
                self.assertEqual(repo.read("CHANGELOG.md"), before)
                self.assertTrue((repo.root / "changelog.d/201.fixed.md").exists())

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
                self.assertIn("changelog.d/200.fixed.md", output)
                self.assertIn(RECOVERY_TEXT, output)
                self.assertIn(RELEASE_HEADING, repo.read("CHANGELOG.md"))
                self.assertFalse((repo.root / "changelog.d/201.fixed.md").exists())
                gamma = repo.root / "changelog.d/200.fixed.md"
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

        self.assertTrue((repo.root / "changelog.d/600.fixed.md").exists())
        self.assertNotIn("changelog.d/600.fixed.md", result.consumed)
        self.assertEqual(len(result.consumed), 2)
        self.assertNotIn("late arrival", repo.read("CHANGELOG.md"))

    def test_cleanup_failure_after_the_write_is_reported_and_collection_goes_on(self) -> None:
        module = _fragments()
        repo = self._repo("cleanup-failure", numbers=(200, 201, 202))
        written: list[bool] = []
        original_write = module._write_changelog
        original_read = module._read_regular
        original_remove = module._remove_fragment

        def write(path, text):
            original_write(path, text)
            written.append(True)

        def read(path):
            if written and path.name == "201.fixed.md":
                raise PermissionError(13, "Permission denied", str(path))
            return original_read(path)

        def remove(path):
            if path.name == "200.fixed.md":
                raise PermissionError(13, "Permission denied", str(path))
            original_remove(path)

        with mock.patch.object(module, "_write_changelog", side_effect=write), mock.patch.object(
            module, "_read_regular", side_effect=read
        ), mock.patch.object(module, "_remove_fragment", side_effect=remove):
            code, output = _cli(
                module.main, "--repo", str(repo.root), "collect", "--version", "0.2.0", "--date", "2026-02-01"
            )

        self.assertEqual(code, 1, output)
        self.assertNotIn("Traceback", output)
        self.assertIn(f"Wrote '{RELEASE_HEADING}'", output)
        self.assertIn(RECOVERY_TEXT, output)
        self.assertIn(RELEASE_HEADING, repo.read("CHANGELOG.md"))
        self.assertFalse((repo.root / "changelog.d/202.added.md").exists())
        listed = [line.strip() for line in output.splitlines() if line.strip().startswith("changelog.d/")]
        self.assertEqual(
            [line.split(":", 1)[0] for line in listed], ["changelog.d/200.fixed.md", "changelog.d/201.fixed.md"]
        )
        for line in listed:
            self.assertIn("still present", line)
            self.assertIn("PermissionError", line)
            self.assertIn(f"already in '{RELEASE_HEADING}'", line)
        for name in ("200.fixed.md", "201.fixed.md"):
            self.assertTrue((repo.root / "changelog.d" / name).exists())


if __name__ == "__main__":
    unittest.main()
