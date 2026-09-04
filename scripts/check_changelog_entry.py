"""Pull-request gate: require a CHANGELOG.md line for code-visible changes.

The gate reads the ``base...head`` diff of one pull request with ``git`` only.
It never talks to the network. When the diff touches a path that operators can
notice (application code, service code, scripts, Compose files, config
examples, docs, or the wiki) and the pull request carries neither the
``no-changelog`` nor the ``dependencies`` label, the diff must add at least
one bullet under
``## Unreleased`` in one of the allowed ``### `` subsections whose trailing
parenthetical names the pull request as ``(#N)``. A pull request labelled
``breaking`` must add that bullet under ``### Breaking changes`` or
``### Upgrade notes``.

Usage from a checkout that has both refs::

    python scripts/check_changelog_entry.py --base origin/main --head HEAD \
        --pr 123 --labels bug,breaking

In GitHub Actions ``--event "$GITHUB_EVENT_PATH"`` supplies the pull request
number and labels from the event payload; ``--pr`` and ``--labels`` override
those values when given.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CHANGELOG_PATH = "CHANGELOG.md"
UNRELEASED_HEADING = "## Unreleased"
NO_CHANGELOG_LABEL = "no-changelog"
# Dependabot pull requests cannot add a changelog line; they still reach the
# release body under "Dependencies" through .github/release.yml.
SKIP_LABELS = (NO_CHANGELOG_LABEL, "dependencies")
BREAKING_LABEL = "breaking"

ALLOWED_SUBSECTIONS = (
    "Breaking changes",
    "Upgrade notes",
    "Security",
    "Added",
    "Changed",
    "Fixed",
    "Performance",
    "Docs",
    "Internal",
)
BREAKING_SUBSECTIONS = ("Breaking changes", "Upgrade notes")

RELEVANT_PREFIXES = (
    "app/",
    "admin_service/",
    "history_service/",
    "scripts/",
    "config/",
    "wiki/",
    "docs/",
)
RELEVANT_PATTERNS = (
    re.compile(r"^docker-compose[^/]*\.yml$"),
    re.compile(r"^Dockerfile[^/]*$"),
    re.compile(r"^\.env\.example$"),
)
EXCLUDED_PATTERNS = (
    re.compile(r"^CHANGELOG\.md$"),
    re.compile(r"^docs/RELEASE_WRAP_[^/]*$"),
    re.compile(r"^docs/RELEASE_NOTES_[^/]*$"),
)

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# The bullet ends with the parenthetical, optionally followed by a full stop.
TRAILING_PARENTHETICAL = re.compile(r"\(([^()]*)\)[.\s]*$")


class GateError(Exception):
    """Raised when the gate cannot evaluate the pull request at all."""


@dataclass(frozen=True)
class Entry:
    subsection: str
    first_line: int
    line_count: int
    text: str

    def names_pr(self, pr_number: int) -> bool:
        match = TRAILING_PARENTHETICAL.search(self.text)
        if match is None:
            return False
        token = re.compile(rf"(?<![\w#])#{pr_number}(?!\d)")
        return token.search(match.group(1)) is not None


@dataclass
class GateResult:
    ok: bool
    messages: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def is_relevant_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if any(pattern.match(normalized) for pattern in EXCLUDED_PATTERNS):
        return False
    if normalized.startswith(RELEVANT_PREFIXES):
        return True
    return any(pattern.match(normalized) for pattern in RELEVANT_PATTERNS)


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    output = _git(repo, "diff", "--name-only", f"{base}...{head}", "--")
    return [line.strip() for line in output.splitlines() if line.strip()]


def added_line_numbers(repo: Path, base: str, head: str, path: str) -> set[int]:
    """Return new-file line numbers that the diff adds to ``path``."""

    output = _git(repo, "diff", "--unified=0", f"{base}...{head}", "--", path)
    added: set[int] = set()
    new_line = 0
    for line in output.splitlines():
        hunk = HUNK_HEADER.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            new_line += 1
    return added


def _head_changelog_lines(repo: Path, head: str) -> list[str]:
    try:
        text = _git(repo, "show", f"{head}:{CHANGELOG_PATH}")
    except GateError:
        return []
    return text.splitlines()


def unreleased_entries(lines: list[str]) -> list[Entry]:
    """Return every bullet under ``## Unreleased`` with its subsection.

    Continuation lines (indented, non-empty) are joined to the bullet so a
    trailing ``(#N)`` may sit on a wrapped line.
    """

    entries: list[Entry] = []
    in_unreleased = False
    subsection = ""
    current_lines: list[str] = []
    current_start = 0

    def flush() -> None:
        nonlocal current_lines, current_start
        if current_lines:
            text = " ".join(part.strip() for part in current_lines)
            entries.append(Entry(subsection, current_start, len(current_lines), text))
        current_lines = []
        current_start = 0

    for number, raw in enumerate(lines, start=1):
        if raw.startswith("## "):
            flush()
            in_unreleased = raw.strip() == UNRELEASED_HEADING
            subsection = ""
            continue
        if not in_unreleased:
            continue
        if raw.startswith("### "):
            flush()
            subsection = raw[4:].strip()
            continue
        if raw.startswith("- "):
            flush()
            current_lines = [raw]
            current_start = number
            continue
        if current_lines and raw.startswith("  ") and raw.strip():
            current_lines.append(raw)
            continue
        flush()
    flush()
    return entries


def evaluate(
    repo: Path,
    *,
    base: str,
    head: str,
    pr_number: int,
    labels: set[str],
) -> GateResult:
    paths = changed_paths(repo, base, head)
    relevant = sorted(path for path in paths if is_relevant_path(path))

    skipping = [label for label in SKIP_LABELS if label in labels]
    if skipping:
        return GateResult(
            True,
            [f"{skipping[0]} label present; CHANGELOG entry not required for #{pr_number}."],
        )
    if not relevant:
        return GateResult(
            True,
            [f"No operator-visible paths changed; CHANGELOG entry not required for #{pr_number}."],
        )

    added = added_line_numbers(repo, base, head, CHANGELOG_PATH) if CHANGELOG_PATH in paths else set()
    entries = unreleased_entries(_head_changelog_lines(repo, head)) if added else []
    touched = [
        entry
        for entry in entries
        if entry.subsection in ALLOWED_SUBSECTIONS
        and entry.names_pr(pr_number)
        and _entry_was_added(entry, entries, added)
    ]

    messages: list[str] = []
    if not touched:
        messages.append(
            f"CHANGELOG.md needs an entry for #{pr_number}. The diff touches "
            f"operator-visible paths ({', '.join(relevant[:5])}"
            f"{', ...' if len(relevant) > 5 else ''}) but adds no bullet under "
            f"'{UNRELEASED_HEADING}' that ends with '(#{pr_number})'."
        )
        messages.append(
            "Add one line under '## Unreleased' in one of these subsections: "
            + ", ".join(f"'### {name}'" for name in ALLOWED_SUBSECTIONS)
            + f". Shape: '- <one sentence in past tense> (#{pr_number})'. "
            "Wrap at about 80 columns; the '(#N)' may sit on the continuation line."
        )
        messages.append(
            f"If this pull request is intentionally invisible to operators, apply the "
            f"'{NO_CHANGELOG_LABEL}' label and re-run the check instead."
        )
        return GateResult(False, messages)

    if BREAKING_LABEL in labels and not any(entry.subsection in BREAKING_SUBSECTIONS for entry in touched):
        messages.append(
            f"#{pr_number} carries the '{BREAKING_LABEL}' label, so CHANGELOG.md must also add "
            f"a bullet ending in '(#{pr_number})' under '### Breaking changes' or "
            f"'### Upgrade notes' that says what an operator must do on upgrade."
        )
        return GateResult(False, messages)

    subsections = sorted({entry.subsection for entry in touched})
    messages.append(
        f"CHANGELOG entry for #{pr_number} found under: {', '.join(subsections)}."
    )
    return GateResult(True, messages)


def _entry_was_added(entry: Entry, entries: list[Entry], added: set[int]) -> bool:
    """True when at least one line of ``entry`` is an added diff line.

    ``Entry.text`` joins the bullet with its continuation lines, so the entry
    spans ``line_count`` consecutive lines starting at ``first_line``.
    """

    return any(line in added for line in range(entry.first_line, entry.first_line + entry.line_count))


def labels_from_event(path: Path) -> tuple[int | None, set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pull_request = payload.get("pull_request") or {}
    number = pull_request.get("number")
    labels = {
        str(label.get("name", "")).strip()
        for label in pull_request.get("labels", [])
        if isinstance(label, dict)
    }
    return (int(number) if number is not None else None), {label for label in labels if label}


def parse_labels(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="Base ref or SHA (the pull request target).")
    parser.add_argument("--head", default="HEAD", help="Head ref or SHA (default: HEAD).")
    parser.add_argument("--pr", type=int, help="Pull request number; overrides --event.")
    parser.add_argument(
        "--labels",
        help="Comma-separated pull request labels; overrides the labels in --event.",
    )
    parser.add_argument(
        "--event",
        type=Path,
        help="GitHub event payload JSON (GITHUB_EVENT_PATH) supplying number and labels.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current directory).",
    )
    args = parser.parse_args(argv)

    pr_number = args.pr
    labels: set[str] = set()
    if args.event is not None:
        try:
            event_number, event_labels = labels_from_event(args.event)
        except (OSError, ValueError) as exc:
            print(f"could not read event payload {args.event}: {exc}")
            return 2
        labels = event_labels
        if pr_number is None:
            pr_number = event_number
    if args.labels is not None:
        labels = parse_labels(args.labels)
    if pr_number is None:
        print("a pull request number is required (--pr or --event)")
        return 2

    try:
        result = evaluate(
            args.repo,
            base=args.base,
            head=args.head,
            pr_number=pr_number,
            labels=labels,
        )
    except GateError as exc:
        print(str(exc))
        return 2

    for message in result.messages:
        print(message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
