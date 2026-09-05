from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.validate_release_wrap import DOCS_PUBLICATION_GATE, REQUIRED_GATES


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "verify_wiki_drift.py"
RELEASE_VALIDATOR = REPOSITORY / "scripts" / "validate_release_wrap.py"


class WikiDriftVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        self.external = self.root / "external-wiki"
        self._init_repository(self.repository)
        self._init_repository(self.external)

        self._write(self.repository / "wiki" / "Home.md", b"# Home\n")
        self._write(self.repository / "wiki" / "images" / "overview.png", b"\x89PNG\r\nrepo-image\x00")
        self.repository_commit = self._commit(self.repository, "repository wiki")
        subprocess.run(
            ["git", "tag", "v0.22.3", self.repository_commit],
            cwd=self.repository,
            check=True,
        )

        self._write(self.external / "Home.md", b"# Home\n")
        self._write(self.external / "images" / "overview.png", b"\x89PNG\r\nrepo-image\x00")
        self.external_commit = self._commit(self.external, "external wiki")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _init_repository(path: Path) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Wiki Test"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "wiki@example.test"], cwd=path, check=True)

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    @staticmethod
    def _commit(path: Path, message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _run(
        self,
        *,
        repository_commit: str | None = None,
        external_commit: str | None = None,
        wiki_source: Path | str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repository",
                str(self.repository),
                "--repository-commit",
                repository_commit or self.repository_commit,
                "--wiki-source",
                str(wiki_source or self.external),
                "--external-wiki-commit",
                external_commit or self.external_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def _write_release_wrap(
        self,
        evidence: str,
        *,
        release_commit: str | None = None,
    ) -> None:
        lines = [
            "# Release Wrap - v0.22.3",
            "",
            f"Release commit: `{release_commit or self.repository_commit}`.",
            "",
            "Validated against `docs/RELEASE_CHECKLIST.md`.",
            "",
            "| Gate | Required | Evidence | Result | N/A Reason |",
            "| --- | --- | --- | --- | --- |",
        ]
        for gate in REQUIRED_GATES:
            lines.append(f"| {gate} | yes | evidence | Pass |  |")
        lines.append(f"| {DOCS_PUBLICATION_GATE} | yes | {evidence} | Pass |  |")
        self._write(
            self.repository / "docs" / "RELEASE_WRAP_0.22.3.md",
            "\n".join(lines).encode(),
        )

    def _run_release_validator(
        self,
        *,
        external_commit: str | None = None,
        include_wiki_arguments: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [sys.executable, str(RELEASE_VALIDATOR), "0.22.3"]
        if include_wiki_arguments:
            arguments.extend(
                (
                    "--repository",
                    str(self.repository),
                    "--repository-commit",
                    self.repository_commit,
                    "--wiki-source",
                    str(self.external),
                    "--external-wiki-commit",
                    external_commit or self.external_commit,
                )
            )
        return subprocess.run(
            arguments,
            cwd=self.repository,
            check=False,
            capture_output=True,
            text=True,
        )

    def _external_revision(
        self,
        *,
        writes: dict[str, bytes] | None = None,
        deletes: tuple[str, ...] = (),
    ) -> str:
        for relative_path, content in (writes or {}).items():
            self._write(self.external / relative_path, content)
        for relative_path in deletes:
            (self.external / relative_path).unlink()
        return self._commit(self.external, "external revision")

    def test_equal_committed_trees_emit_exact_commit_evidence(self) -> None:
        self._write(self.repository / "wiki" / "Home.md", b"dirty repository bytes\n")
        self._write(self.external / "Home.md", b"dirty external bytes\n")

        completed = self._run()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "Wiki drift verification: PASS",
                f"Repository commit: {self.repository_commit}",
                f"External wiki commit: {self.external_commit}",
                "Compared files: 2",
            ],
        )

    def test_repository_commit_must_match_source_head(self) -> None:
        stale_commit = self.repository_commit
        self._write(self.repository / "wiki" / "Home.md", b"# Release wiki\n")
        release_commit = self._commit(self.repository, "release wiki update")

        completed = self._run(repository_commit=stale_commit)

        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            f"repository commit does not match repository source HEAD {release_commit}",
            completed.stderr,
        )
        self.assertNotIn("PASS", completed.stdout + completed.stderr)

    def test_external_git_url_is_fetched_without_credentials(self) -> None:
        completed = self._run(wiki_source=self.external.as_uri())

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"External wiki commit: {self.external_commit}", completed.stdout)

    def test_missing_page_fails_closed(self) -> None:
        external_commit = self._external_revision(deletes=("Home.md",))

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Wiki drift verification: FAIL", completed.stdout)
        self.assertIn("Missing from external wiki: Home.md", completed.stdout)
        self.assertNotIn("Wiki drift verification: PASS", completed.stdout)

    def test_extra_page_fails_closed(self) -> None:
        external_commit = self._external_revision(writes={"Extra.md": b"extra\n"})

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Extra in external wiki: Extra.md", completed.stdout)

    def test_changed_page_bytes_fail_closed(self) -> None:
        external_commit = self._external_revision(writes={"Home.md": b"# Home\r\n"})

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertRegex(
            completed.stdout,
            r"Changed: Home\.md \(repository sha256=[0-9a-f]{64}, external sha256=[0-9a-f]{64}\)",
        )

    def test_missing_image_fails_closed(self) -> None:
        external_commit = self._external_revision(deletes=("images/overview.png",))

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Missing from external wiki: images/overview.png", completed.stdout)

    def test_extra_image_fails_closed(self) -> None:
        external_commit = self._external_revision(writes={"images/extra.png": b"extra-image\x00"})

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Extra in external wiki: images/extra.png", completed.stdout)

    def test_changed_image_bytes_fail_closed(self) -> None:
        external_commit = self._external_revision(
            writes={"images/overview.png": b"\x89PNG\r\nexternal-image\x00"}
        )

        completed = self._run(external_commit=external_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertRegex(completed.stdout, r"Changed: images/overview\.png ")

    def test_malformed_commit_is_an_error(self) -> None:
        completed = self._run(external_commit="c5b7dd4")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Wiki drift verification: ERROR", completed.stderr)
        self.assertIn("external wiki commit must be exactly 40 lowercase hexadecimal characters", completed.stderr)
        self.assertNotIn("PASS", completed.stdout + completed.stderr)

    def test_unavailable_source_is_an_error(self) -> None:
        completed = self._run(wiki_source=self.root / "missing-wiki")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Wiki drift verification: ERROR", completed.stderr)
        self.assertIn("external wiki source is unavailable", completed.stderr)
        self.assertNotIn("PASS", completed.stdout + completed.stderr)

    def test_malformed_source_is_an_error(self) -> None:
        malformed_source = self.root / "malformed-wiki"
        malformed_source.mkdir()
        self._write(malformed_source / "Home.md", b"not committed\n")

        completed = self._run(wiki_source=malformed_source)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("external wiki source is not a Git repository", completed.stderr)
        self.assertNotIn("PASS", completed.stdout + completed.stderr)

    def test_external_commit_must_match_the_source_head(self) -> None:
        self._external_revision(writes={"Home.md": b"new published bytes\n"})

        completed = self._run(external_commit=self.external_commit)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("external wiki commit does not match external wiki source HEAD", completed.stderr)
        self.assertNotIn("PASS", completed.stdout + completed.stderr)

    def test_git_replace_cannot_substitute_external_commit_bytes(self) -> None:
        changed_commit = self._external_revision(writes={"Home.md": b"changed bytes\n"})
        subprocess.run(
            ["git", "replace", changed_commit, self.external_commit],
            cwd=self.external,
            check=True,
        )

        completed = self._run(external_commit=changed_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Changed: Home.md", completed.stdout)
        self.assertNotIn("Wiki drift verification: PASS", completed.stdout)

    def test_release_validator_requires_the_wiki_verification_inputs(self) -> None:
        self._write_release_wrap("evidence")

        completed = self._run_release_validator(include_wiki_arguments=False)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("wiki verification requires", completed.stdout)

    def test_release_validator_accepts_only_the_exact_equal_tree_receipt(self) -> None:
        evidence = "; ".join(
            (
                "Wiki drift verification: PASS",
                f"Repository commit: {self.repository_commit}",
                f"External wiki commit: {self.external_commit}",
                "Compared files: 2",
            )
        )
        self._write_release_wrap(evidence)

        completed = self._run_release_validator()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("checklist evidence is complete", completed.stdout)

    def test_final_validator_rejects_stale_ancestor_of_release_tag(self) -> None:
        stale_commit = self.repository_commit
        self._write(self.repository / "wiki" / "Home.md", b"# Release wiki\n")
        release_commit = self._commit(self.repository, "release wiki update")
        subprocess.run(
            ["git", "tag", "-f", "v0.22.3", release_commit],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )
        evidence = "; ".join(
            (
                "Wiki drift verification: PASS",
                f"Repository commit: {stale_commit}",
                f"External wiki commit: {self.external_commit}",
                "Compared files: 2",
            )
        )
        self._write_release_wrap(evidence, release_commit=release_commit)

        completed = self._run_release_validator()

        self.assertEqual(completed.returncode, 1)
        self.assertIn(
            f"repository commit does not match release tag v0.22.3 {release_commit}",
            completed.stdout,
        )
        self.assertNotIn("checklist evidence is complete", completed.stdout)

    def test_release_validator_rejects_tree_drift_even_with_matching_commit_tokens(self) -> None:
        changed_commit = self._external_revision(writes={"Home.md": b"changed\n"})
        evidence = "; ".join(
            (
                "Wiki drift verification: PASS",
                f"Repository commit: {self.repository_commit}",
                f"External wiki commit: {changed_commit}",
                "Compared files: 2",
            )
        )
        self._write_release_wrap(evidence)

        completed = self._run_release_validator(external_commit=changed_commit)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("external wiki differs from repository wiki/", completed.stdout)

    def test_wiki_publish_guide_keeps_publication_manual_and_documents_verification(self) -> None:
        guide = (REPOSITORY / "wiki" / "Publishing-the-Wiki.md").read_text(encoding="utf-8")

        self.assertIn("python scripts/verify_wiki_drift.py", guide)
        self.assertIn("--repository-commit", guide)
        self.assertIn("--external-wiki-commit", guide)
        self.assertIn("does not publish or change the external wiki", guide)
        self.assertIn("owner", guide.lower())
        self.assertNotIn("SCREENSHOT_TAG='v0.18.0'", guide)

    def test_release_checklist_runs_the_verifier_with_both_exact_commits(self) -> None:
        checklist = (REPOSITORY / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        self.assertIn("python scripts/verify_wiki_drift.py", checklist)
        self.assertIn("--repository-commit", checklist)
        self.assertIn("--external-wiki-commit", checklist)
        self.assertIn("Wiki drift verification: PASS", checklist)
        self.assertIn("External wiki commit: <sha>", checklist)
        self.assertIn("after v0.22.2", checklist)
        self.assertNotIn("starting with v0.22.3", checklist)

    def test_release_checklist_keeps_canonical_validator_commands_runnable(self) -> None:
        checklist = (REPOSITORY / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        pre_tag_command = """```bash
version="<version>"
repo_commit="$(git rev-parse HEAD)"
wiki_commit="<full 40-character external wiki commit>"
python scripts/validate_release_wrap.py "$version" --phase pre-tag \\
  --repository . \\
  --repository-commit "$repo_commit" \\
  --wiki-source https://github.com/gcs8/truenas-jbod-ui.wiki.git \\
  --external-wiki-commit "$wiki_commit"
```"""
        final_command = """```bash
version="<version>"
repo_commit="$(git rev-parse "v${version#v}^{commit}")"
wiki_commit="<full 40-character external wiki commit>"
python scripts/validate_release_wrap.py "$version" \\
  --repository . \\
  --repository-commit "$repo_commit" \\
  --wiki-source https://github.com/gcs8/truenas-jbod-ui.wiki.git \\
  --external-wiki-commit "$wiki_commit"
```"""

        self.assertIn(pre_tag_command, checklist)
        self.assertIn(final_command, checklist)


if __name__ == "__main__":
    unittest.main()
