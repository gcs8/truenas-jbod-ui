from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

if __package__:
    from scripts.verify_wiki_drift import (
        WikiVerificationError,
        WikiVerificationResult,
        verify_wiki_drift,
    )
else:
    from verify_wiki_drift import (  # type: ignore[no-redef]
        WikiVerificationError,
        WikiVerificationResult,
        verify_wiki_drift,
    )


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
    "docs/wiki/public-demo gate",
    "ghcr publish verification",
    "deployment refresh/sniff tests",
    "post-release reopen",
}

VALID_RESULTS = {"pass", "blocked", "n/a"}
WIKI_DRIFT_REQUIRED_FROM = (0, 22, 3)
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


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
    require_wiki_verification: bool = False,
    wiki_verification: WikiVerificationResult | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    normalized_phase = phase.lower()
    if normalized_phase not in {"pre-tag", "final"}:
        issues.append(ValidationIssue("phase must be pre-tag or final"))

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
            pre_tag_post_publish = normalized_phase == "pre-tag" and gate.lower() in POST_PUBLISH_GATES
            if not pre_tag_post_publish:
                issues.append(ValidationIssue(f"{gate}: Blocked gates cannot ship"))
        if normalized_result == "blocked" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Blocked requires evidence"))
        if normalized_result == "n/a" and reason.lower() in {"", "-", "n/a", "none", "reason"}:
            issues.append(ValidationIssue(f"{gate}: N/A requires a concrete reason"))

    wiki_row = rows.get("docs/wiki/public-demo gate")
    wiki_is_deferred = bool(
        wiki_row
        and normalized_phase == "pre-tag"
        and wiki_row[3].lower() == "blocked"
    )
    if require_wiki_verification and not wiki_is_deferred:
        if wiki_row is not None and wiki_row[3].lower() != "pass":
            issues.append(
                ValidationIssue(
                    "Docs/wiki/public-demo gate: Result must be Pass after wiki publication"
                )
            )
        if wiki_verification is None:
            issues.append(
                ValidationIssue("Docs/wiki/public-demo gate: wiki drift verification was not run")
            )
        elif not wiki_verification.matches:
            issues.append(
                ValidationIssue(
                    "Docs/wiki/public-demo gate: external wiki differs from repository wiki/"
                )
            )
        elif wiki_row is not None and wiki_verification.release_evidence() not in wiki_row[2]:
            issues.append(
                ValidationIssue(
                    "Docs/wiki/public-demo gate: evidence does not contain the exact wiki "
                    "verification receipt"
                )
            )

    return issues


def validate_release_wrap_path(
    path: Path,
    *,
    allow_blocked: bool = False,
    phase: str = "final",
    require_wiki_verification: bool = False,
    wiki_verification: WikiVerificationResult | None = None,
) -> list[ValidationIssue]:
    if not path.exists():
        return [ValidationIssue(f"release wrap not found: {path}")]
    return validate_release_wrap_text(
        path.read_text(encoding="utf-8"),
        allow_blocked=allow_blocked,
        phase=phase,
        require_wiki_verification=require_wiki_verification,
        wiki_verification=wiki_verification,
    )


def wiki_drift_required(version: str) -> bool:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError("version must use semantic version form X.Y.Z")
    return tuple(int(part) for part in match.groups()) >= WIKI_DRIFT_REQUIRED_FROM


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate release-wrap checklist evidence.")
    parser.add_argument("version", help="Release version, for example 0.20.2 or v0.20.2.")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--repository-commit")
    parser.add_argument("--wiki-source")
    parser.add_argument("--external-wiki-commit")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    version = args.version.removeprefix("v")
    try:
        require_wiki_verification = wiki_drift_required(version)
    except ValueError as exc:
        print(f"- {exc}")
        return 1
    path = args.repository / "docs" / f"RELEASE_WRAP_{version}.md"
    wiki_verification = None
    rows = {}
    if path.exists():
        rows = parse_checklist_evidence_table(path.read_text(encoding="utf-8"))
    wiki_row = rows.get("docs/wiki/public-demo gate")
    wiki_is_deferred = bool(
        wiki_row
        and args.phase == "pre-tag"
        and wiki_row[3].lower() == "blocked"
    )
    if require_wiki_verification and not wiki_is_deferred:
        required_arguments = {
            "--repository-commit": args.repository_commit,
            "--wiki-source": args.wiki_source,
            "--external-wiki-commit": args.external_wiki_commit,
        }
        missing_arguments = [name for name, value in required_arguments.items() if not value]
        if missing_arguments:
            print(f"- wiki verification requires {', '.join(missing_arguments)}")
            return 1
        try:
            wiki_verification = verify_wiki_drift(
                repository=args.repository,
                repository_commit=args.repository_commit,
                wiki_source=args.wiki_source,
                external_wiki_commit=args.external_wiki_commit,
            )
        except (OSError, UnicodeError, WikiVerificationError) as exc:
            print(f"- Docs/wiki/public-demo gate: wiki verification failed: {exc}")
            return 1
    issues = validate_release_wrap_path(
        path,
        allow_blocked=args.allow_blocked,
        phase=args.phase,
        require_wiki_verification=require_wiki_verification,
        wiki_verification=wiki_verification,
    )
    if issues:
        for issue in issues:
            print(f"- {issue.message}")
        return 1
    print(f"{path} checklist evidence is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
