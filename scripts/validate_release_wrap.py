from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
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

DOCS_PUBLICATION_GATE = "Docs/wiki/public-demo publication"
POST_PUBLISH_GATES = {
    DOCS_PUBLICATION_GATE.lower(),
    "ghcr publish verification",
    "deployment refresh/sniff tests",
    "post-release reopen",
}

OWNER_PUBLICATION_TARGETS = {"external wiki", "public demo"}
VALID_RESULTS = {"pass", "blocked", "n/a", "deferred"}
WIKI_DRIFT_REQUIRED_FROM = (0, 22, 3)
CHANGELOG_COVERAGE_REQUIRED_FROM = (0, 22, 3)
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
# Releases from this version on must ship a public demo rebuilt from the
# release source. Earlier releases predate the release-time rebuild rule.
PUBLIC_DEMO_FRESHNESS_REQUIRED_FROM = (0, 23, 1)
PUBLIC_DEMO_GATE = "Docs/wiki/public-demo gate"
PUBLIC_DEMO_VERSION_RE = re.compile(r'id="snapshot-app-version">v([0-9A-Za-z][0-9A-Za-z.+-]*)<')
CHANGELOG_COVERAGE_LINE = re.compile(r"^Changelog coverage: pass \(\d+ PRs\)$", re.MULTILINE)

# Narrow beta policy (docs/RELEASE_CHECKLIST.md, "Narrow beta"). A beta whose
# whole source delta since the previous release tag is front-end assets, tests,
# QA tooling or docs may record these long gates as Deferred. Deferred is never
# a Pass: the next rc or final release runs every one of them. Every other row
# keeps today's rules, and rc and final versions can never use Deferred.
DEFERRABLE_NARROW_BETA_GATES = (
    "Optional-sidecar runtime matrix",
    "Local release perf harnesses",
    "Linux QA restore gate",
    "Restored Linux QA perf harnesses",
)
NARROW_BETA_MODE = "narrow beta"
FULL_RELEASE_MODE = "full"
RELEASE_MODE_LINE = re.compile(r"^Release mode:[ \t]*(.*?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
PREVIOUS_TAG_LINE = re.compile(r"^Previous release tag:[ \t]*(.*?)[ \t]*$", re.MULTILINE | re.IGNORECASE)
RELEASE_TAG_RE = re.compile(r"v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")
DEFERRAL_TARGET_RE = re.compile(r"\bdeferred to (?:the )?next (?:rc/final|rc or final)(?: full)? run\b", re.IGNORECASE)
NARROW_BETA_PATH_PREFIXES = (
    "app/static/",
    "app/templates/",
    "tests/",
    "qa/",
    "docs/",
    "wiki/",
)
# Release/QA-only tooling reviewed against Dockerfile COPY/ADD sources and the
# release checklist. Tests fail closed if a tool or its parent enters the image.
# Do not replace this explicit list with a scripts/ prefix exception.
NARROW_BETA_QA_TOOLING = frozenset(
    {
        "scripts/build_current_source_browser_fixture.py",
        "scripts/build_perf_baseline.py",
        "scripts/build_public_demo.py",
        "scripts/build_ui_crawl_fixture.py",
        "scripts/check_changelog_entry.py",
        "scripts/check_public_demo_artifact.py",
        "scripts/check_public_docs.py",
        "scripts/check_public_screenshots.py",
        "scripts/check_release_changelog_coverage.py",
        "scripts/dev_check.py",
        "scripts/render_release_notes.py",
        "scripts/run_compose_runtime_matrix.py",
        "scripts/run_history_perf_harness.py",
        "scripts/run_perf_harness.py",
        "scripts/run_private_qa_restore.py",
        "scripts/run_test_shard.py",
        "scripts/ui_crawl.js",
        "scripts/validate_release_wrap.py",
        "scripts/verify_wiki_drift.py",
    }
)
# Match only the exact expected assignments in the old blob. Byte comparison
# after replacing their version groups preserves whitespace, newlines and all
# other content, including dependency versions in package-lock.json.
_JSON_ROOT_VERSION = re.compile(rb'^  "version": "(?P<version>[^"\n]+)",?$', re.MULTILINE)
NARROW_BETA_VERSION_LINES = {
    "app/__init__.py": (re.compile(rb'^__version__ = "(?P<version>[^"\n]+)"$', re.MULTILINE),),
    "package.json": (_JSON_ROOT_VERSION,),
    "package-lock.json": (
        _JSON_ROOT_VERSION,
        re.compile(
            rb'^    "": \{\n(?:      "name": "[^"\n]*",\n)?'
            rb'      "version": "(?P<version>[^"\n]+)",?$',
            re.MULTILINE,
        ),
    ),
}
GIT_TIMEOUT_SECONDS = 60


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


def _has_pending_owner_publication_evidence(evidence: str) -> bool:
    match = re.fullmatch(r"Pending owner publication:\s*(.+)", evidence, flags=re.IGNORECASE)
    if match is None:
        return False
    targets = {
        target.strip().lower()
        for target in re.split(r"\s*(?:,|;|\band\b)\s*", match.group(1), flags=re.IGNORECASE)
        if target.strip()
    }
    return bool(targets) and targets <= OWNER_PUBLICATION_TARGETS


def _docs_publication_is_deferred(rows: dict[str, list[str]], phase: str) -> bool:
    row = rows.get(DOCS_PUBLICATION_GATE.lower())
    return bool(
        row
        and phase == "pre-tag"
        and row[3].lower() == "blocked"
        and _has_pending_owner_publication_evidence(row[2])
    )


def validate_release_wrap_text(
    text: str,
    *,
    allow_blocked: bool = False,
    phase: str = "final",
    require_changelog_coverage: bool = True,
    require_wiki_verification: bool = False,
    wiki_verification: WikiVerificationResult | None = None,
    version: str | None = None,
    repository: Path | None = None,
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

    rows = parse_checklist_evidence_table(text)
    if not rows:
        issues.append(ValidationIssue("release wrap is missing the checklist evidence table"))
        return issues

    required_gates = REQUIRED_GATES
    if require_wiki_verification:
        required_gates += (DOCS_PUBLICATION_GATE,)

    for gate in required_gates:
        row = rows.get(gate.lower())
        if row is None:
            issues.append(ValidationIssue(f"missing checklist evidence row: {gate}"))
            continue

        required, evidence, result, reason = row[1], row[2], row[3], row[4]
        if required.lower() not in {"yes", "no"}:
            issues.append(ValidationIssue(f"{gate}: Required must be yes or no"))

        normalized_result = result.lower()
        if normalized_result not in VALID_RESULTS:
            issues.append(ValidationIssue(f"{gate}: Result must be Pass, Blocked, N/A, or Deferred"))
            continue

        if normalized_result == "pass" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Pass requires evidence"))
        if normalized_result == "blocked" and not allow_blocked:
            pre_tag_post_publish = normalized_phase == "pre-tag" and gate.lower() in POST_PUBLISH_GATES
            if not pre_tag_post_publish:
                issues.append(ValidationIssue(f"{gate}: Blocked gates cannot ship"))
        if normalized_result == "blocked" and not evidence:
            issues.append(ValidationIssue(f"{gate}: Blocked requires evidence"))
        if (
            gate == DOCS_PUBLICATION_GATE
            and normalized_phase == "pre-tag"
            and normalized_result == "blocked"
            and not _has_pending_owner_publication_evidence(evidence)
        ):
            issues.append(
                ValidationIssue(
                    f"{DOCS_PUBLICATION_GATE}: Blocked evidence must identify pending owner publication"
                )
            )
        if normalized_result == "n/a" and reason.lower() in {"", "-", "n/a", "none", "reason"}:
            issues.append(ValidationIssue(f"{gate}: N/A requires a concrete reason"))

    issues.extend(
        narrow_beta_issues(
            text,
            rows,
            version=version,
            repository=repository,
            phase=normalized_phase,
        )
    )

    wiki_row = rows.get(DOCS_PUBLICATION_GATE.lower())
    wiki_is_deferred = _docs_publication_is_deferred(rows, normalized_phase)
    if require_wiki_verification and not wiki_is_deferred:
        if wiki_row is not None and wiki_row[3].lower() != "pass":
            issues.append(
                ValidationIssue(
                    f"{DOCS_PUBLICATION_GATE}: Result must be Pass after publication"
                )
            )
        if wiki_verification is None:
            issues.append(
                ValidationIssue(f"{DOCS_PUBLICATION_GATE}: wiki drift verification was not run")
            )
        elif not wiki_verification.matches:
            issues.append(
                ValidationIssue(
                    f"{DOCS_PUBLICATION_GATE}: external wiki differs from repository wiki/"
                )
            )
        elif wiki_row is not None and wiki_verification.release_evidence() not in wiki_row[2]:
            issues.append(
                ValidationIssue(
                    f"{DOCS_PUBLICATION_GATE}: evidence does not contain the exact wiki "
                    "verification receipt"
                )
            )

    return issues


def _semver_key(version: str) -> tuple:
    """SemVer 2.0 precedence key; raises ValueError for anything else."""

    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"not a semantic version: {version}")
    core = tuple(int(part) for part in match.groups())
    prerelease = version.split("+", 1)[0].partition("-")[2]
    if not prerelease:
        return (core, 1, ())
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in prerelease.split(".")
    )
    return (core, 0, identifiers)


def _prerelease_label(version: str) -> str:
    return version.split("+", 1)[0].partition("-")[2].split(".", 1)[0].lower()


def _git(repository: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        text=text,
        env=environment,
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    return "; ".join(line.strip() for line in result.stderr.splitlines() if line.strip()) or (
        f"git exit {result.returncode}"
    )


def _version_only_change(
    repository: Path, previous_ref: str, candidate_ref: str, path: str, previous: str, candidate: str
) -> bool:
    blobs = []
    for ref in (previous_ref, candidate_ref):
        entry = _git(repository, "ls-tree", "-z", ref, "--", path, text=False)
        # Missing, renamed, deleted, executable or symlink version files are not
        # a version-only bump, even if their blob content happens to match.
        if entry.returncode != 0 or not entry.stdout.startswith(b"100644 blob "):
            return False
        blob = _git(repository, "show", f"{ref}:{path}", text=False)
        if blob.returncode != 0:
            return False
        blobs.append(blob.stdout)
    expected, actual = blobs
    for pattern in NARROW_BETA_VERSION_LINES[path]:
        matches = list(pattern.finditer(expected))
        if len(matches) != 1 or matches[0].group("version") != previous.encode("ascii"):
            return False
        start, end = matches[0].span("version")
        expected = expected[:start] + candidate.encode("ascii") + expected[end:]
    return expected == actual


def narrow_beta_path_is_allowed(path: str) -> bool:
    """Return whether a changed path keeps a beta narrow (version files excluded)."""

    return (
        path.startswith(NARROW_BETA_PATH_PREFIXES)
        or path in NARROW_BETA_QA_TOOLING
        or path.endswith(".md")
    )


def narrow_beta_eligibility_issues(
    repository: Path | None, version: str, previous_tag: str, *, phase: str
) -> list[ValidationIssue]:
    """Verify with git that every path changed since ``previous_tag`` is narrow.

    The candidate is ``HEAD`` before tagging and ``refs/tags/v<version>`` after.
    Any git failure, missing tag or unexpected tag fails closed.
    """

    prefix = f"release v{version} is not eligible for a narrow beta"
    if repository is None:
        return [ValidationIssue(f"{prefix}: no git repository was given to verify the changed paths")]
    tag_match = RELEASE_TAG_RE.fullmatch(previous_tag)
    if tag_match is None:
        return [ValidationIssue(f"{prefix}: Previous release tag {previous_tag!r} is not a vX.Y.Z release tag")]
    previous_version = tag_match.group(1)
    if _semver_key(previous_version) >= _semver_key(version):
        return [
            ValidationIssue(
                f"{prefix}: Previous release tag {previous_tag} must be earlier than the release version"
            )
        ]
    candidate_ref = "HEAD" if phase == "pre-tag" else f"refs/tags/v{version}"
    previous_ref = f"refs/tags/{previous_tag}"
    try:
        for ref in (previous_ref, candidate_ref):
            resolved = _git(repository, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            if resolved.returncode != 0:
                return [ValidationIssue(f"{prefix}: git cannot resolve {ref}: {_git_error(resolved)}")]
        tags = _git(repository, "tag", "--list", "v*")
        if tags.returncode != 0:
            return [ValidationIssue(f"{prefix}: git tag --list failed: {_git_error(tags)}")]
        earlier = [
            (key, tag)
            for tag in tags.stdout.split()
            if (match := RELEASE_TAG_RE.fullmatch(tag)) is not None
            and VERSION_RE.fullmatch(match.group(1)) is not None
            and (key := _semver_key(match.group(1))) < _semver_key(version)
        ]
        latest = max(earlier)[1] if earlier else None
        if latest != previous_tag:
            return [
                ValidationIssue(
                    f"{prefix}: Previous release tag is {previous_tag}, but the previous release tag is "
                    f"{latest or 'missing'}"
                )
            ]
        ancestor = _git(repository, "merge-base", "--is-ancestor", previous_ref, candidate_ref)
        if ancestor.returncode != 0:
            return [ValidationIssue(f"{prefix}: {previous_tag} is not an ancestor of {candidate_ref}")]
        diff = _git(
            repository, "diff", "--name-only", "--no-renames", "--no-ext-diff",
            f"{previous_ref}..{candidate_ref}",
        )
        if diff.returncode != 0:
            return [ValidationIssue(f"{prefix}: git diff failed: {_git_error(diff)}")]
        disqualifying = []
        for path in sorted(set(diff.stdout.splitlines())):
            if not path or narrow_beta_path_is_allowed(path):
                continue
            if path in NARROW_BETA_VERSION_LINES and _version_only_change(
                repository, previous_ref, candidate_ref, path, previous_version, version
            ):
                continue
            disqualifying.append(path)
    except (OSError, subprocess.SubprocessError) as exc:
        return [ValidationIssue(f"{prefix}: git is unavailable: {exc}")]
    if disqualifying:
        return [
            ValidationIssue(
                f"{prefix}: paths changed since {previous_tag} outside the narrow-beta rules: "
                + ", ".join(disqualifying)
            )
        ]
    return []


def narrow_beta_issues(
    text: str,
    rows: dict[str, list[str]],
    *,
    version: str | None,
    repository: Path | None,
    phase: str,
) -> list[ValidationIssue]:
    # Canonical presence checks are separate. Release-specific rows must obey
    # the same Deferred restrictions even when Required is no.
    deferred_rows = [row for row in rows.values() if row[3].lower() == "deferred"]
    deferred = [row[0] for row in deferred_rows]
    issues: list[ValidationIssue] = []
    allowed = {gate.lower() for gate in DEFERRABLE_NARROW_BETA_GATES}
    for gate, _required, evidence, _result, _reason in deferred_rows:
        if gate.lower() not in allowed:
            issues.append(ValidationIssue(
                f"{gate}: Deferred is not allowed for this gate; only "
                f"{', '.join(DEFERRABLE_NARROW_BETA_GATES)} may be Deferred in a narrow beta"
            ))
        if DEFERRAL_TARGET_RE.search(evidence) is None:
            issues.append(ValidationIssue(
                f"{gate}: Deferred evidence must name the next rc/final full run "
                "('Deferred to the next rc/final full run')"
            ))
    modes = [value.strip("`").strip().lower() for value in RELEASE_MODE_LINE.findall(text)]
    previous_tags = [value.strip("`").strip() for value in PREVIOUS_TAG_LINE.findall(text)]
    if not deferred and not modes:
        return issues
    if len(modes) > 1:
        issues.append(ValidationIssue("release wrap must declare Release mode at most once"))
    mode = modes[0] if modes else None
    if mode is not None and mode not in {NARROW_BETA_MODE, FULL_RELEASE_MODE}:
        issues.append(ValidationIssue("Release mode must be 'narrow beta' or 'full'"))
    narrow = mode == NARROW_BETA_MODE and len(modes) == 1
    if deferred and not narrow:
        issues.append(
            ValidationIssue(
                "Deferred rows require 'Release mode: narrow beta' and a verified "
                f"previous release tag: {', '.join(deferred)}"
            )
        )
    if not narrow and not deferred:
        return issues

    if version is None:
        issues.append(ValidationIssue("narrow beta validation requires the release version"))
        return issues
    version = version.removeprefix("v")
    try:
        beta = is_prerelease(version) and _prerelease_label(version) == "beta"
    except ValueError as exc:
        issues.append(ValidationIssue(str(exc)))
        return issues
    if not beta:
        issues.append(
            ValidationIssue(
                f"Deferred rows are only allowed for a beta prerelease; {version} must Pass every gate"
                if deferred
                else f"Release mode narrow beta is only allowed for a beta prerelease, not {version}"
            )
        )
        return issues
    if not narrow:
        return issues
    if len(previous_tags) != 1 or not previous_tags[0]:
        issues.append(
            ValidationIssue("narrow beta must declare exactly one 'Previous release tag: vX.Y.Z...' line")
        )
        return issues
    issues.extend(narrow_beta_eligibility_issues(repository, version, previous_tags[0], phase=phase))
    return issues


def release_wrap_path(repository: Path, version: str) -> Path:
    """Return the release wrap for ``version``: current under docs/, older under docs/archive/."""

    current = repository / "docs" / f"RELEASE_WRAP_{version}.md"
    if current.exists():
        return current
    archived = repository / "docs" / "archive" / f"RELEASE_WRAP_{version}.md"
    if archived.exists():
        return archived
    return current


def validate_release_wrap_path(
    path: Path,
    *,
    allow_blocked: bool = False,
    phase: str = "final",
    require_changelog_coverage: bool = True,
    require_wiki_verification: bool = False,
    wiki_verification: WikiVerificationResult | None = None,
    version: str | None = None,
    repository: Path | None = None,
) -> list[ValidationIssue]:
    if not path.exists():
        return [ValidationIssue(f"release wrap not found: {path}")]
    return validate_release_wrap_text(
        path.read_text(encoding="utf-8"),
        allow_blocked=allow_blocked,
        phase=phase,
        require_changelog_coverage=require_changelog_coverage,
        require_wiki_verification=require_wiki_verification,
        wiki_verification=wiki_verification,
        version=version,
        repository=repository,
    )


def wiki_drift_required(version: str) -> bool:
    match = VERSION_RE.fullmatch(version.removeprefix("v"))
    if match is None:
        raise ValueError("version must use semantic version form X.Y.Z")
    return tuple(int(part) for part in match.groups()) >= WIKI_DRIFT_REQUIRED_FROM


def changelog_coverage_required(version: str) -> bool:
    match = VERSION_RE.fullmatch(version.removeprefix("v"))
    if match is None:
        raise ValueError("version must use semantic version form X.Y.Z")
    return tuple(int(part) for part in match.groups()) >= CHANGELOG_COVERAGE_REQUIRED_FROM


def is_prerelease(version: str) -> bool:
    """``X.Y.Z-beta.1`` is a prerelease; ``+build`` metadata alone is not."""

    version = version.removeprefix("v")
    if VERSION_RE.fullmatch(version) is None:
        raise ValueError("version must use semantic version form X.Y.Z")
    return "-" in version.split("+", 1)[0]


def public_demo_freshness_required(version: str) -> bool:
    """Stable releases rebuild the demo; a prerelease waits for the next stable one."""

    match = VERSION_RE.fullmatch(version.removeprefix("v"))
    if match is None:
        raise ValueError("version must use semantic version form X.Y.Z")
    return not is_prerelease(version) and tuple(map(int, match.groups())) >= PUBLIC_DEMO_FRESHNESS_REQUIRED_FROM


def public_demo_release_issues(repository: Path, version: str) -> list[ValidationIssue]:
    """Refuse a release whose checked-in public demo was not rebuilt for it.

    Pull requests only check that the demo is an untampered build of the commit
    it records. Cutting a release is where the demo must be rebuilt from the
    release source, recaptured, and pixel-reviewed; these checks fail until that
    has happened.
    """

    version = version.removeprefix("v")
    issues: list[ValidationIssue] = []
    for label, command in (
        (
            "checked-in public demo is not current for this release source",
            ("scripts/check_public_demo_artifact.py", "public-demo", "--require-current"),
        ),
        (
            "public demo screenshots do not match the rebuilt demo or lack a PASS pixel review",
            ("scripts/check_public_screenshots.py", "--root", "."),
        ),
    ):
        result = subprocess.run(
            (sys.executable, *command),
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            details = "; ".join(line.strip() for line in result.stderr.splitlines() if line.strip())
            issues.append(ValidationIssue(f"{PUBLIC_DEMO_GATE}: {label}: {details or 'check failed'}"))
    artifact = repository / "public-demo" / "index.html"
    try:
        artifact_versions = set(PUBLIC_DEMO_VERSION_RE.findall(artifact.read_text(encoding="utf-8")))
    except (OSError, UnicodeError):
        artifact_versions = set()
    if artifact_versions != {version}:
        found = ", ".join(sorted(artifact_versions)) or "none"
        issues.append(
            ValidationIssue(
                f"{PUBLIC_DEMO_GATE}: public demo shows app version {found}, not the release version {version}; "
                "rebuild it from the release commit (see docs/RELEASE_CHECKLIST.md, Public demo rebuild)"
            )
        )
    return issues


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
    parser.add_argument(
        "--public-demo-only",
        action="store_true",
        help=(
            "Only check that the checked-in public demo and screenshots were rebuilt and "
            "reviewed for this release version. The GHCR release workflow runs this."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    version = args.version.removeprefix("v")
    if args.public_demo_only:
        try:
            if is_prerelease(version):
                print(f"Prerelease {version}: the public demo waits for the next stable release.")
                return 0
            demo_issues = public_demo_release_issues(args.repository, version)
        except ValueError as exc:
            print(f"- {exc}")
            return 1
        for issue in demo_issues:
            print(f"- {issue.message}")
        if demo_issues:
            return 1
        print(f"Public demo is current for release {version}.")
        return 0
    try:
        require_wiki_verification = wiki_drift_required(version)
        require_changelog_coverage = changelog_coverage_required(version)
    except ValueError as exc:
        print(f"- {exc}")
        return 1
    path = release_wrap_path(args.repository, version)
    wiki_verification = None
    rows = {}
    if path.exists():
        rows = parse_checklist_evidence_table(path.read_text(encoding="utf-8"))
    wiki_is_deferred = _docs_publication_is_deferred(rows, args.phase)
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
        repository_authority = "HEAD"
        repository_authority_label = "repository source HEAD"
        if args.phase == "final":
            repository_authority = f"refs/tags/v{version}"
            repository_authority_label = f"release tag v{version}"
        try:
            wiki_verification = verify_wiki_drift(
                repository=args.repository,
                repository_commit=args.repository_commit,
                wiki_source=args.wiki_source,
                external_wiki_commit=args.external_wiki_commit,
                repository_authority=repository_authority,
                repository_authority_label=repository_authority_label,
            )
        except (OSError, UnicodeError, WikiVerificationError) as exc:
            print(f"- {DOCS_PUBLICATION_GATE}: wiki verification failed: {exc}")
            return 1
    issues = validate_release_wrap_path(
        path,
        allow_blocked=args.allow_blocked,
        phase=args.phase,
        require_changelog_coverage=require_changelog_coverage,
        require_wiki_verification=require_wiki_verification,
        wiki_verification=wiki_verification,
        version=version,
        repository=args.repository,
    )
    if public_demo_freshness_required(version):
        issues.extend(public_demo_release_issues(args.repository, version))
    if issues:
        for issue in issues:
            print(f"- {issue.message}")
        return 1
    print(f"{path} checklist evidence is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
