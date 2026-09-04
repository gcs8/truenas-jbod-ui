from __future__ import annotations

import argparse
import re
import subprocess
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

POST_PUBLISH_GATES = {
    "ghcr publish verification",
    "deployment refresh/sniff tests",
    "post-release reopen",
}

VALID_RESULTS = {"pass", "blocked", "n/a"}

# Releases from this version on must record the changelog coverage gate
# (scripts/check_release_changelog_coverage.py). Earlier wraps predate it.
CHANGELOG_COVERAGE_REQUIRED_FROM = (0, 22, 3)
CHANGELOG_COVERAGE_LINE = re.compile(r"Changelog coverage: pass \(\d+ PRs\)")
EXTERNAL_WIKI_COMMIT_LINE = re.compile(r"External wiki commit: [0-9a-f]{7,40}\b")


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


def validate_release_wrap_text(
    text: str,
    *,
    allow_blocked: bool = False,
    phase: str = "final",
    require_changelog_coverage: bool = True,
    wiki_changed: bool = False,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    normalized_phase = phase.lower()
    if normalized_phase not in {"pre-tag", "final"}:
        issues.append(ValidationIssue("phase must be pre-tag or final"))

    if "docs/RELEASE_CHECKLIST.md" not in text:
        issues.append(ValidationIssue("release wrap must reference docs/RELEASE_CHECKLIST.md"))

    if require_changelog_coverage and CHANGELOG_COVERAGE_LINE.search(text) is None:
        issues.append(
            ValidationIssue(
                "release wrap must record the changelog coverage gate as "
                "'Changelog coverage: pass (<N> PRs)' from scripts/check_release_changelog_coverage.py"
            )
        )
    if wiki_changed and EXTERNAL_WIKI_COMMIT_LINE.search(text) is None:
        issues.append(
            ValidationIssue(
                "wiki/ changed since the previous tag; release wrap must record "
                "'External wiki commit: <sha>' for the published GitHub wiki"
            )
        )

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
            pre_tag_post_publish = normalized_phase == "pre-tag" and gate.lower() in POST_PUBLISH_GATES
            if not pre_tag_post_publish:
                issues.append(ValidationIssue(f"{gate}: Blocked gates cannot ship"))
        if normalized_result == "blocked" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Blocked requires evidence"))
        if normalized_result == "n/a" and reason.lower() in {"", "-", "n/a", "none", "reason"}:
            issues.append(ValidationIssue(f"{gate}: N/A requires a concrete reason"))

    return issues


def validate_release_wrap_path(
    path: Path,
    *,
    allow_blocked: bool = False,
    phase: str = "final",
    require_changelog_coverage: bool = True,
    wiki_changed: bool = False,
) -> list[ValidationIssue]:
    if not path.exists():
        return [ValidationIssue(f"release wrap not found: {path}")]
    return validate_release_wrap_text(
        path.read_text(encoding="utf-8"),
        allow_blocked=allow_blocked,
        phase=phase,
        require_changelog_coverage=require_changelog_coverage,
        wiki_changed=wiki_changed,
    )


def parse_version_tuple(version: str) -> tuple[int, ...]:
    core = version.removeprefix("v").split("-", 1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def changelog_coverage_required(version: str) -> bool:
    return parse_version_tuple(version) >= CHANGELOG_COVERAGE_REQUIRED_FROM


def wiki_changed_since(previous_tag: str) -> bool:
    completed = subprocess.run(
        ["git", "diff", "--quiet", f"{previous_tag}..HEAD", "--", "wiki/"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode in {0, 1}:
        return completed.returncode == 1
    raise SystemExit(f"git diff --quiet {previous_tag}..HEAD -- wiki/ failed: {completed.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate release-wrap checklist evidence.")
    parser.add_argument("version", help="Release version, for example 0.20.2 or v0.20.2.")
    parser.add_argument(
        "--allow-blocked",
        action="store_true",
        help="Report missing/invalid evidence but do not fail solely because a row is Blocked.",
    )
    parser.add_argument(
        "--phase",
        choices=("pre-tag", "final"),
        default="final",
        help=(
            "Use pre-tag to allow only inherently post-publish rows to remain Blocked. "
            "Use final after GHCR, deployment sniff tests, and reopen work are recorded."
        ),
    )
    parser.add_argument(
        "--previous-tag",
        help=(
            "Previous release tag. When wiki/ changed between it and HEAD, the wrap must "
            "record 'External wiki commit: <sha>'."
        ),
    )
    parser.add_argument(
        "--wiki-changed",
        action="store_true",
        help="Require the external wiki commit line without inspecting git.",
    )
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    path = Path("docs") / f"RELEASE_WRAP_{version}.md"
    wiki_changed = args.wiki_changed or (
        args.previous_tag is not None and wiki_changed_since(args.previous_tag)
    )
    issues = validate_release_wrap_path(
        path,
        allow_blocked=args.allow_blocked,
        phase=args.phase,
        require_changelog_coverage=changelog_coverage_required(version),
        wiki_changed=wiki_changed,
    )
    if issues:
        for issue in issues:
            print(f"- {issue.message}")
        return 1
    print(f"{path} checklist evidence is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
