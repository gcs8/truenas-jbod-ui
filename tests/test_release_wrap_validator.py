from __future__ import annotations

import unittest
from pathlib import Path

from scripts.validate_release_wrap import (
    REQUIRED_GATES,
    changelog_coverage_required,
    validate_release_wrap_text,
)


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
    return "\n".join(lines)


class ReleaseWrapValidatorTests(unittest.TestCase):
    def test_v0222_final_release_wrap_is_complete(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")

        # v0.22.2 predates the changelog coverage gate; main() grandfathers it.
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
        text = (repository / "docs" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")

        self.assertIn("Post-release reconciliation", text)
        self.assertIn("PR #121 merged", text)
        self.assertIn("Issue #124 closed", text)
        self.assertNotIn("PR #121 remains draft", text)

    def test_v0222_deployment_gate_records_private_receipt_validation(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "RELEASE_WRAP_0.22.2.md").read_text(encoding="utf-8")
        deployment_row = next(
            line for line in text.splitlines() if line.startswith("| Deployment refresh/sniff tests |")
        )

        self.assertIn("private deployment receipt was validated", deployment_row)
        self.assertIn("runtime convergence was reverified", deployment_row)
        self.assertIn("Private deployment identifiers are not retained", deployment_row)

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

    def test_wiki_change_requires_external_wiki_commit_line(self) -> None:
        text = _wrap_with_rows({})

        issues = validate_release_wrap_text(text, wiki_changed=True)

        self.assertIn(
            "wiki/ changed since the previous tag; release wrap must record "
            "'External wiki commit: <sha>' for the published GitHub wiki",
            [issue.message for issue in issues],
        )
        self.assertEqual(validate_release_wrap_text(text, wiki_changed=False), [])
        recorded = text + "\nExternal wiki commit: 0123456789abcdef0123456789abcdef01234567\n"
        self.assertEqual(validate_release_wrap_text(recorded, wiki_changed=True), [])

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


if __name__ == "__main__":
    unittest.main()
