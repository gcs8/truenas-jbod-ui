"""Pull-request gate: require a changelog fragment under changelog.d/.

The gate reads the ``base...head`` diff of one pull request with ``git`` only.
It never talks to the network. A fragment is named for the pull request that
adds it, ``changelog.d/<N>.<section>.md`` (``<N>.<section>.<k>.md`` for a
second and later entry, ``k`` from 2); see ``scripts/changelog_fragments.py``
for the format. The checks, in order:

1. Every path the diff adds under ``changelog.d/`` (including a rename or copy
   target) must be named for this pull request's number; the failure names
   the file and the name it should have.
2. A fragment that is already on the base branch belongs to another pull
   request: deleting, renaming, editing, or changing its mode or type is
   refused, on a release cut too. The ``changelog-edit-ok`` label, applied by
   a maintainer after reading the diff, waives this check and nothing else.
3. The ``no-changelog`` or ``dependencies`` label makes an entry optional.
4. Otherwise the diff must add at least one valid fragment named for this pull
   request, and every fragment it adds or changes must be valid. A pull
   request labelled ``breaking`` must add one in the ``breaking`` or
   ``upgrade`` section. Editing ``CHANGELOG.md`` does not count, except for a
   release cut: a pull request that introduces the first SemVer release
   heading and adds a bullet naming itself as ``(#N)`` in that new section
   passes as before.

Usage from a checkout that has both refs::

    python scripts/check_changelog_entry.py --base origin/main --head HEAD \\
        --pr 123 --labels bug,breaking

In GitHub Actions the job checks out the merge commit of the pull request and
passes ``--merged-head <pull request head commit>`` instead of ``--base``. The
base is then the first parent of that merge commit: the base branch exactly as
it was merged. The base commit recorded in the event payload is not used,
because it stays where the pull request opened; once another pull request
merges, it would charge this one with that pull request's fragments, and it
would hide a change to a fragment that reached the base branch later.

``--event "$GITHUB_EVENT_PATH"`` supplies the pull request
number and labels from the event payload; ``--pr`` and ``--labels`` override
those values when given. ``--advisory`` is passed for pull requests whose
author has no write access and so cannot apply the ``no-changelog`` label.
Advisory mode softens only a missing or invalid entry (checks 3 and 4): it
keeps the message and exits 0. It never softens checks 1 and 2, which exit 1
in advisory mode too, because the author can fix them alone.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__:
    from scripts import changelog_fragments as fragments
else:
    import changelog_fragments as fragments  # type: ignore[no-redef]

CHANGELOG_PATH = "CHANGELOG.md"
UNRELEASED_HEADING = "## Unreleased"
SEMVER_CORE = r"(?:0|[1-9][0-9]*)"
SEMVER_PRERELEASE_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
RELEASE_HEADING = re.compile(
    rf"^## v{SEMVER_CORE}\.{SEMVER_CORE}\.{SEMVER_CORE}"
    rf"(?:-{SEMVER_PRERELEASE_IDENTIFIER}(?:\.{SEMVER_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+{SEMVER_BUILD_IDENTIFIER}(?:\.{SEMVER_BUILD_IDENTIFIER})*)?"
    r"(?: - \d{4}-\d{2}-\d{2})?$"
)
NO_CHANGELOG_LABEL = "no-changelog"
# Dependabot pull requests cannot add a changelog line; they still reach the
# release body under "Dependencies" through .github/release.yml.
SKIP_LABELS = (NO_CHANGELOG_LABEL, "dependencies")
BREAKING_LABEL = "breaking"
# Applied by a maintainer after reading: allows changing or deleting a fragment
# that is already on the base branch (for example on a revert). It waives that
# protection only.
EDIT_LABEL = "changelog-edit-ok"

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
    re.compile(r"^docs/archive/"),
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
    # A refusal the author can fix alone (rename an own fragment, restore
    # another one), so advisory mode does not turn it into a pass.
    protected: bool = False


def _run_git(repo: Path, args: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, **kwargs)
    except OSError as exc:
        raise GateError("could not run git; install git and put it on PATH") from exc


def _git(repo: Path, *args: str) -> str:
    completed = _run_git(repo, args, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


_COMMIT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def resolve_commit(repo: Path, option: str, value: str) -> str:
    """The commit id that ``value``, given as ``option``, names.

    Every git call after this one receives the id, never ``value``. A value
    that is empty, starts with '-' or names no single commit (a tree, a blob,
    a missing object, a range) is refused, so git never reads it as an option.
    """

    detail = ""
    if value and not value.startswith("-"):
        completed = _run_git(
            repo,
            ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{value}^{{commit}}"),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        commit = completed.stdout.strip()
        if completed.returncode == 0 and _COMMIT_ID.fullmatch(commit):
            return commit
        detail = completed.stderr.strip()
    raise GateError(f"{option} {value} does not name a commit" + (f": {detail}" if detail else ""))


def merge_commit_base(repo: Path, *, head: str, pull_request_head: str) -> str:
    """Return the base commit that ``head`` merged the pull request into.

    ``head`` must be the merge commit built for the pull request: exactly two
    parents, the base branch first and ``pull_request_head`` second. Anything
    else is refused, so the gate never guesses a base.
    """

    if not _COMMIT_ID.fullmatch(pull_request_head):
        raise GateError("the pull request head must be a full lowercase commit id")
    commits = _git(repo, "rev-list", "--parents", "--max-count=1", "--end-of-options", head, "--").split()
    if len(commits) != 3 or commits[2] != pull_request_head:
        raise GateError(
            f"the checked-out commit is not the merge of pull request head {pull_request_head} into its base "
            "branch, so there is no base to compare against"
        )
    return commits[1]


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


def current_entries(lines: list[str]) -> tuple[str, list[Entry]]:
    """Return bullets from Unreleased or the first versioned release section.

    Continuation lines (indented, non-empty) are joined to the bullet so a
    trailing ``(#N)`` may sit on a wrapped line.
    """

    headings = [line.strip() for line in lines if line.startswith("## ")]
    if UNRELEASED_HEADING in headings:
        selected_heading = UNRELEASED_HEADING
    else:
        selected_heading = next(
            (heading for heading in headings if RELEASE_HEADING.fullmatch(heading)),
            UNRELEASED_HEADING,
        )
    entries: list[Entry] = []
    in_current = False
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
            in_current = raw.strip() == selected_heading
            subsection = ""
            continue
        if not in_current:
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
    return selected_heading, entries


ADVISORY_PREFIX = (
    "Advisory only: this pull request's author cannot apply repository labels, "
    "so the changelog gate reports instead of blocking. Before merge a "
    "maintainer either applies the label named below or adds the fragment."
)
BREAKING_FRAGMENT_SECTIONS = ("breaking", "upgrade")


def evaluate(
    repo: Path,
    *,
    base: str,
    head: str,
    pr_number: int,
    labels: set[str],
    advisory: bool = False,
) -> GateResult:
    """Decide the gate for one pull request.

    ``advisory`` is used for contributors without write access, who cannot
    apply the ``no-changelog`` label the failure text asks for. Advisory mode
    softens only a missing or invalid entry: it keeps the messages and passes,
    and a maintainer resolves the reported item before merge. It never softens
    a fragment named for another pull request or a change to a fragment that
    is already on the base branch.
    """

    # Every git call below receives these commit ids, never the given values.
    base = resolve_commit(repo, "--base", base)
    head = resolve_commit(repo, "--head", head)
    result = _evaluate(repo, base=base, head=head, pr_number=pr_number, labels=labels)
    if result.ok or not advisory or result.protected:
        return result
    return GateResult(True, [ADVISORY_PREFIX, *result.messages])


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = _run_git(repo, args)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def fragment_changes(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """(status letter, path) for every changelog.d/ path the diff touches.

    Renames are not detected, so a rename is a deletion of the old path plus an
    addition of the new one, and a copy is an addition.
    """

    # No trailing slash: a path named exactly changelog.d that is not a directory shows up too.
    raw = _git_bytes(repo, "diff", "--name-status", "-z", "--no-renames", f"{base}...{head}", "--", fragments.FRAGMENT_DIR)
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    return [(fields[index][0], fields[index + 1]) for index in range(0, len(fields) - 1, 2) if fields[index]]


def _expected_name(path: str, pr_number: int) -> str:
    pieces = path.replace("\\", "/").rsplit("/", 1)[-1].split(".")
    section = pieces[1] if len(pieces) > 1 and pieces[1] in fragments.SECTIONS else "<section>"
    return f"{fragments.FRAGMENT_DIR}/{pr_number}.{section}.md"


def _protected_result(repo: Path, base: str, head: str, pr_number: int, labels: set[str]) -> GateResult | None:
    """Checks 1 and 2: fragments named for another pull request, and changes to
    fragments already on the base branch. Neither label nor advisory mode
    softens them; ``changelog-edit-ok`` waives check 2 only."""

    readme = f"{fragments.FRAGMENT_DIR}/{fragments.README_NAME}"
    changes = [(status, path) for status, path in fragment_changes(repo, base, head) if path != readme]
    messages: list[str] = []

    misnamed = sorted(path for status, path in changes if status == "A" and fragments.name_number(path) != pr_number)
    if misnamed:
        messages += [
            f"#{pr_number} adds changelog fragments that are not named for it; a pull request adds only "
            f"fragments named {fragments.FRAGMENT_DIR}/{pr_number}.<section>.md:",
            *(f"  {path}: name it {_expected_name(path, pr_number)}" for path in misnamed),
            f"Use {fragments.FRAGMENT_DIR}/{pr_number}.<section>.<k>.md, with k from 2, for a second entry in "
            "the same section. A pull request that carries work from other pull requests names its fragments "
            "with its own number. Labels and advisory mode do not waive this.",
        ]

    changed = sorted((path, status) for status, path in changes if status != "A")
    if changed and EDIT_LABEL not in labels:
        messages += [
            f"#{pr_number} changes changelog fragments that are already on the base branch; they belong "
            "to other pull requests:",
            *(f"  {path}: {_change_word(status)}" for path, status in changed),
            "Restore them; the exemption labels and advisory mode do not allow this. A maintainer may "
            f"apply the '{EDIT_LABEL}' label after reading the diff, for example on a revert or on the release "
            "pull request that collects them.",
        ]
    if not messages:
        return None
    return GateResult(False, messages, protected=True)


def _change_word(status: str) -> str:
    return {"D": "deleted or renamed", "M": "content or mode changed", "T": "type changed"}.get(status, "changed")


def _fragment_at(repo: Path, head: str, path: str) -> tuple[bytes, bool]:
    """Content of ``path`` at ``head`` and whether it is a regular file."""

    entry = _git_bytes(repo, "ls-tree", "-z", head, "--", path).decode("utf-8", "surrogateescape")
    meta, _tab, _name = entry.partition("\t")
    parts = meta.split(" ")
    if len(parts) != 3 or parts[0] not in {"100644", "100755"} or parts[1] != "blob":
        return b"", False
    return _git_bytes(repo, "cat-file", "blob", parts[2]), True


def _missing_fragment_messages(pr_number: int, paths: list[str]) -> list[str]:
    directory = fragments.FRAGMENT_DIR
    messages = [
        f"No changelog fragment added for #{pr_number}. Add one file named for this pull request, for example "
        f"add {directory}/{pr_number}.fixed.md, holding one past-tense sentence about the change. The release "
        f"appends '(#{pr_number})' from the file name, so do not write it; any #N inside parentheses is refused, "
        "and a plain mention of an issue or another pull request, such as 'see #123', is allowed.",
        f"Name it {directory}/{pr_number}.<section>.md ({directory}/{pr_number}.<section>.<k>.md, with k from 2, "
        f"for a second entry in the same section); <section> is one of: {fragments.section_names()}. "
        f"See {directory}/README.md.",
    ]
    if CHANGELOG_PATH in paths:
        messages.append(
            f"This pull request edits {CHANGELOG_PATH}; that no longer counts as an entry. Move the "
            f"bullet into a fragment and drop the {CHANGELOG_PATH} edit."
        )
    if not any(is_relevant_path(path) for path in paths):
        messages.append(
            "The diff touches no operator-visible path; an 'internal' fragment is accepted for tests, "
            "CI or tooling."
        )
    messages.append(
        f"If this pull request is intentionally invisible to operators, apply the "
        f"'{NO_CHANGELOG_LABEL}' label and re-run the check instead."
    )
    return messages


def _evaluate(
    repo: Path,
    *,
    base: str,
    head: str,
    pr_number: int,
    labels: set[str],
) -> GateResult:
    paths = changed_paths(repo, base, head)

    protected = _protected_result(repo, base, head, pr_number, labels)
    if protected is not None:
        return protected

    skipping = [label for label in SKIP_LABELS if label in labels]
    if skipping:
        return GateResult(
            True,
            [f"{skipping[0]} label present; CHANGELOG entry not required for #{pr_number}."],
        )

    problems: list[str] = []
    added: list[fragments.Fragment] = []
    for status, path in fragment_changes(repo, base, head):
        if status == "D" or path == f"{fragments.FRAGMENT_DIR}/{fragments.README_NAME}":
            continue
        data, regular = _fragment_at(repo, head, path)
        problem = fragments.fragment_problem(path, data, regular_file=regular)
        if problem is not None:
            problems.append(f"{path}: {problem}")
        elif status == "A":
            added.append(fragments.parse_fragment(path, data))
    if problems:
        return GateResult(
            False,
            [
                f"Invalid changelog fragment(s) in #{pr_number}:",
                *(f"  {problem}" for problem in problems),
                f"See {fragments.FRAGMENT_DIR}/README.md for the format.",
            ],
        )

    if added:
        sections = {fragment.section for fragment in added}
        if BREAKING_LABEL in labels and not sections & set(BREAKING_FRAGMENT_SECTIONS):
            return GateResult(
                False,
                [
                    f"#{pr_number} carries the '{BREAKING_LABEL}' label, so it must also add "
                    f"{fragments.FRAGMENT_DIR}/{pr_number}.breaking.md or "
                    f"{fragments.FRAGMENT_DIR}/{pr_number}.upgrade.md that says what an operator must "
                    "do on upgrade."
                ],
            )
        listed = ", ".join(f"{fragment.path} ({fragment.heading})" for fragment in added)
        return GateResult(True, [f"Changelog fragment for #{pr_number}: {listed}."])

    release_cut = _release_cut_result(repo, base, head, pr_number, labels, paths)
    if release_cut is not None:
        return release_cut
    return GateResult(False, _missing_fragment_messages(pr_number, paths))


def _release_cut_result(
    repo: Path, base: str, head: str, pr_number: int, labels: set[str], paths: list[str]
) -> GateResult | None:
    """Admit a release cut the way the gate always has, or return None.

    A release cut introduces the first SemVer release heading of CHANGELOG.md
    (the collector renames Unreleased) and adds a bullet naming this pull
    request in that new section.
    """

    selected_heading = _added_release_heading(repo, base, head, paths)
    if selected_heading is None:
        return None
    head_lines = _head_changelog_lines(repo, head)
    added = added_line_numbers(repo, base, head, CHANGELOG_PATH)
    _heading, entries = current_entries(head_lines)
    touched = [
        entry
        for entry in entries
        if entry.subsection in ALLOWED_SUBSECTIONS
        and entry.names_pr(pr_number)
        and _entry_was_added(entry, entries, added)
    ]
    if not touched:
        return None
    if BREAKING_LABEL in labels and not any(entry.subsection in BREAKING_SUBSECTIONS for entry in touched):
        return GateResult(
            False,
            [
                f"#{pr_number} carries the '{BREAKING_LABEL}' label, so CHANGELOG.md must also add "
                f"a bullet ending in '(#{pr_number})' under '### Breaking changes' or "
                f"'### Upgrade notes' that says what an operator must do on upgrade."
            ],
        )
    subsections = sorted({entry.subsection for entry in touched})
    return GateResult(
        True,
        [f"CHANGELOG entry for #{pr_number} found in {selected_heading} under: {', '.join(subsections)}."],
    )


def _added_release_heading(repo: Path, base: str, head: str, paths: list[str]) -> str | None:
    """The release heading this pull request introduces in CHANGELOG.md, or None."""

    if CHANGELOG_PATH not in paths:
        return None
    head_lines = _head_changelog_lines(repo, head)
    selected_heading, _entries = current_entries(head_lines)
    added = added_line_numbers(repo, base, head, CHANGELOG_PATH)
    if selected_heading == UNRELEASED_HEADING or not _heading_was_added(head_lines, selected_heading, added):
        return None
    return selected_heading


def _heading_was_added(lines: list[str], heading: str, added: set[int]) -> bool:
    """True when this pull request itself introduces ``heading`` (a release cut)."""

    return any(
        number in added
        for number, raw in enumerate(lines, start=1)
        if raw.startswith("## ") and raw.strip() == heading
    )


def _entry_was_added(entry: Entry, entries: list[Entry], added: set[int]) -> bool:
    """True when at least one line of ``entry`` is an added diff line.

    ``Entry.text`` joins the bullet with its continuation lines, so the entry
    spans ``line_count`` consecutive lines starting at ``first_line``.
    """

    return any(line in added for line in range(entry.first_line, entry.first_line + entry.line_count))


def labels_from_event(path: Path) -> tuple[int | None, set[str]]:
    """Number and labels of the pull request in an event payload; ValueError if it has another shape."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    pull_request = (payload.get("pull_request") or {}) if isinstance(payload, dict) else None
    number = pull_request.get("number") if isinstance(pull_request, dict) else None
    raw_labels = pull_request.get("labels", []) if isinstance(pull_request, dict) else None
    if not isinstance(raw_labels, list) or isinstance(number, bool) or not isinstance(number, (int, str, type(None))):
        raise ValueError("it is not the payload of a pull request event")
    labels = {str(label.get("name", "")).strip() for label in raw_labels if isinstance(label, dict)}
    return (int(number) if number is not None else None), {label for label in labels if label}


def parse_labels(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    # Escapes the program name and every parser error, which can quote command-line text.
    parser = fragments.ArgumentParser(description=__doc__.splitlines()[0])
    base_source = parser.add_mutually_exclusive_group(required=True)
    base_source.add_argument("--base", help="Base ref or SHA (the pull request target).")
    base_source.add_argument(
        "--merged-head",
        help=(
            "Pull request head commit id. --head must then be the merge commit of that head "
            "into the base branch; its first parent is used as the base."
        ),
    )
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
        "--advisory",
        action="store_true",
        help=(
            "Report a missing entry without failing. Used for pull requests whose "
            "author cannot apply the 'no-changelog' label."
        ),
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
            print(fragments.printable(f"could not read event payload {args.event}: {exc}"))
            return 2
        labels = event_labels
        if pr_number is None:
            pr_number = event_number
    if args.labels is not None:
        labels = parse_labels(args.labels)
    if pr_number is None:
        print(fragments.printable("a pull request number is required (--pr or --event)"))
        return 2

    try:
        base = args.base
        if base is None:
            base = merge_commit_base(args.repo, head=args.head, pull_request_head=args.merged_head)
        result = evaluate(
            args.repo,
            base=base,
            head=args.head,
            pr_number=pr_number,
            labels=labels,
            advisory=args.advisory,
        )
    except GateError as exc:
        print(fragments.printable(str(exc)))
        return 2

    # File names, git arguments and git errors are printed through printable(),
    # so none of them can start a log line of its own.
    for message in result.messages:
        print(fragments.printable(message))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
