from __future__ import annotations

import unittest

from scripts.validate_release_wrap import REQUIRED_GATES, validate_release_wrap_text


def _wrap_with_rows(rows: dict[str, tuple[str, str, str, str]]) -> str:
    lines = [
        "# Release Wrap - v0.20.2",
        "",
        "Validated against `docs/RELEASE_CHECKLIST.md`.",
        "",
        "| Gate | Required | Evidence | Result | N/A Reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in REQUIRED_GATES:
        required, evidence, result, reason = rows.get(gate, ("yes", "evidence", "Pass", ""))
        lines.append(f"| {gate} | {required} | {evidence} | {result} | {reason} |")
    return "\n".join(lines)


class ReleaseWrapValidatorTests(unittest.TestCase):
    def test_accepts_complete_release_wrap_evidence_table(self) -> None:
        issues = validate_release_wrap_text(_wrap_with_rows({}))

        self.assertEqual(issues, [])

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


if __name__ == "__main__":
    unittest.main()
