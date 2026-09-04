from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import check_release_changelog_coverage as coverage
from scripts import render_release_notes


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


CHANGELOG_TEMPLATE = """# Changelog

## Unreleased

### Highlights

- Two things shipped.

### Upgrade notes

- `SETTING` now defaults to off; set it explicitly (#12).

### Fixed

- Fixed the first thing (#10).
- Fixed the second thing, described at length so the reference wraps
  onto the next line (#11).
- Recovered restores (#12, #13, and #14).

## v0.1.0 - 2026-01-01

- Initial release (#5).
"""


@unittest.skipIf(shutil.which("git") is None, "git executable is required")
class ReleaseChangelogCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test@example.test")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        self._write("CHANGELOG.md", "# Changelog\n\n## v0.1.0 - 2026-01-01\n\n- Initial release (#5).\n")
        self._write("app/service.py", "VALUE = 1\n")
        self._commit("fix: initial release (#5)")
        _git(self.repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self._write("CHANGELOG.md", CHANGELOG_TEMPLATE)
        self._commit("fix: first thing (#10)")
        self._write("app/service.py", "VALUE = 2\n")
        self._commit("fix: second thing (#11)")
        self._write("app/service.py", "VALUE = 3\n")
        self._commit("Merge pull request #12 from example/topic")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, subject: str) -> None:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", subject)

    def _json(self, payload: object) -> Path:
        path = self.repo / "merged.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_all_git_subject_numbers_covered_passes(self) -> None:
        result = coverage.evaluate(self.repo, previous_tag="v0.1.0", section_header="## Unreleased")

        self.assertTrue(result.ok, result.messages)
        self.assertEqual(result.covered, {10, 11, 12})
        self.assertEqual(result.messages[0], "Changelog coverage: pass (3 PRs)")
        self.assertFalse(result.wiki_changed)

    def test_missing_number_from_git_subjects_fails(self) -> None:
        self._write("app/service.py", "VALUE = 4\n")
        self._commit("fix: unrecorded thing (#20)")

        result = coverage.evaluate(self.repo, previous_tag="v0.1.0", section_header="## Unreleased")

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {20})
        self.assertIn("#20", result.messages[0])
        self.assertIn("Changelog coverage: FAIL", result.messages[0])

    def test_merged_pr_json_adds_numbers_missing_from_subjects(self) -> None:
        self._write("app/service.py", "VALUE = 4\n")
        self._commit("fix: squash subject lost its number")
        merged = self._json([{"number": 13}, {"number": 21}])

        result = coverage.evaluate(
            self.repo,
            previous_tag="v0.1.0",
            section_header="## Unreleased",
            merged_prs_json=merged,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.missing, {21})
        self.assertIn(13, result.covered)

    def test_merged_pr_json_entries_released_before_the_tag_are_ignored(self) -> None:
        tag_time = coverage.tag_commit_time(self.repo, "v0.1.0")
        merged = self._json([5, {"number": 6, "mergedAt": tag_time}, {"number": 10}])

        result = coverage.evaluate(
            self.repo,
            previous_tag="v0.1.0",
            section_header="## Unreleased",
            merged_prs_json=merged,
        )

        self.assertTrue(result.ok, result.messages)
        self.assertNotIn(5, result.missing | result.covered)
        self.assertNotIn(6, result.missing | result.covered)

    def test_numbers_count_only_in_the_target_section(self) -> None:
        self._write("app/service.py", "VALUE = 4\n")
        self._commit("fix: recorded only in an old section (#5)")

        result = coverage.evaluate(self.repo, previous_tag="v0.1.0", section_header="## Unreleased")

        # #5 was released at the tag, so it is dropped rather than reported.
        self.assertTrue(result.ok, result.messages)

        with self.assertRaises(coverage.CoverageError):
            coverage.evaluate(self.repo, previous_tag="v0.1.0", section_header="## v9.9.9")

    def test_wiki_change_requires_a_wiki_commit(self) -> None:
        self._write("wiki/Home.md", "# Home\n")
        self._commit("docs: wiki page (#13)")

        result = coverage.evaluate(self.repo, previous_tag="v0.1.0", section_header="## Unreleased")

        self.assertFalse(result.ok)
        self.assertTrue(result.wiki_changed)
        self.assertIn("--wiki-commit", "\n".join(result.messages))

    def test_wiki_commit_is_verified_against_ls_remote_tips(self) -> None:
        self._write("wiki/Home.md", "# Home\n")
        self._commit("docs: wiki page (#13)")
        with tempfile.TemporaryDirectory() as raw_wiki:
            wiki = Path(raw_wiki)
            _git(wiki, "init", "-q", "-b", "master")
            _git(wiki, "config", "user.email", "test@example.test")
            _git(wiki, "config", "user.name", "Test")
            _git(wiki, "config", "commit.gpgsign", "false")
            (wiki / "Home.md").write_text("# Home\n", encoding="utf-8")
            _git(wiki, "add", "-A")
            _git(wiki, "commit", "-q", "-m", "publish")
            tip = _git(wiki, "rev-parse", "HEAD").strip()

            good = coverage.evaluate(
                self.repo,
                previous_tag="v0.1.0",
                section_header="## Unreleased",
                wiki_commit=tip[:12],
                wiki_remote=str(wiki),
            )
            bad = coverage.evaluate(
                self.repo,
                previous_tag="v0.1.0",
                section_header="## Unreleased",
                wiki_commit="0123456789abcdef",
                wiki_remote=str(wiki),
            )

        self.assertTrue(good.ok, good.messages)
        self.assertEqual(good.wiki_commit, tip[:12])
        self.assertIn(f"External wiki commit: {tip[:12]}", "\n".join(good.messages))
        self.assertFalse(bad.ok)
        self.assertIn("not a branch tip", "\n".join(bad.messages))

    def test_offline_accepts_wiki_commit_without_verification(self) -> None:
        self._write("wiki/Home.md", "# Home\n")
        self._commit("docs: wiki page (#13)")

        result = coverage.evaluate(
            self.repo,
            previous_tag="v0.1.0",
            section_header="## Unreleased",
            wiki_commit="abcdef1",
            offline=True,
        )

        self.assertTrue(result.ok, result.messages)
        self.assertIn("External wiki commit: abcdef1 (not verified: --offline)", result.messages)

    def test_malformed_wiki_commit_is_rejected(self) -> None:
        self._write("wiki/Home.md", "# Home\n")
        self._commit("docs: wiki page (#13)")

        result = coverage.evaluate(
            self.repo,
            previous_tag="v0.1.0",
            section_header="## Unreleased",
            wiki_commit="not-a-sha",
            offline=True,
        )

        self.assertFalse(result.ok)


class CoverageParsingTests(unittest.TestCase):
    def test_subject_number_extraction(self) -> None:
        numbers = coverage.pr_numbers_from_subjects(
            [
                "fix: squash (#10)",
                "[verified] fix: tagged (#11) ",
                "Merge pull request #12 from example/topic",
                "docs: no number",
                "fix: reference in the middle (#13) but not at the end",
            ]
        )

        self.assertEqual(numbers, {10, 11, 12})

    def test_section_number_extraction_reads_parentheticals_only(self) -> None:
        section = coverage.changelog_section(CHANGELOG_TEMPLATE, "## Unreleased")
        numbers = coverage.pr_numbers_in_section(section)

        self.assertEqual(numbers, {10, 11, 12, 13, 14})
        self.assertNotIn(5, numbers)


class RenderReleaseNotesTests(unittest.TestCase):
    def test_render_prints_highlights_then_upgrade_notes(self) -> None:
        rendered = render_release_notes.render(CHANGELOG_TEMPLATE, "## Unreleased")

        self.assertEqual(
            rendered,
            "## Highlights\n\n- Two things shipped.\n\n"
            "## Upgrade notes\n\n- `SETTING` now defaults to off; set it explicitly (#12).\n",
        )
        self.assertNotIn("Fixed the first thing", rendered)

    def test_render_requires_highlights(self) -> None:
        with self.assertRaises(render_release_notes.RenderError):
            render_release_notes.render(CHANGELOG_TEMPLATE, "## v0.1.0 - 2026-01-01")

    def test_render_current_unreleased_section(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "CHANGELOG.md").read_text(encoding="utf-8")

        rendered = render_release_notes.render(text, "## Unreleased")

        self.assertTrue(rendered.startswith("## Highlights\n\n- "))
        self.assertIn("## Upgrade notes", rendered)


if __name__ == "__main__":
    unittest.main()
