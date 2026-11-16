"""Validate, preview and collect changelog fragments under changelog.d/.

A pull request records itself in one small file instead of editing
``CHANGELOG.md``::

    changelog.d/<slug>.<section>.md

``<slug>`` is 3 to 60 lowercase letters, digits and hyphens, starting with a
letter or digit. ``<section>`` is one of ``breaking``, ``upgrade``,
``security``, ``added``, ``changed``, ``fixed``, ``performance``, ``docs`` or
``internal``. The file holds the text of one bullet: past tense, no leading
``- ``, no heading, no pull request number, at most 4096 bytes. It may wrap
over several lines.

A fragment belongs to the commit that added it on the release branch. The
collector reads the pull request number from that commit's subject (a squash
subject ends in ``(#N)``) and appends ``(#N)`` to the bullet. It never guesses:
a fragment whose adding commit carries no number stops ``collect`` unless the
release preparer passes ``--pr <file>=<N>``.

Usage::

    python scripts/changelog_fragments.py check
    python scripts/changelog_fragments.py preview
    python scripts/changelog_fragments.py collect --version 0.25.0 --date 2026-12-01

``collect`` writes the fragments into ``CHANGELOG.md`` under ``## Unreleased``,
renames that heading to ``## vX.Y.Z - YYYY-MM-DD``, deletes exactly the
fragment files it read and prints them. It refuses a version that already has
a section, so a second run cannot insert the same entries twice.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CHANGELOG_PATH = "CHANGELOG.md"
FRAGMENT_DIR = "changelog.d"
README_NAME = "README.md"
UNRELEASED_HEADING = "## Unreleased"
MAX_FRAGMENT_BYTES = 4096

# Fragment section name -> CHANGELOG.md subsection heading, in file order.
# Highlights are written by hand at release time; there is no fragment for them.
SECTIONS = {
    "breaking": "Breaking changes",
    "upgrade": "Upgrade notes",
    "security": "Security",
    "added": "Added",
    "changed": "Changed",
    "fixed": "Fixed",
    "performance": "Performance",
    "docs": "Docs",
    "internal": "Internal",
}
SUBSECTION_ORDER = ("Highlights", *SECTIONS.values())

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,59}")
HEADING_LINE = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
FENCE_LINE = re.compile(r"^\s*(?:```|~~~)")
LIST_MARKER = re.compile(r"^\s*[-*+]\s")
PARENTHETICAL = re.compile(r"\(([^()]*)\)")
PR_TOKEN = re.compile(r"(?<![\w#])#(\d+)(?!\d)")
SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
SEMVER_CORE = r"(?:0|[1-9][0-9]*)"
SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
VERSION = re.compile(
    rf"{SEMVER_CORE}\.{SEMVER_CORE}\.{SEMVER_CORE}(?:-{SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*)?"
)


class CollectError(ValueError):
    """Raised when fragments cannot be collected; nothing has been written."""


@dataclass(frozen=True)
class Fragment:
    path: str  # repository-relative, for example changelog.d/topic.fixed.md
    slug: str
    section: str
    lines: tuple[str, ...]

    @property
    def heading(self) -> str:
        return SECTIONS[self.section]


@dataclass
class CollectResult:
    heading: str
    consumed: list[str] = field(default_factory=list)
    numbers: dict[str, int] = field(default_factory=dict)


def section_names() -> str:
    return ", ".join(SECTIONS)


def name_problem(path: str) -> str | None:
    """Reason a repository-relative path is not a valid fragment name, or None."""

    parts = path.replace("\\", "/").split("/")
    if len(parts) != 2 or parts[0] != FRAGMENT_DIR:
        return f"fragments live directly in {FRAGMENT_DIR}/, not in a subdirectory"
    pieces = parts[1].split(".")
    if len(pieces) != 3 or pieces[2] != "md" or SLUG.fullmatch(pieces[0]) is None:
        return (
            f"bad name; use {FRAGMENT_DIR}/<slug>.<section>.md, where <slug> is 3 to 60 lowercase "
            "letters, digits or hyphens starting with a letter or digit"
        )
    if pieces[1] not in SECTIONS:
        return f"unknown section '{pieces[1]}'; use one of: {section_names()}"
    return None


def text_lines(data: bytes) -> tuple[tuple[str, ...], str | None]:
    """Return the normalized lines of a fragment and the reason it is invalid."""

    if len(data) > MAX_FRAGMENT_BYTES:
        return (), f"too long ({len(data)} bytes; the limit is {MAX_FRAGMENT_BYTES})"
    if b"\0" in data:
        return (), "contains a NUL byte"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return (), "is not UTF-8 text"
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return (), "is empty; write one past-tense sentence"
    for line in lines:
        if HEADING_LINE.match(line):
            return (), "contains a heading line; write only the bullet text"
        if FENCE_LINE.match(line):
            return (), "contains a code fence; keep a fragment to one paragraph"
    if LIST_MARKER.match(lines[0]):
        return (), "starts with a list marker such as '- '; write the text without it"
    if any(LIST_MARKER.match(line) for line in lines[1:]):
        return (), "holds more than one bullet; write one bullet per fragment"
    if any(not line for line in lines):
        return (), "contains a blank line; a fragment is one paragraph"
    joined = " ".join(line.strip() for line in lines)
    for group in PARENTHETICAL.finditer(joined):
        token = PR_TOKEN.search(group.group(1))
        if token is not None:
            return (), (
                f"names pull request number #{token.group(1)} in parentheses; leave it out, "
                "the collector adds the number of the pull request that merged the fragment"
            )
    return tuple(line.strip() for line in lines), None


def fragment_problem(path: str, data: bytes, *, regular_file: bool = True) -> str | None:
    """Reason ``path`` with content ``data`` is not a valid fragment, or None."""

    problem = name_problem(path)
    if problem is not None:
        return problem
    if not regular_file:
        return "is not a regular file"
    return text_lines(data)[1]


def parse_fragment(path: str, data: bytes) -> Fragment:
    problem = fragment_problem(path, data)
    if problem is not None:
        raise ValueError(f"{path}: {problem}")
    slug, section, _suffix = path.split("/", 1)[1].split(".")
    return Fragment(path=path, slug=slug, section=section, lines=text_lines(data)[0])


def load_fragments(root: Path) -> tuple[list[Fragment], list[str]]:
    """Read every fragment in ``root/changelog.d``; return them and any problems."""

    directory = root / FRAGMENT_DIR
    fragments: list[Fragment] = []
    problems: list[str] = []
    if not directory.is_dir():
        return fragments, problems
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = f"{FRAGMENT_DIR}/{entry.name}"
        if entry.name == README_NAME and entry.is_file() and not entry.is_symlink():
            continue
        if entry.is_dir() and not entry.is_symlink():
            problems.append(f"{relative}/: fragments live directly in {FRAGMENT_DIR}/, not in a subdirectory")
            continue
        regular = entry.is_file() and not entry.is_symlink()
        data = entry.read_bytes() if regular else b""
        problem = fragment_problem(relative, data, regular_file=regular)
        if problem is not None:
            problems.append(f"{relative}: {problem}")
            continue
        fragments.append(parse_fragment(relative, data))
    return fragments, problems


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def adding_commit(root: Path, path: str, ref: str = "HEAD") -> tuple[str, str] | None:
    """Return (sha, subject) of the latest first-parent commit on ``ref`` that added ``path``."""

    completed = _git(
        root,
        "log",
        "--first-parent",
        "--diff-merges=first-parent",
        "--no-patch",
        "--diff-filter=A",
        "--format=%H%x00%s",
        "-n",
        "1",
        ref,
        "--",
        path,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    sha, _, subject = completed.stdout.strip().partition("\0")
    return sha, subject


def bind_numbers(
    root: Path, fragments: list[Fragment], ref: str = "HEAD"
) -> tuple[dict[str, int], dict[str, str]]:
    """Map each fragment to the pull request number of its adding commit.

    Returns the bound numbers and, for every unbound fragment, the reason.
    """

    numbers: dict[str, int] = {}
    unbound: dict[str, str] = {}
    for fragment in fragments:
        commit = adding_commit(root, fragment.path, ref)
        if commit is None:
            unbound[fragment.path] = f"no commit on {ref} added it (not committed yet?)"
            continue
        sha, subject = commit
        match = SQUASH_SUBJECT.search(subject)
        if match is None:
            unbound[fragment.path] = (
                f"the commit that added it ({sha[:12]} {subject!r}) does not end in '(#N)'"
            )
            continue
        numbers[fragment.path] = int(match.group(1))
    return numbers, unbound


def render_bullet(fragment: Fragment, number: int | None) -> list[str]:
    lines = [f"- {fragment.lines[0]}", *(f"  {line}" for line in fragment.lines[1:])]
    if number is not None:
        last = lines[-1]
        if last.endswith("."):
            lines[-1] = f"{last[:-1]} (#{number})."
        else:
            lines[-1] = f"{last} (#{number})"
    return lines


def bullets_by_section(fragments: list[Fragment], numbers: dict[str, int]) -> dict[str, list[list[str]]]:
    """Rendered bullets per section, ordered by pull request number then path."""

    ordered = sorted(
        fragments,
        key=lambda item: (numbers.get(item.path) is None, numbers.get(item.path, 0), item.path),
    )
    result: dict[str, list[list[str]]] = {}
    for fragment in ordered:
        result.setdefault(fragment.section, []).append(render_bullet(fragment, numbers.get(fragment.path)))
    return result


def _top_level_headings(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if line.startswith("## ")]


def _unreleased_bounds(lines: list[str]) -> tuple[int, int] | None:
    headings = _top_level_headings(lines)
    matches = [index for index in headings if lines[index].strip() == UNRELEASED_HEADING]
    if len(matches) > 1:
        raise CollectError(f"{CHANGELOG_PATH} has more than one '{UNRELEASED_HEADING}' heading")
    if not matches:
        return None
    start = matches[0]
    end = next((index for index in headings if index > start), len(lines))
    return start, end


def merge_into_unreleased(text: str, bullets: dict[str, list[list[str]]]) -> str:
    """Insert rendered bullets into the Unreleased section of ``text``.

    Existing lines are never changed or reordered: each bullet goes after the
    last existing line of its subsection, and a missing subsection is inserted
    before the first existing subsection that comes later in the standard
    order. Without an Unreleased section one is inserted above the first
    release heading.
    """

    lines = text.split("\n")
    if not any(bullets.values()):
        return text
    if _unreleased_bounds(lines) is None:
        headings = _top_level_headings(lines)
        position = headings[0] if headings else len(lines)
        lines[position:position] = [UNRELEASED_HEADING, ""]
    for section, heading in SECTIONS.items():
        entries = bullets.get(section) or []
        if not entries:
            continue
        new_lines = [line for bullet in entries for line in bullet]
        bounds = _unreleased_bounds(lines)
        assert bounds is not None  # inserted above when missing
        start, end = bounds
        subsections = [
            (lines[index][4:].strip(), index) for index in range(start + 1, end) if lines[index].startswith("### ")
        ]
        names = [name for name, _index in subsections]
        if names.count(heading) > 1:
            raise CollectError(f"'{UNRELEASED_HEADING}' has more than one '### {heading}' subsection")
        if heading in names:
            index = names.index(heading)
            block_start = subsections[index][1]
            block_end = subsections[index + 1][1] if index + 1 < len(subsections) else end
            last = max(
                (row for row in range(block_start, block_end) if lines[row].strip()),
                default=block_start,
            )
            if last == block_start:
                lines[last + 1:last + 1] = ["", *new_lines]
            else:
                lines[last + 1:last + 1] = new_lines
            continue
        rank = SUBSECTION_ORDER.index(heading)
        later = next(
            (
                row
                for name, row in subsections
                if name in SUBSECTION_ORDER and SUBSECTION_ORDER.index(name) > rank
            ),
            None,
        )
        if later is not None:
            lines[later:later] = [f"### {heading}", "", *new_lines, ""]
            continue
        last = max((row for row in range(start, end) if lines[row].strip()), default=start)
        insert = ["", f"### {heading}", "", *new_lines]
        if last + 1 >= len(lines) or lines[last + 1].startswith("## "):
            insert.append("")
        lines[last + 1:last + 1] = insert
    return "\n".join(lines)


def combined_changelog(root: Path, text: str, *, ref: str = "HEAD") -> tuple[str, list[str]]:
    """``text`` with every valid fragment under ``root`` merged into Unreleased.

    Fragments bound to a pull request carry ``(#N)``; the others show without a
    number. Returns the text and notes about invalid or unbound fragments.
    """

    fragments, problems = load_fragments(root)
    numbers, unbound = bind_numbers(root, fragments, ref) if fragments else ({}, {})
    notes = [f"invalid fragment {problem}" for problem in problems]
    notes += [f"{path} shows without a pull request number: {reason}" for path, reason in sorted(unbound.items())]
    return merge_into_unreleased(text, bullets_by_section(fragments, numbers)), notes


def section_text(text: str, heading: str) -> str:
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line.strip() == heading.strip()), None)
    if start is None:
        return ""
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n") + "\n"


def _resolve_overrides(fragments: list[Fragment], overrides: dict[str, int]) -> dict[str, int]:
    by_path = {fragment.path: fragment for fragment in fragments}
    resolved: dict[str, int] = {}
    unknown: list[str] = []
    for raw, number in overrides.items():
        key = raw.replace("\\", "/")
        if "/" not in key:
            key = f"{FRAGMENT_DIR}/{key}"
        if key not in by_path:
            unknown.append(raw)
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CollectError(f"--pr {raw}={number}: the number must be a positive integer")
        resolved[key] = number
    if unknown:
        raise CollectError(
            "--pr names files that are not fragments in this checkout: " + ", ".join(sorted(unknown))
        )
    return resolved


def _release_heading(version: str, date: str) -> str:
    version = version.removeprefix("v")
    if VERSION.fullmatch(version) is None:
        raise CollectError(f"--version {version!r} is not X.Y.Z or X.Y.Z-prerelease")
    try:
        parsed = datetime.date.fromisoformat(date)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != date:
        raise CollectError(f"--date {date!r} is not a YYYY-MM-DD date")
    return f"## v{version} - {date}"


def _remove_fragment(path: Path) -> None:
    path.unlink()


def _write_changelog(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".CHANGELOG.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def collect(
    root: Path,
    *,
    version: str,
    date: str,
    pr_overrides: dict[str, int] | None = None,
    ref: str = "HEAD",
) -> CollectResult:
    """Fold every fragment into ``CHANGELOG.md`` as the ``version`` section.

    Everything is checked before anything is written. The changelog is written
    first (to a temporary file, then renamed over), then exactly the fragment
    files read in this run are deleted. That is two steps, not one atomic
    change: if the deletions are interrupted, the next run for the same version
    refuses and lists the fragments that are already in the section.
    """

    heading = _release_heading(version, date)
    changelog_path = root / CHANGELOG_PATH
    text = changelog_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    version_heading = heading.rsplit(" - ", 1)[0]
    existing = next(
        (
            line.strip()
            for line in lines
            if line.startswith("## ")
            and (line.strip() == version_heading or line.strip().startswith(version_heading + " "))
        ),
        None,
    )
    fragments, problems = load_fragments(root)
    if existing is not None:
        message = [f"{CHANGELOG_PATH} already has '{existing}'; refusing to collect v{version.removeprefix('v')} twice."]
        released = section_text(text, existing)
        remaining = [*fragments]
        if remaining:
            message.append(
                "These fragments are still in changelog.d/ (a previous collect may have been interrupted "
                "after writing CHANGELOG.md):"
            )
            joined_section = " ".join(released.split())
            for fragment in remaining:
                # The rendered bullet is the fragment text with " (#N)" before
                # its final full stop, so compare the text without that stop.
                body = " ".join(" ".join(fragment.lines).split()).removesuffix(".")
                where = "already in" if body in joined_section else "not in"
                message.append(f"  {fragment.path}: {where} '{existing}'")
            message.append(
                "Delete the ones already in that section and commit, or restore CHANGELOG.md from Git and "
                "collect again."
            )
        for problem in problems:
            message.append(f"  invalid fragment {problem}")
        raise CollectError("\n".join(message))
    if problems:
        raise CollectError("invalid fragments; fix them before collecting:\n" + "\n".join(f"  {p}" for p in problems))
    overrides = _resolve_overrides(fragments, pr_overrides or {})
    numbers, unbound = bind_numbers(root, fragments, ref)
    numbers.update(overrides)
    missing = {path: reason for path, reason in unbound.items() if path not in overrides}
    if missing:
        raise CollectError(
            "no pull request number for these fragments; pass --pr <file>=<N> for each after checking it:\n"
            + "\n".join(f"  {path}: {reason}" for path, reason in sorted(missing.items()))
        )
    if _unreleased_bounds(lines) is None and not fragments:
        raise CollectError(f"nothing to collect: no '{UNRELEASED_HEADING}' section and no fragments")

    merged = merge_into_unreleased(text, bullets_by_section(fragments, numbers)).split("\n")
    bounds = _unreleased_bounds(merged)
    assert bounds is not None
    merged[bounds[0]] = heading
    _write_changelog(changelog_path, "\n".join(merged))

    result = CollectResult(heading=heading, numbers={path: numbers[path] for path in sorted(numbers)})
    for fragment in sorted(fragments, key=lambda item: item.path):
        _remove_fragment(root / fragment.path)
        result.consumed.append(fragment.path)
    return result


def _parse_pr_option(values: list[str]) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for value in values:
        name, separator, number = value.rpartition("=")
        if not separator or not name or not number.isdigit():
            raise CollectError(f"--pr {value!r}: use <file>=<number>")
        overrides[name] = int(number)
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root (default: cwd).")
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="Commit whose first-parent history binds fragments to pull requests (default: HEAD).",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="{check,preview,collect}")
    commands.add_parser("check", help="Validate every fragment in changelog.d/.")
    commands.add_parser(
        "preview",
        help="Print the Unreleased section with every fragment merged in; unbound ones show without (#N).",
    )
    collect_parser = commands.add_parser(
        "collect",
        help="Write the fragments into CHANGELOG.md as a release section and delete them.",
    )
    collect_parser.add_argument("--version", required=True, help="Release version, X.Y.Z or X.Y.Z-pre.")
    collect_parser.add_argument("--date", required=True, help="Release date, YYYY-MM-DD.")
    collect_parser.add_argument(
        "--pr",
        action="append",
        default=[],
        metavar="FILE=N",
        help="Pull request number for a fragment whose adding commit has none. Repeatable.",
    )
    args = parser.parse_args(argv)
    root: Path = args.repo

    if args.command == "check":
        fragments, problems = load_fragments(root)
        for problem in problems:
            print(problem)
        if problems:
            return 1
        print(f"{len(fragments)} changelog fragment(s) valid.")
        return 0

    if args.command == "preview":
        try:
            text = (root / CHANGELOG_PATH).read_text(encoding="utf-8")
            combined, notes = combined_changelog(root, text, ref=args.ref)
        except (OSError, CollectError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(section_text(combined, UNRELEASED_HEADING))
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        return 0

    try:
        result = collect(
            root,
            version=args.version,
            date=args.date,
            pr_overrides=_parse_pr_option(args.pr),
            ref=args.ref,
        )
    except (OSError, CollectError) as exc:
        print(str(exc))
        return 1
    print(f"Wrote '{result.heading}' to {CHANGELOG_PATH}.")
    for path in result.consumed:
        print(f"consumed {path} (#{result.numbers[path]})")
    print(f"{len(result.consumed)} fragment(s) consumed and deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
