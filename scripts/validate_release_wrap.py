from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


REQUIRED_GATES = (
    "Scope and branch",
    "Python unit and syntax gates",
    "JavaScript syntax gates",
    "Docker build and health gates",
    "Optional-sidecar runtime matrix",
    "Full Playwright/browser gates",
    "Feature-specific live API/UI gates",
    "Local release perf harnesses",
    "Linux QA restore gate",
    "Restored Linux QA perf harnesses",
    "Snapshot/export/offline artifact gate",
    "Docs/wiki/public-demo gate",
    "GHCR publish verification",
    "Deployment refresh/sniff tests",
    "Post-release reopen",
)

VALID_RESULTS = {"pass", "blocked", "n/a"}


@dataclass(frozen=True)
class ValidationIssue:
    message: str


def _split_markdown_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell.replace(" ", "")) <= {"-", ":"} for cell in cells)


def parse_checklist_evidence_table(text: str) -> dict[str, list[str]]:
    """Return release-wrap checklist evidence rows keyed by lower-case gate."""

    lines = text.splitlines()
    header_index: int | None = None
    for index, line in enumerate(lines):
        cells = _split_markdown_row(line)
        if cells == ["Gate", "Required", "Evidence", "Result", "N/A Reason"]:
            header_index = index
            break

    if header_index is None:
        return {}

    rows: dict[str, list[str]] = {}
    for line in lines[header_index + 1 :]:
        cells = _split_markdown_row(line)
        if cells is None:
            if rows:
                break
            continue
        if _is_separator_row(cells):
            continue
        if len(cells) != 5:
            continue
        gate = cells[0].strip()
        if gate:
            rows[gate.lower()] = cells
    return rows


def validate_release_wrap_text(text: str, *, allow_blocked: bool = False) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if "docs/RELEASE_CHECKLIST.md" not in text:
        issues.append(ValidationIssue("release wrap must reference docs/RELEASE_CHECKLIST.md"))

    rows = parse_checklist_evidence_table(text)
    if not rows:
        issues.append(ValidationIssue("release wrap is missing the checklist evidence table"))
        return issues

    for gate in REQUIRED_GATES:
        row = rows.get(gate.lower())
        if row is None:
            issues.append(ValidationIssue(f"missing checklist evidence row: {gate}"))
            continue

        required, evidence, result, reason = row[1], row[2], row[3], row[4]
        if required.lower() not in {"yes", "no"}:
            issues.append(ValidationIssue(f"{gate}: Required must be yes or no"))

        normalized_result = result.lower()
        if normalized_result not in VALID_RESULTS:
            issues.append(ValidationIssue(f"{gate}: Result must be Pass, Blocked, or N/A"))
            continue

        if normalized_result == "pass" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Pass requires evidence"))
        if normalized_result == "blocked" and not allow_blocked:
            issues.append(ValidationIssue(f"{gate}: Blocked gates cannot ship"))
        if normalized_result == "blocked" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Blocked requires evidence"))
        if normalized_result == "n/a" and reason.lower() in {"", "-", "n/a", "none", "reason"}:
            issues.append(ValidationIssue(f"{gate}: N/A requires a concrete reason"))

    return issues


def validate_release_wrap_path(path: Path, *, allow_blocked: bool = False) -> list[ValidationIssue]:
    if not path.exists():
        return [ValidationIssue(f"release wrap not found: {path}")]
    return validate_release_wrap_text(path.read_text(encoding="utf-8"), allow_blocked=allow_blocked)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate release-wrap checklist evidence.")
    parser.add_argument("version", help="Release version, for example 0.20.2 or v0.20.2.")
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Report missing/invalid evidence but do not fail solely because a row is Blocked.",
    )
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    path = Path("docs") / f"RELEASE_WRAP_{version}.md"
    issues = validate_release_wrap_path(path, allow_blocked=args.allow_blocked)
    if issues:
        for issue in issues:
            print(f"- {issue.message}")
        return 1
    print(f"{path} checklist evidence is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
