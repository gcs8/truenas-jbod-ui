from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from scripts.validate_release_wrap import (
    DEFERRABLE_NARROW_BETA_GATES,
    NARROW_BETA_QA_TOOLING,
    RELEASE_MODE_LINE,
    REQUIRED_GATES,
    changelog_coverage_required,
    public_demo_freshness_required,
    release_wrap_path,
    validate_release_wrap_text,
)
from scripts.verify_wiki_drift import ChangedFile, WikiVerificationResult

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _wrap_with_rows(
    rows: dict[str, tuple[str, str, str, str]],
    *,
    coverage_line: str = "Changelog coverage: pass (12 PRs)",
) -> str:
    lines = [
        "# Release Wrap - v0.20.2",
        "",
        "Validated against `docs/RELEASE_CHECKLIST.md`.",
        "",
        coverage_line,
        "",
        "| Gate | Required | Evidence | Result | N/A Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in REQUIRED_GATES:
        required, evidence, result, reason = rows.get(gate, ("yes", "evidence", "Pass", ""))
        lines.append(f"| {gate} | {required} | {evidence} | {result} | {reason} |")
    for gate in rows:
        if gate in REQUIRED_GATES:
            continue
        required, evidence, result, reason = rows[gate]
        lines.append(f"| {gate} | {required} | {evidence} | {result} | {reason} |")
    return "\n".join(lines)


def _wiki_result(*, changed: bool = False) -> WikiVerificationResult:
    changed_files = (
        ChangedFile("Home.md", "1" * 64, "2" * 64),
    ) if changed else ()
    return WikiVerificationResult(
        repository_commit="a" * 40,
        external_wiki_commit="b" * 40,
        compared_files=2,
        missing_from_external=(),
        extra_in_external=(),
        changed=changed_files,
    )


class ReleaseWrapValidatorTests(unittest.TestCase):
    def test_release_checklist_separates_source_and_publication_gates(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

        self.assertIn("| Docs/wiki/public-demo gate |", text)
        self.assertIn("| Docs/wiki/public-demo publication |", text)
        self.assertIn("Pending owner publication: external wiki, public demo", text)

    def test_release_wrap_path_prefers_current_then_archived_wrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            current = repository / "docs" / "RELEASE_WRAP_0.1.0.md"
            archived = repository / "docs" / "archive" / "RELEASE_WRAP_0.1.0.md"

            self.assertEqual(release_wrap_path(repository, "0.1.0"), current)

            archived.parent.mkdir(parents=True)
            archived.write_text("wrap\n", encoding="utf-8")
            self.assertEqual(release_wrap_path(repository, "0.1.0"), archived)

            current.write_text("wrap\n", encoding="utf-8")
            self.assertEqual(release_wrap_path(repository, "0.1.0"), current)

    def test_every_archived_release_wrap_is_readable_by_version(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        archived = sorted((repository / "docs" / "archive").glob("RELEASE_WRAP_*.md"))

        self.assertGreater(len(archived), 0)
        self.assertFalse(list((repository / "docs").glob("RELEASE_WRAP_0.22.*.md")))
        for path in archived:
            version = path.stem.removeprefix("RELEASE_WRAP_")
            with self.subTest(version=version):
                self.assertEqual(release_wrap_path(repository, version), path)

    def test_v0222_final_release_wrap_is_complete(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "archive" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")

        self.assertFalse(changelog_coverage_required("0.22.2"))
        self.assertEqual(validate_release_wrap_text(text, require_changelog_coverage=False), [])
        for marker in (
            "https://github.com/gcs8/truenas-jbod-ui/releases/tag/v0.22.2",
            "https://github.com/gcs8/truenas-jbod-ui/actions/runs/33505635256",
            "6473d05f46d8344146cbbd7d0cdbf44487613a3c",
            "sha256:4bfa37a4c40a058055aef384194f98248722eca25f7fb429d6a5a34446d647a7",
            "Development resumed on `main` after the tag",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        self.assertNotIn("Tag: `v0.22.2` pending", text)
        self.assertNotIn("Release candidate commit: pending", text)

    def test_v0222_release_wrap_reconciles_pr121_after_release(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "archive" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")

        self.assertIn("Post-release reconciliation", text)
        self.assertIn("PR #121 merged", text)
        self.assertIn("Issue #124 closed", text)
        self.assertNotIn("PR #121 remains draft", text)

    def test_v0222_deployment_gate_records_private_receipt_validation(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "archive" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")
        deployment_row = next(
            line for line in text.splitlines() if line.startswith("| Deployment refresh/sniff tests |")
        )

        self.assertIn("private deployment receipt was validated", deployment_row)
        self.assertIn("runtime convergence was reverified", deployment_row)
        self.assertIn("Private deployment identifiers are not retained", deployment_row)

    def test_v0230_wrap_records_publication_and_remaining_holds(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "RELEASE_WRAP_0.23.0.md").read_text(encoding="utf-8")

        self.assertEqual(
            validate_release_wrap_text(
                text,
                allow_blocked=True,
                phase="pre-tag",
                require_wiki_verification=True,
            ),
            [],
        )
        self.assertEqual(
            validate_release_wrap_text(
                text,
                phase="pre-tag",
                require_wiki_verification=True,
            ),
            [],
        )
        self.assertIn("Tag: `v0.23.0`, published 2026-09-09", text)
        self.assertIn("https://github.com/gcs8/truenas-jbod-ui/actions/runs/34293966354", text)
        self.assertIn(
            "ghcr.io/gcs8/truenas-jbod-ui@sha256:567d42025850a048186cf35adb2c97c071d27fa93f3071ec0eb3ddd71dd9df17",
            text,
        )
        self.assertIn("Pending owner publication: external wiki", text)
        self.assertIn("Private deployment qualification remains unverified", text)
        self.assertIn("app version remains `0.23.0`", text)
        self.assertIn("Development reopened before private deployment qualification completed", text)
        self.assertNotIn("Tag: `v0.23.0` pending", text)
        self.assertNotIn("Do not tag or publish v0.23.0", text)

    def test_accepts_complete_release_wrap_evidence_table(self) -> None:
        issues = validate_release_wrap_text(_wrap_with_rows({}))

        self.assertEqual(issues, [])

    def test_requires_changelog_coverage_evidence_line(self) -> None:
        text = _wrap_with_rows({}, coverage_line="")

        issues = validate_release_wrap_text(text)

        self.assertIn(
            "release wrap must record the changelog coverage gate as "
            "'Changelog coverage: pass (<N> PRs)' from scripts/check_release_changelog_coverage.py",
            [issue.message for issue in issues],
        )
        self.assertEqual(validate_release_wrap_text(text, require_changelog_coverage=False), [])

    def test_changelog_coverage_requirement_starts_at_v0223(self) -> None:
        self.assertFalse(changelog_coverage_required("v0.22.2"))
        self.assertTrue(changelog_coverage_required("0.22.3"))
        self.assertTrue(changelog_coverage_required("v0.23.0"))
        self.assertTrue(changelog_coverage_required("1.0.0"))

    def test_wiki_verification_requires_a_distinct_publication_row(self) -> None:
        issues = validate_release_wrap_text(
            _wrap_with_rows({}),
            phase="pre-tag",
            require_wiki_verification=True,
        )

        self.assertIn(
            "missing checklist evidence row: Docs/wiki/public-demo publication",
            [issue.message for issue in issues],
        )

    def test_wiki_publication_gate_pass_requires_a_verification_run(self) -> None:
        issues = validate_release_wrap_text(
            _wrap_with_rows(
                {
                    "Docs/wiki/public-demo publication": (
                        "yes",
                        "publication evidence",
                        "Pass",
                        "",
                    ),
                }
            ),
            require_wiki_verification=True,
        )

        self.assertIn(
            "Docs/wiki/public-demo publication: wiki drift verification was not run",
            [issue.message for issue in issues],
        )

    def test_wiki_publication_gate_pass_rejects_a_drift_result(self) -> None:
        result = _wiki_result(changed=True)
        issues = validate_release_wrap_text(
            _wrap_with_rows(
                {
                    "Docs/wiki/public-demo publication": (
                        "yes",
                        "publication evidence",
                        "Pass",
                        "",
                    ),
                }
            ),
            require_wiki_verification=True,
            wiki_verification=result,
        )

        self.assertIn(
            "Docs/wiki/public-demo publication: external wiki differs from repository wiki/",
            [issue.message for issue in issues],
        )
        with self.assertRaisesRegex(ValueError, "cannot create PASS evidence from wiki drift"):
            result.release_evidence()

    def test_wiki_publication_gate_requires_pass_after_publication(self) -> None:
        result = _wiki_result()
        text = _wrap_with_rows(
            {
                "Docs/wiki/public-demo publication": (
                    "yes",
                    result.release_evidence(),
                    "N/A",
                    "wiki unchanged",
                ),
            }
        )

        issues = validate_release_wrap_text(
            text,
            require_wiki_verification=True,
            wiki_verification=result,
        )

        self.assertIn(
            "Docs/wiki/public-demo publication: Result must be Pass after publication",
            [issue.message for issue in issues],
        )

    def test_wiki_publication_gate_requires_exact_commit_evidence(self) -> None:
        result = _wiki_result()
        issues = validate_release_wrap_text(
            _wrap_with_rows(
                {
                    "Docs/wiki/public-demo publication": (
                        "yes",
                        "publication evidence",
                        "Pass",
                        "",
                    ),
                }
            ),
            require_wiki_verification=True,
            wiki_verification=result,
        )

        self.assertIn(
            "Docs/wiki/public-demo publication: evidence does not contain the exact wiki verification receipt",
            [issue.message for issue in issues],
        )

        text = _wrap_with_rows(
            {
                "Docs/wiki/public-demo publication": (
                    "yes",
                    result.release_evidence(),
                    "Pass",
                    "",
                ),
            }
        )
        self.assertEqual(
            validate_release_wrap_text(
                text,
                require_wiki_verification=True,
                wiki_verification=result,
            ),
            [],
        )

    def test_pre_tag_requires_docs_source_checks_before_publication(self) -> None:
        text = _wrap_with_rows(
            {
                "Docs/wiki/public-demo gate": (
                    "yes",
                    "awaiting owner-approved wiki publication",
                    "Blocked",
                    "",
                ),
                "Docs/wiki/public-demo publication": (
                    "yes",
                    "Pending owner publication: external wiki",
                    "Blocked",
                    "",
                ),
            }
        )

        issues = validate_release_wrap_text(
            text,
            phase="pre-tag",
            require_wiki_verification=True,
        )

        self.assertIn(
            "Docs/wiki/public-demo gate: Blocked gates cannot ship",
            [issue.message for issue in issues],
        )

    def test_pre_tag_allows_only_docs_publication_to_remain_blocked(self) -> None:
        text = _wrap_with_rows(
            {
                "Docs/wiki/public-demo gate": (
                    "yes",
                    "source, diff, privacy, and checked-in artifact checks passed",
                    "Pass",
                    "",
                ),
                "Docs/wiki/public-demo publication": (
                    "yes",
                    "Pending owner publication: external wiki; public demo",
                    "Blocked",
                    "",
                ),
            }
        )

        self.assertEqual(
            validate_release_wrap_text(
                text,
                phase="pre-tag",
                require_wiki_verification=True,
            ),
            [],
        )

    def test_pre_tag_rejects_unrelated_publication_blocker_evidence(self) -> None:
        text = _wrap_with_rows(
            {
                "Docs/wiki/public-demo publication": (
                    "yes",
                    "awaiting GHCR image",
                    "Blocked",
                    "",
                ),
            }
        )

        issues = validate_release_wrap_text(
            text,
            phase="pre-tag",
            require_wiki_verification=True,
        )

        self.assertIn(
            "Docs/wiki/public-demo publication: Blocked evidence must identify pending owner publication",
            [issue.message for issue in issues],
        )

    def test_requires_all_global_checklist_rows(self) -> None:
        text = _wrap_with_rows({}).replace(
            "| GHCR publish verification | yes | evidence | Pass |  |\n",
            "",
        )

        issues = validate_release_wrap_text(text)

        self.assertIn(
            "missing checklist evidence row: GHCR publish verification",
            [issue.message for issue in issues],
        )

    def test_requires_concrete_na_reason(self) -> None:
        text = _wrap_with_rows(
            {
                "GHCR publish verification": ("yes", "", "N/A", ""),
            }
        )

        issues = validate_release_wrap_text(text)

        self.assertIn(
            "GHCR publish verification: N/A requires a concrete reason",
            [issue.message for issue in issues],
        )

    def test_blocked_gate_fails_ship_validation(self) -> None:
        text = _wrap_with_rows(
            {
                "Linux QA restore gate": ("yes", "restore failed", "Blocked", ""),
            }
        )

        issues = validate_release_wrap_text(text)

        self.assertIn(
            "Linux QA restore gate: Blocked gates cannot ship",
            [issue.message for issue in issues],
        )

    def test_pre_tag_validation_allows_only_post_publish_blockers(self) -> None:
        text = _wrap_with_rows(
            {
                "GHCR publish verification": ("yes", "awaiting public tag workflow", "Blocked", ""),
                "Deployment refresh/sniff tests": ("yes", "awaiting GHCR image", "Blocked", ""),
                "Post-release reopen": ("yes", "awaiting release cut", "Blocked", ""),
            }
        )

        issues = validate_release_wrap_text(text, phase="pre-tag")

        self.assertEqual(issues, [])

    def test_pre_tag_validation_still_rejects_pre_publish_blockers(self) -> None:
        text = _wrap_with_rows(
            {
                "Linux QA restore gate": ("yes", "restore failed", "Blocked", ""),
            }
        )

        issues = validate_release_wrap_text(text, phase="pre-tag")

        self.assertIn(
            "Linux QA restore gate: Blocked gates cannot ship",
            [issue.message for issue in issues],
        )

    def test_public_demo_rebuild_is_required_from_the_first_release_after_v0230(self) -> None:
        self.assertFalse(public_demo_freshness_required("v0.23.0"))
        self.assertTrue(public_demo_freshness_required("0.23.1"))
        self.assertTrue(public_demo_freshness_required("v0.24.0"))
        self.assertTrue(public_demo_freshness_required("1.0.0"))

    def test_a_prerelease_leaves_the_public_demo_for_the_stable_release(self) -> None:
        self.assertFalse(public_demo_freshness_required("v0.24.0-beta.1"))
        self.assertFalse(public_demo_freshness_required("0.24.0-rc.2"))
        self.assertFalse(public_demo_freshness_required("v0.24.0-rc.1+build.5"))
        # Build metadata alone is not a prerelease, even with a hyphen in it.
        self.assertTrue(public_demo_freshness_required("v0.24.0+build-1"))
        with self.assertRaises(ValueError):
            public_demo_freshness_required("v0.24-beta")

    def test_public_demo_only_passes_a_prerelease_and_still_rejects_a_bad_version(self) -> None:
        def run(version: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "scripts/validate_release_wrap.py", version, "--public-demo-only"],
                cwd=REPOSITORY_ROOT, text=True, capture_output=True, check=False,
            )

        beta = run("v99.0.0-beta.1")
        self.assertEqual(beta.returncode, 0, beta.stdout)
        self.assertIn("waits for the next stable release", beta.stdout)

        malformed = run("v99.0-beta.1")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("semantic version form", malformed.stdout)

    def test_release_refuses_a_public_demo_that_was_not_rebuilt_for_it(self) -> None:
        # The checked-in demo carries its own app version. Asking for any other
        # version must fail before any tag, image, or Pages publication.
        result = subprocess.run(
            [sys.executable, "scripts/validate_release_wrap.py", "v99.0.0", "--public-demo-only"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docs/wiki/public-demo gate", result.stdout)
        self.assertIn("not the release version 99.0.0", result.stdout)
        self.assertIn("Public demo rebuild", result.stdout)

    def test_public_demo_release_issues_run_the_strict_current_source_check(self) -> None:
        source = (REPOSITORY_ROOT / "scripts" / "validate_release_wrap.py").read_text(encoding="utf-8")

        self.assertIn('"--require-current"', source)
        self.assertIn('"scripts/check_public_screenshots.py"', source)

    def test_final_validation_rejects_post_publish_blockers(self) -> None:
        text = _wrap_with_rows(
            {
                "GHCR publish verification": ("yes", "workflow pending", "Blocked", ""),
            }
        )

        issues = validate_release_wrap_text(text)

        self.assertIn(
            "GHCR publish verification: Blocked gates cannot ship",
            [issue.message for issue in issues],
        )


NARROW_DEFERRED_EVIDENCE = "Deferred to the next rc/final full run under the narrow-beta policy"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Release Test",
    "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
    "GIT_COMMITTER_NAME": "Release Test",
    "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        env={**os.environ, **GIT_ENV},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _write(repository: Path, relative: str, content: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repository: Path, message: str) -> None:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "-m", message)


def _version_files(version: str) -> dict[str, str]:
    return {
        "app/__init__.py": f'"""Synthetic app."""\n\n__version__ = "{version}"\n',
        "package.json": f'{{\n  "name": "synthetic",\n  "version": "{version}",\n  "private": true\n}}\n',
        "package-lock.json": (
            f'{{\n  "name": "synthetic",\n  "version": "{version}",\n  "packages": {{\n'
            f'    "": {{\n      "name": "synthetic",\n      "version": "{version}"\n    }}\n  }}\n}}\n'
        ),
    }


def _narrow_repository(
    root: Path,
    *,
    previous: str = "1.0.0-beta.1",
    candidate: str = "1.0.0-beta.2",
    extra_changes: dict[str, str] | None = None,
    tag_candidate: bool = False,
) -> Path:
    """Build a synthetic repository: a previous release tag plus a candidate commit."""

    repository = root / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    for relative, content in {
        **_version_files(previous),
        "app/main.py": "VALUE = 1\n",
        "app/services/inventory.py": "VALUE = 1\n",
        "app/static/app.js": "const value = 1;\n",
        "Dockerfile": "FROM scratch\n",
        "docs/README.md": "docs\n",
    }.items():
        _write(repository, relative, content)
    _commit(repository, "previous release")
    _git(repository, "tag", f"v{previous}")
    changes = {
        **_version_files(candidate),
        "app/static/app.js": "const value = 2;\n",
        "app/templates/index.html": "<p>new</p>\n",
        "tests/test_new.py": "VALUE = 2\n",
        "qa/new.spec.js": "// new\n",
        "scripts/build_ui_crawl_fixture.py": "VALUE = 2\n",
        "docs/RELEASE_WRAP_x.md": "wrap\n",
        "wiki/Home.md": "wiki\n",
        "CHANGELOG.md": "changes\n",
        "README.md": "readme\n",
        **(extra_changes or {}),
    }
    for relative, content in changes.items():
        _write(repository, relative, content)
    _commit(repository, "candidate")
    if tag_candidate:
        _git(repository, "tag", f"v{candidate}")
    return repository


def _narrow_wrap(
    *,
    deferred: tuple[str, ...] = DEFERRABLE_NARROW_BETA_GATES,
    evidence: str = NARROW_DEFERRED_EVIDENCE,
    mode_lines: tuple[str, ...] = ("Release mode: narrow beta", "Previous release tag: `v1.0.0-beta.1`"),
) -> str:
    text = _wrap_with_rows({gate: ("yes", evidence, "Deferred", "") for gate in deferred})
    return text.replace(
        "Validated against `docs/RELEASE_CHECKLIST.md`.",
        "\n".join(("Validated against `docs/RELEASE_CHECKLIST.md`.", "", *mode_lines)),
    )


class NarrowBetaReleaseWrapTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _messages(self, text: str, **kwargs: object) -> list[str]:
        kwargs.setdefault("version", "1.0.0-beta.2")
        kwargs.setdefault("phase", "pre-tag")
        return [issue.message for issue in validate_release_wrap_text(text, **kwargs)]

    def test_deferrable_gates_are_exactly_the_long_rows(self) -> None:
        self.assertEqual(
            DEFERRABLE_NARROW_BETA_GATES,
            (
                "Optional-sidecar runtime matrix",
                "Local release perf harnesses",
                "Linux QA restore gate",
                "Restored Linux QA perf harnesses",
            ),
        )
        self.assertTrue(set(DEFERRABLE_NARROW_BETA_GATES) <= set(REQUIRED_GATES))

    def test_eligible_narrow_beta_accepts_deferred_rows(self) -> None:
        repository = _narrow_repository(self.root, tag_candidate=True)

        self.assertEqual(self._messages(_narrow_wrap(), repository=repository), [])
        self.assertEqual(
            self._messages(_narrow_wrap(), repository=repository, phase="final"),
            [],
        )

    def test_backend_paths_make_the_beta_full_and_are_listed(self) -> None:
        repository = _narrow_repository(
            self.root,
            extra_changes={
                "app/services/inventory.py": "VALUE = 2\n",
                "Dockerfile": "FROM scratch\nUSER 1000\n",
                "history_service/main.py": "VALUE = 2\n",
            },
        )

        messages = self._messages(_narrow_wrap(), repository=repository)

        joined = "\n".join(messages)
        self.assertIn("not eligible for a narrow beta", joined)
        for path in ("Dockerfile", "app/services/inventory.py", "history_service/main.py"):
            with self.subTest(path=path):
                self.assertIn(path, joined)
        self.assertNotIn("app/static/app.js", joined)

    def test_rename_out_of_an_ineligible_path_lists_the_source(self) -> None:
        repository = _narrow_repository(self.root)
        _git(repository, "mv", "app/main.py", "docs/main.md")
        _commit(repository, "rename")

        joined = "\n".join(self._messages(_narrow_wrap(), repository=repository))

        self.assertIn("app/main.py", joined)

    def test_version_files_may_only_change_the_version(self) -> None:
        repository = _narrow_repository(
            self.root,
            extra_changes={
                "app/__init__.py": '"""Synthetic app."""\n\n__version__ = "1.0.0-beta.2"\nDEBUG = True\n',
            },
        )

        joined = "\n".join(self._messages(_narrow_wrap(), repository=repository))

        self.assertIn("app/__init__.py", joined)

    def test_version_patch_header_spoof_is_rejected(self) -> None:
        repository = _narrow_repository(self.root, extra_changes={
            "app/__init__.py": _version_files("1.0.0-beta.2")["app/__init__.py"]
            + '++ (globals().__setitem__("RUNTIME_FEATURE", True) or 0)\n',
        })
        self.assertIn("app/__init__.py", "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_missing_version_assignment_is_rejected(self) -> None:
        repository = _narrow_repository(self.root, extra_changes={
            "app/__init__.py": '"""Synthetic app."""\n\n',
        })
        self.assertIn("app/__init__.py", "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_json_version_bump_with_extra_line_is_rejected(self) -> None:
        for path in ("package.json", "package-lock.json"):
            with self.subTest(path=path), tempfile.TemporaryDirectory(dir=self.root) as raw:
                content = _version_files("1.0.0-beta.2")[path].replace(
                    '{\n', '{\n  "extra": true,\n', 1,
                )
                repository = _narrow_repository(Path(raw), extra_changes={path: content})
                self.assertIn(path, "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_version_bump_preserves_every_other_byte(self) -> None:
        for suffix in (b"\n", b"\r\n", b"\xff"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(dir=self.root) as raw:
                repository = _narrow_repository(Path(raw))
                path = repository / "app/__init__.py"
                path.write_bytes(path.read_bytes() + suffix)
                _commit(repository, "extra bytes")
                self.assertIn("app/__init__.py", "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_version_file_deletion_or_rename_is_rejected(self) -> None:
        for path in _version_files("1.0.0-beta.2"):
            for rename in (False, True):
                with self.subTest(path=path, rename=rename), tempfile.TemporaryDirectory(dir=self.root) as raw:
                    repository = _narrow_repository(Path(raw))
                    if rename:
                        _git(repository, "mv", path, "docs/version.md")
                    else:
                        (repository / path).unlink()
                    _commit(repository, "remove version file")
                    self.assertIn(path, "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_clean_version_bump_preserves_dependency_versions(self) -> None:
        repository = _narrow_repository(self.root)
        _git(repository, "reset", "--hard", "v1.0.0-beta.1")
        path = "package-lock.json"
        dependency = '    "node_modules/synthetic": {\n      "version": "1.0.0-beta.1"\n    },\n'
        old = _version_files("1.0.0-beta.1")[path].replace('  "packages": {\n', '  "packages": {\n' + dependency)
        _write(repository, path, old)
        _commit(repository, "dependency version")
        _git(repository, "tag", "-f", "v1.0.0-beta.1")
        for relative, content in _version_files("1.0.0-beta.2").items():
            if relative == path:
                content = content.replace('  "packages": {\n', '  "packages": {\n' + dependency)
            _write(repository, relative, content)
        _commit(repository, "clean bump")
        self.assertEqual(self._messages(_narrow_wrap(), repository=repository), [])

    def test_appended_deferred_row_is_rejected(self) -> None:
        repository = _narrow_repository(self.root)
        for required in ("yes", "no"):
            for version, mode in (("1.0.0-beta.2", True), ("1.0.0-beta.2", False), ("1.0.0-rc.1", True), ("1.0.0", True)):
                with self.subTest(required=required, version=version, mode=mode):
                    text = _narrow_wrap(deferred=()) if mode else _wrap_with_rows({})
                    text += f"\n| Human-view gate | {required} | {NARROW_DEFERRED_EVIDENCE} | Deferred |  |\n"
                    joined = "\n".join(self._messages(text, repository=repository, version=version))
                    self.assertIn("Human-view gate: Deferred is not allowed for this gate", joined)
                    if not mode:
                        self.assertIn("Deferred rows require", joined)
                    if "beta" not in version:
                        self.assertIn("Deferred rows are only allowed for a beta prerelease", joined)

    def test_case_insensitive_deferred_row_still_requires_evidence(self) -> None:
        repository = _narrow_repository(self.root)
        text = _narrow_wrap().replace(
            f"| Linux QA restore gate | yes | {NARROW_DEFERRED_EVIDENCE}",
            "| linux qa restore gate | yes | no target",
        )
        joined = "\n".join(self._messages(text, repository=repository))
        self.assertIn("Deferred evidence must name the next rc/final full run", joined)

    def test_git_replacement_cannot_hide_backend_change(self) -> None:
        repository = _narrow_repository(self.root)
        _write(repository, "app/main.py", "VALUE = 999\n")
        _commit(repository, "backend change")
        replacement = _git(repository, "commit-tree", "HEAD^{tree}", "-m", "replacement").strip()
        _git(repository, "replace", "v1.0.0-beta.1", replacement)
        self.assertEqual(_git(repository, "diff", "--name-only", "v1.0.0-beta.1", "HEAD"), "")
        environment = {key: value for key, value in os.environ.items() if key != "GIT_NO_REPLACE_OBJECTS"}
        with mock.patch.dict(os.environ, environment, clear=True):
            joined = "\n".join(self._messages(_narrow_wrap(), repository=repository))
        self.assertIn("app/main.py", joined)

    def test_release_qa_tools_keep_the_beta_narrow(self) -> None:
        required_tools = {
            "scripts/validate_release_wrap.py", "scripts/check_release_changelog_coverage.py",
            "scripts/dev_check.py", "scripts/run_test_shard.py",
            "scripts/check_public_docs.py", "scripts/check_public_screenshots.py",
            "scripts/check_public_demo_artifact.py", "scripts/verify_wiki_drift.py",
        }
        self.assertTrue(required_tools <= NARROW_BETA_QA_TOOLING)
        repository = _narrow_repository(self.root, extra_changes={path: "# QA only\n" for path in required_tools})
        self.assertEqual(self._messages(_narrow_wrap(), repository=repository), [])

    def _assert_qa_tools_not_copied(self, dockerfile: str) -> list[str]:
        sources = []
        for line in dockerfile.replace("\\\n", " ").splitlines():
            words = shlex.split(line, comments=True)
            if not words or words[0].upper() not in {"COPY", "ADD"}:
                continue
            # Fail closed if Dockerfile stops using simple literal local sources.
            self.assertEqual(len(words), 3, line)
            source = str(PurePosixPath(words[1]))
            self.assertNotRegex(source, r"[\[\]{}*$?]|^[-/]|\.\.")
            self.assertTrue((REPOSITORY_ROOT / source).exists(), source)
            sources.append(source)
            for tool in NARROW_BETA_QA_TOOLING:
                self.assertFalse(source == "." or tool == source or tool.startswith(source + "/"), (source, tool))
        self.assertTrue(sources)
        return sources

    def test_narrow_qa_tools_are_not_copied_into_the_image(self) -> None:
        sources = self._assert_qa_tools_not_copied((REPOSITORY_ROOT / "Dockerfile").read_text())
        self.assertTrue(all((REPOSITORY_ROOT / tool).is_file() for tool in NARROW_BETA_QA_TOOLING))
        repository = _narrow_repository(self.root)
        for source in sources:
            if source.startswith("scripts/"):
                _write(repository, source, "# runtime change\n")
                _commit(repository, "runtime script")
                self.assertIn(source, "\n".join(self._messages(_narrow_wrap(), repository=repository)))

    def test_image_copy_guard_rejects_qa_file_and_parent_sources(self) -> None:
        for instruction in ("COPY", "ADD"):
            for source in ("scripts/validate_release_wrap.py", "scripts/", "."):
                with self.subTest(instruction=instruction, source=source), self.assertRaises(AssertionError):
                    self._assert_qa_tools_not_copied(f"{instruction} {source} /app/\n")

    def test_deferred_required_gate_is_rejected(self) -> None:
        repository = _narrow_repository(self.root)
        for gate in (
            "Docker build and health gates",
            "Full Playwright/browser gates",
            "Feature-specific live API/UI gates",
            "Snapshot/export/offline artifact gate",
            "Deployment refresh/sniff tests",
        ):
            with self.subTest(gate=gate):
                messages = self._messages(
                    _narrow_wrap(deferred=DEFERRABLE_NARROW_BETA_GATES + (gate,)),
                    repository=repository,
                )
                self.assertIn(f"{gate}: Deferred is not allowed for this gate", "\n".join(messages))

    def test_deferred_on_a_final_or_rc_version_is_rejected(self) -> None:
        repository = _narrow_repository(self.root)
        for version in ("1.0.0", "1.0.0-rc.1"):
            with self.subTest(version=version):
                joined = "\n".join(
                    self._messages(_narrow_wrap(), repository=repository, version=version)
                )
                self.assertIn("Deferred rows are only allowed for a beta prerelease", joined)

    def test_deferred_without_narrow_mode_is_rejected(self) -> None:
        repository = _narrow_repository(self.root)

        joined = "\n".join(
            self._messages(_narrow_wrap(mode_lines=()), repository=repository)
        )

        self.assertIn("Deferred rows require 'Release mode: narrow beta'", joined)

    def test_narrow_mode_requires_a_previous_release_tag(self) -> None:
        repository = _narrow_repository(self.root)

        joined = "\n".join(
            self._messages(
                _narrow_wrap(mode_lines=("Release mode: narrow beta",)),
                repository=repository,
            )
        )

        self.assertIn("Previous release tag", joined)

    def test_unknown_release_mode_is_rejected(self) -> None:
        joined = "\n".join(
            self._messages(_narrow_wrap(deferred=(), mode_lines=("Release mode: tiny beta",)))
        )

        self.assertIn("Release mode must be", joined)

    def test_deferred_evidence_must_name_the_next_rc_or_final_run(self) -> None:
        repository = _narrow_repository(self.root)

        joined = "\n".join(
            self._messages(_narrow_wrap(evidence="skipped for time"), repository=repository)
        )

        self.assertIn(
            "Optional-sidecar runtime matrix: Deferred evidence must name the next rc/final full run",
            joined,
        )

    def test_missing_previous_tag_fails_closed(self) -> None:
        repository = _narrow_repository(self.root)
        _git(repository, "tag", "-d", "v1.0.0-beta.1")

        joined = "\n".join(self._messages(_narrow_wrap(), repository=repository))

        self.assertIn("v1.0.0-beta.1", joined)
        self.assertIn("not eligible for a narrow beta", joined)

    def test_previous_tag_must_be_the_latest_earlier_release(self) -> None:
        repository = _narrow_repository(self.root)
        # A newer earlier release exists, so declaring beta.1 skips a release
        # boundary; declaring the candidate itself would compare against nothing.
        _git(repository, "tag", "v1.0.0-beta.1.1", "HEAD~1")
        _git(repository, "tag", "v1.0.0-beta.2", "HEAD")
        for declared, expected in (
            ("v1.0.0-beta.1", "previous release tag is v1.0.0-beta.1.1"),
            ("v1.0.0-beta.2", "must be earlier than the release version"),
        ):
            with self.subTest(declared=declared):
                joined = "\n".join(
                    self._messages(
                        _narrow_wrap(
                            mode_lines=("Release mode: narrow beta", f"Previous release tag: `{declared}`")
                        ),
                        repository=repository,
                    )
                )
                self.assertIn(expected, joined)

    def test_git_failure_fails_closed(self) -> None:
        not_a_repository = self.root / "plain"
        not_a_repository.mkdir()

        joined = "\n".join(self._messages(_narrow_wrap(), repository=not_a_repository))
        self.assertIn("not eligible for a narrow beta", joined)

        repository = _narrow_repository(self.root)
        with mock.patch(
            "scripts.validate_release_wrap.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ):
            joined = "\n".join(self._messages(_narrow_wrap(), repository=repository))
        self.assertIn("not eligible for a narrow beta", joined)

        joined = "\n".join(self._messages(_narrow_wrap(), repository=None))
        self.assertIn("not eligible for a narrow beta", joined)

    def test_final_phase_requires_the_candidate_tag(self) -> None:
        repository = _narrow_repository(self.root)

        joined = "\n".join(self._messages(_narrow_wrap(), repository=repository, phase="final"))

        self.assertIn("v1.0.0-beta.2", joined)
        self.assertIn("not eligible for a narrow beta", joined)

    def test_unknown_result_is_still_rejected(self) -> None:
        text = _wrap_with_rows({"Linux QA restore gate": ("yes", "evidence", "Skipped", "")})

        self.assertIn(
            "Linux QA restore gate: Result must be Pass, Blocked, N/A, or Deferred",
            self._messages(text),
        )

    def test_cli_validates_an_eligible_narrow_beta(self) -> None:
        repository = _narrow_repository(self.root, previous="99.0.0-beta.1", candidate="99.0.0-beta.2")
        text = _narrow_wrap(
            mode_lines=("Release mode: narrow beta", "Previous release tag: `v99.0.0-beta.1`")
        ) + "\n| Docs/wiki/public-demo publication | yes | Pending owner publication: external wiki | Blocked |  |\n"
        _write(repository, "docs/RELEASE_WRAP_99.0.0-beta.2.md", text)
        _commit(repository, "wrap")

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "validate_release_wrap.py"),
                    "99.0.0-beta.2",
                    "--phase",
                    "pre-tag",
                    "--repository",
                    str(repository),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        eligible = run()
        self.assertEqual(eligible.returncode, 0, eligible.stdout + eligible.stderr)

        _write(repository, "app/main.py", "VALUE = 3\n")
        _commit(repository, "backend change")
        ineligible = run()
        self.assertNotEqual(ineligible.returncode, 0)
        self.assertIn("app/main.py", ineligible.stdout)

    def _assert_pre_mode_release_wraps_unchanged(self, repository: Path) -> set[Path]:
        wraps = sorted((repository / "docs").glob("RELEASE_WRAP_*.md")) + sorted(
            (repository / "docs" / "archive").glob("RELEASE_WRAP_*.md")
        )
        compared: set[Path] = set()
        declared_modes: set[Path] = set()
        for path in wraps:
            version = path.stem.removeprefix("RELEASE_WRAP_")
            text = path.read_text(encoding="utf-8")
            if RELEASE_MODE_LINE.search(text):
                # Mode-specific behavior is covered by the dedicated narrow tests.
                declared_modes.add(path)
                continue
            for phase in ("pre-tag", "final"):
                with self.subTest(version=version, phase=phase):
                    baseline = validate_release_wrap_text(
                        text,
                        phase=phase,
                        require_changelog_coverage=changelog_coverage_required(version),
                    )
                    with_release_context = validate_release_wrap_text(
                        text,
                        phase=phase,
                        require_changelog_coverage=changelog_coverage_required(version),
                        version=version,
                        repository=repository,
                    )
                    self.assertEqual(with_release_context, baseline)
                    self.assertFalse(
                        [issue for issue in baseline if "eferred" in issue.message or "narrow" in issue.message]
                    )
                    compared.add(path)
        self.assertEqual(compared, set(wraps) - declared_modes)
        self.assertTrue(compared)
        return compared

    def test_existing_release_wrap_comparison_skips_declared_modes(self) -> None:
        legacy_wraps = {
            self.root / "docs" / "RELEASE_WRAP_1.0.0.md",
            self.root / "docs" / "archive" / "RELEASE_WRAP_0.9.0.md",
        }
        for path in legacy_wraps:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_wrap_with_rows({}), encoding="utf-8")
        for index, mode in enumerate(("Release mode: narrow beta", "rElEaSe MoDe:\t`narrow beta`"), 1):
            path = self.root / "docs" / f"RELEASE_WRAP_1.1.0-beta.{index}.md"
            path.write_text(_narrow_wrap(mode_lines=(mode,)), encoding="utf-8")

        wraps = set((self.root / "docs").glob("RELEASE_WRAP_*.md")) | set(
            (self.root / "docs" / "archive").glob("RELEASE_WRAP_*.md")
        )
        declared_modes = {
            path for path in wraps if RELEASE_MODE_LINE.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(len(declared_modes), 2)
        compared = self._assert_pre_mode_release_wraps_unchanged(self.root)

        self.assertEqual(compared, wraps - declared_modes)
        self.assertEqual(compared, legacy_wraps)
        self.assertTrue(compared)

    def test_existing_release_wraps_are_unchanged_by_narrow_beta_rules(self) -> None:
        self._assert_pre_mode_release_wraps_unchanged(REPOSITORY_ROOT)
        for version in ("0.23.0", *(f"0.24.0-beta.{n}" for n in range(1, 7))):
            with self.subTest(version=version, phase="pre-tag clean"):
                text = release_wrap_path(REPOSITORY_ROOT, version).read_text(encoding="utf-8")
                self.assertEqual(validate_release_wrap_text(text, phase="pre-tag", version=version), [])


if __name__ == "__main__":
    unittest.main()
