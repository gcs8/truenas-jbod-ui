"""Release-wrap evidence rows: duplicates, malformed rows and forbidden N/A reasons.

A later row for the same gate must not overwrite an earlier one, a row the
parser cannot read must be reported instead of skipped, and the reason the
release checklist forbids for N/A ("Not enough time") cannot satisfy a gate.
All inputs are synthetic text; nothing runs a real gate.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts.validate_release_wrap import REQUIRED_GATES, main, validate_release_wrap_text

PYTHON_GATE = "Python unit and syntax gates"
HEADER = (
    "# Release Wrap - v0.20.2",
    "",
    "Validated against `docs/RELEASE_CHECKLIST.md`.",
    "",
    "Changelog coverage: pass (3 PRs)",
    "",
    "| Gate | Required | Evidence | Result | N/A Reason |",
    "| --- | --- | --- | --- | --- |",
)


def _row(gate: str, evidence: str = "synthetic receipt", result: str = "Pass", reason: str = "") -> str:
    return f"| {gate} | yes | {evidence} | {result} | {reason} |"


def _wrap(*, overrides: dict[str, str] | None = None, extra: tuple[str, ...] = ()) -> str:
    overrides = overrides or {}
    rows = [overrides.get(gate, _row(gate)) for gate in REQUIRED_GATES]
    return "\n".join((*HEADER, *rows, *extra)) + "\n"


def _messages(text: str, *, allow_blocked: bool = False) -> list[str]:
    return [issue.message for issue in validate_release_wrap_text(text, allow_blocked=allow_blocked)]


BLOCKED_PYTHON = _row(PYTHON_GATE, evidence="failed native test run", result="Blocked")


class DuplicateGateRowTests(unittest.TestCase):
    def test_distinct_rows_validate_cleanly(self) -> None:
        self.assertEqual(_messages(_wrap()), [])

    def test_blocked_row_alone_reports_blocked_gate(self) -> None:
        self.assertEqual(
            _messages(_wrap(overrides={PYTHON_GATE: BLOCKED_PYTHON})),
            [f"{PYTHON_GATE}: Blocked gates cannot ship"],
        )

    def test_later_pass_row_cannot_hide_earlier_blocked_row(self) -> None:
        text = _wrap(
            overrides={PYTHON_GATE: BLOCKED_PYTHON},
            extra=(_row(PYTHON_GATE, evidence="later summary"),),
        )
        messages = _messages(text)
        self.assertIn(f"duplicate checklist evidence row: {PYTHON_GATE}", messages)
        self.assertIn(f"{PYTHON_GATE}: Blocked gates cannot ship", messages)

    def test_case_variant_duplicate_is_rejected(self) -> None:
        text = _wrap(
            overrides={PYTHON_GATE: BLOCKED_PYTHON},
            extra=(_row("python UNIT and Syntax gates", evidence="later summary"),),
        )
        self.assertIn(
            "duplicate checklist evidence row: python UNIT and Syntax gates",
            _messages(text),
        )

    def test_duplicate_is_rejected_even_when_blocked_rows_are_allowed(self) -> None:
        text = _wrap(
            overrides={PYTHON_GATE: BLOCKED_PYTHON},
            extra=(_row(PYTHON_GATE, evidence="later summary"),),
        )
        self.assertEqual(
            _messages(text, allow_blocked=True),
            [f"duplicate checklist evidence row: {PYTHON_GATE}"],
        )

    def test_two_pass_rows_for_one_gate_are_still_a_duplicate(self) -> None:
        text = _wrap(extra=(_row(PYTHON_GATE, evidence="second receipt"),))
        self.assertEqual(_messages(text), [f"duplicate checklist evidence row: {PYTHON_GATE}"])

    def test_duplicate_release_specific_row_is_rejected(self) -> None:
        text = _wrap(
            extra=(
                _row("Release-specific check", result="Deferred", evidence="none"),
                _row("Release-specific check"),
            )
        )
        self.assertIn("duplicate checklist evidence row: Release-specific check", _messages(text))


class MalformedEvidenceRowTests(unittest.TestCase):
    def test_row_with_wrong_cell_count_is_reported(self) -> None:
        text = _wrap(
            overrides={PYTHON_GATE: f"| {PYTHON_GATE} | yes | failed run | Blocked |"},
        )
        messages = _messages(text)
        self.assertIn(
            "malformed checklist evidence row 10: expected 5 cells, found 4",
            messages,
        )
        # The unreadable row is not silently treated as if the gate were absent only.
        self.assertIn(f"missing checklist evidence row: {PYTHON_GATE}", messages)

    def test_row_with_extra_cell_is_reported(self) -> None:
        text = _wrap(extra=(f"| {PYTHON_GATE} | yes | a | b | Pass | |",))
        self.assertIn(
            f"malformed checklist evidence row {len(HEADER) + len(REQUIRED_GATES) + 1}: "
            "expected 5 cells, found 6",
            _messages(text),
        )

    def test_row_without_gate_name_is_reported(self) -> None:
        text = _wrap(extra=("|  | yes | synthetic receipt | Blocked | |",))
        self.assertIn(
            f"malformed checklist evidence row {len(HEADER) + len(REQUIRED_GATES) + 1}: gate name is empty",
            _messages(text),
        )


class ForbiddenNotApplicableReasonTests(unittest.TestCase):
    def test_not_enough_time_cannot_justify_na(self) -> None:
        for reason in ("Not enough time", "not enough time.", "NOT  ENOUGH TIME", "Not enough time to run it"):
            with self.subTest(reason=reason):
                text = _wrap(overrides={PYTHON_GATE: _row(PYTHON_GATE, "", "N/A", reason)})
                self.assertEqual(
                    _messages(text),
                    [f"{PYTHON_GATE}: N/A reason 'Not enough time' is not allowed; stop the release instead"],
                )

    def test_every_mandatory_gate_na_for_lack_of_time_fails(self) -> None:
        text = _wrap(
            overrides={gate: _row(gate, "", "N/A", "Not enough time") for gate in REQUIRED_GATES}
        )
        messages = _messages(text)
        self.assertEqual(len(messages), len(REQUIRED_GATES))
        for gate in REQUIRED_GATES:
            self.assertIn(
                f"{gate}: N/A reason 'Not enough time' is not allowed; stop the release instead",
                messages,
            )

    def test_concrete_na_reason_still_passes(self) -> None:
        text = _wrap(
            overrides={
                PYTHON_GATE: _row(PYTHON_GATE, "", "N/A", "No Python source changed since the last tag"),
                "Local release perf harnesses": _row(
                    "Local release perf harnesses", "", "N/A", "Time-series storage untouched; enough time left"
                ),
            }
        )
        self.assertEqual(_messages(text), [])


class CommandLineResultTests(unittest.TestCase):
    def _run(self, text: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / "docs").mkdir()
            (repository / "docs" / "RELEASE_WRAP_0.20.2.md").write_text(text, encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["0.20.2", "--repository", str(repository)])
        return code, output.getvalue()

    def test_clean_wrap_reaches_complete(self) -> None:
        code, output = self._run(_wrap())
        self.assertEqual(code, 0)
        self.assertIn("checklist evidence is complete", output)

    def test_duplicate_row_never_reaches_complete(self) -> None:
        code, output = self._run(
            _wrap(
                overrides={PYTHON_GATE: BLOCKED_PYTHON},
                extra=(_row(PYTHON_GATE, evidence="later summary"),),
            )
        )
        self.assertEqual(code, 1)
        self.assertIn(f"- duplicate checklist evidence row: {PYTHON_GATE}", output)
        self.assertNotIn("checklist evidence is complete", output)


if __name__ == "__main__":
    unittest.main()
