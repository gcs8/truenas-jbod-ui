"""Validate, preview and collect changelog fragments under changelog.d/.

A pull request records itself in one small file instead of editing
``CHANGELOG.md``::

    changelog.d/<slug>.<section>.md

``<slug>`` is 3 to 60 lowercase letters, digits and hyphens, starting with a
letter or digit. ``<section>`` is one of ``breaking``, ``upgrade``,
``security``, ``added``, ``changed``, ``fixed``, ``performance``, ``docs`` or
``internal``. The file holds the text of one bullet as one inline paragraph:
past tense, no leading ``- ``, no heading, no HTML comment, no link reference
definition, at most 4096 bytes, and no ``#N`` inside parentheses (the release
tooling reads every ``(#N)`` as a pull request reference). It may wrap over
several lines.

A fragment belongs to the commit that added it on the release branch. The
collector reads the pull request number from that commit's subject (a squash
subject ends in ``(#N)``) and appends ``(#N)`` to the bullet. It never guesses:
a fragment is bound only when the repository is not shallow, the file is
committed at ``--ref`` with the same bytes, and the adding commit added it as a
new file rather than renaming or copying another fragment. Any other fragment
stops ``collect`` unless the release preparer passes ``--pr <file>=<N>``; a
``--pr`` for one fragment of a commit must be given for every fragment that
commit added.

Usage::

    python scripts/changelog_fragments.py check
    python scripts/changelog_fragments.py preview [--pr <file>=<N> ...]
    python scripts/changelog_fragments.py collect --version 0.25.0 --date 2026-12-01 [--pr <file>=<N> ...]

``collect`` writes the fragments into ``CHANGELOG.md`` under ``## Unreleased``,
renames that heading to ``## vX.Y.Z - YYYY-MM-DD``, deletes the fragment files
it read and prints them. It re-reads every fragment just before writing and
refuses if one changed; it deletes a file only while its bytes are still the
bytes it read. It refuses a version that already has a section, so a second
run cannot insert the same entries twice.
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
from dataclasses import dataclass, field, replace
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
# Checked on the stripped lines the renderer receives, so indentation cannot hide them.
HEADING_LINE = re.compile(r"^#{1,6}(?:\s|$)")
SETEXT_UNDERLINE = re.compile(r"^(?:=+|-+)$")
LINK_DEFINITION = re.compile(r"^\[(?:[^\]\\]|\\.)+\]:")
COMMENT_DELIMITER = ("<!--", "-->")
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


RECOVERY_TEXT = "Delete the ones already in that section and commit, or restore CHANGELOG.md from Git and collect again."


class CollectError(ValueError):
    """Raised when fragments cannot be collected; nothing has been written."""


class CleanupIncomplete(CollectError):
    """CHANGELOG.md was written, but some fragments were left in place."""

    def __init__(self, message: str, result: CollectResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class Fragment:
    path: str  # repository-relative, for example changelog.d/topic.fixed.md
    slug: str
    section: str
    lines: tuple[str, ...]
    data: bytes = field(default=b"", repr=False, compare=False)

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
    normalized = [line.strip() for line in lines]
    for line in lines:
        if FENCE_LINE.match(line):
            return (), "contains a code fence; keep a fragment to one paragraph"
    for line in normalized:
        if HEADING_LINE.match(line):
            return (), "contains a heading line; write only the bullet text"
    if any(delimiter in text for delimiter in COMMENT_DELIMITER):
        return (), "contains an HTML comment delimiter ('<!--' or '-->'); it can hide the entries after it"
    if any(LINK_DEFINITION.match(line) for line in normalized):
        return (), (
            "contains a link reference definition ('[label]: target'); it can change links elsewhere "
            "in CHANGELOG.md, so write an inline link '[text](target)' instead"
        )
    if any(SETEXT_UNDERLINE.match(line) for line in normalized[1:]):
        return (), "contains a setext heading underline (a line of only '=' or '-'); write only the bullet text"
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
                f"names pull request number #{token.group(1)} in parentheses; the release tooling reads "
                "every '(#N)' as a pull request reference. Leave out this pull request's own number (the "
                "collector adds it) and mention another issue or pull request without parentheses, for "
                "example 'see #N'"
            )
    return tuple(normalized), None


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
    return Fragment(path=path, slug=slug, section=section, lines=text_lines(data)[0], data=data)


def text_in_section(data: bytes, section: str) -> bool:
    """True when the text of fragment ``data`` appears in rendered ``section``.

    Whitespace is normalised and the final full stop dropped, because the
    rendered bullet puts `` (#N)`` before it.
    """

    lines = text_lines(data)[0] or tuple(data.decode("utf-8", "replace").split("\n"))
    body = " ".join(" ".join(lines).split()).removesuffix(".")
    return not body or body in " ".join(section.split())


def _read_regular(path: Path) -> bytes | None:
    """Bytes of ``path`` when it is a regular file, else None."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except FileNotFoundError:
        return None


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


def _git_bytes(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True)


def is_shallow(root: Path) -> bool | None:
    """True for a shallow clone, False for full history, None outside Git."""

    completed = _git(root, "rev-parse", "--is-shallow-repository")
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() == "true"


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


def _committed_bytes(root: Path, ref: str, path: str) -> bytes | None:
    """Bytes of ``path`` at ``ref`` when it is a regular file there, else None."""

    listed = _git_bytes(root, "ls-tree", "-z", ref, "--", path)
    meta = listed.stdout.decode("utf-8", "surrogateescape").partition("\t")[0].split(" ")
    if listed.returncode != 0 or len(meta) != 3 or meta[0] not in {"100644", "100755"} or meta[1] != "blob":
        return None
    blob = _git_bytes(root, "cat-file", "blob", meta[2])
    return blob.stdout if blob.returncode == 0 else None


def _rename_source(root: Path, sha: str, path: str) -> str | None:
    """Why ``path`` in commit ``sha`` is not a plain addition, or None.

    Rename and copy detection runs against the first parent and only among
    fragment paths, so a fragment renamed or copied from another one is caught.
    """

    parents = _git(root, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    if len(parents) < 2:
        return None
    completed = _git_bytes(
        root, "diff-tree", "-r", "-z", "--name-status", "-M", "-C", "--find-copies-harder",
        parents[1], sha, "--", f"{FRAGMENT_DIR}/",
    )
    fields = completed.stdout.decode("utf-8", "surrogateescape").split("\0")
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index][0]
        if status in "RC":
            source, target = fields[index + 1], fields[index + 2]
            if target == path:
                return f"{'renamed' if status == 'R' else 'copied'} in {sha[:12]} from {source}"
            index += 3
        else:
            index += 2
    return None


@dataclass(frozen=True)
class Binding:
    """How one fragment gets its pull request number."""

    path: str
    commit: str | None  # the adding commit, when the fragment is committed at the ref
    subject: str
    number: int | None
    reason: str | None  # why there is no number
    explicit: bool = False  # given with --pr


def bind_fragments(
    root: Path,
    fragments: list[Fragment],
    ref: str = "HEAD",
    pr_overrides: dict[str, int] | None = None,
) -> dict[str, Binding]:
    """Bind every fragment to a pull request number, never by guessing.

    Raises CollectError for a shallow repository, for a ``--pr`` that names no
    fragment, and for a ``--pr`` that covers only some of the fragments one
    commit added.
    """

    overrides = _resolve_overrides(fragments, pr_overrides or {})
    shallow = is_shallow(root) if fragments else False
    if shallow:
        raise CollectError(
            f"{root} is a shallow clone, so the commit that added a fragment cannot be proved; "
            "fetch the full history first (git fetch --unshallow) and run again"
        )
    bindings: dict[str, Binding] = {}
    for fragment in fragments:
        path = fragment.path
        if shallow is None:
            bindings[path] = Binding(path, None, "", None, "not in a Git checkout")
            continue
        committed = _committed_bytes(root, ref, path)
        if committed is None:
            bindings[path] = Binding(path, None, "", None, f"not committed at {ref}")
            continue
        commit = adding_commit(root, path, ref)
        if commit is None:
            bindings[path] = Binding(path, None, "", None, f"no first-parent commit on {ref} added it")
            continue
        sha, subject = commit
        reason: str | None = None
        number: int | None = None
        if committed != fragment.data:
            reason = f"the file differs from the one committed at {ref}"
        else:
            reason = _rename_source(root, sha, path)
        if reason is None:
            match = SQUASH_SUBJECT.search(subject)
            if match is None:
                reason = f"the commit that added it ({sha[:12]} {subject!r}) does not end in '(#N)'"
            else:
                number = int(match.group(1))
        bindings[path] = Binding(path, sha, subject, number, reason)

    partial: list[str] = []
    for path in sorted(overrides):
        sha = bindings[path].commit
        if sha is None:
            continue
        siblings = sorted(p for p, b in bindings.items() if b.commit == sha and p not in overrides)
        if siblings:
            partial.append(f"  {sha[:12]} {bindings[path].subject!r}: also pass --pr for " + ", ".join(siblings))
    if partial:
        raise CollectError(
            "--pr must name every fragment the same commit added, or none of them:\n" + "\n".join(partial)
        )
    for path, number in overrides.items():
        bindings[path] = replace(bindings[path], number=number, reason=None, explicit=True)
    return bindings


def bind_numbers(
    root: Path, fragments: list[Fragment], ref: str = "HEAD"
) -> tuple[dict[str, int], dict[str, str]]:
    """Map each fragment to the pull request number of its adding commit.

    Returns the bound numbers and, for every unbound fragment, the reason.
    """

    bindings = bind_fragments(root, fragments, ref)
    numbers = {path: b.number for path, b in bindings.items() if b.number is not None}
    unbound = {path: b.reason or "" for path, b in bindings.items() if b.number is None}
    return numbers, unbound


def binding_notes(bindings: dict[str, Binding]) -> list[str]:
    """One line per adding commit naming the fragments it added and their numbers."""

    by_commit: dict[str, list[Binding]] = {}
    for binding in bindings.values():
        if binding.commit is not None:
            by_commit.setdefault(binding.commit, []).append(binding)
    notes = []
    for sha, group in sorted(by_commit.items(), key=lambda item: min(b.path for b in item[1])):
        listed = ", ".join(
            f"{b.path} ({'#' + str(b.number) if b.number is not None else 'no number'}"
            f"{', from --pr' if b.explicit else ''})"
            for b in sorted(group, key=lambda b: b.path)
        )
        notes.append(f"commit {sha[:12]} {group[0].subject!r} added {listed}")
    return notes


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


def combined_view(
    root: Path, text: str, *, ref: str = "HEAD", pr_overrides: dict[str, int] | None = None
) -> tuple[str, list[str], dict[str, Binding]]:
    """``text`` with every valid fragment under ``root`` merged into Unreleased.

    Fragments bound to a pull request carry ``(#N)``; the others show without a
    number. Returns the text, notes about invalid, unbound and per-commit
    fragments, and the binding of each fragment.
    """

    fragments, problems = load_fragments(root)
    bindings = bind_fragments(root, fragments, ref, pr_overrides)
    numbers = {path: b.number for path, b in bindings.items() if b.number is not None}
    notes = [f"invalid fragment {problem}" for problem in problems]
    notes += [
        f"{path} shows without a pull request number: {b.reason}"
        for path, b in sorted(bindings.items())
        if b.number is None
    ]
    notes += binding_notes(bindings)
    return merge_into_unreleased(text, bullets_by_section(fragments, numbers)), notes, bindings


def combined_changelog(
    root: Path, text: str, *, ref: str = "HEAD", pr_overrides: dict[str, int] | None = None
) -> tuple[str, list[str]]:
    """``combined_view`` without the bindings."""

    combined, notes, _bindings = combined_view(root, text, ref=ref, pr_overrides=pr_overrides)
    return combined, notes


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


def _recovery_lines(root: Path, heading: str, released: str) -> list[str]:
    """Each fragment still on disk, whether its text is in ``released``, and what to do."""

    fragments, _problems = load_fragments(root)
    if not fragments:
        return []
    lines = [
        "These fragments are still in changelog.d/ (a previous collect may have been interrupted "
        "after writing CHANGELOG.md):"
    ]
    for fragment in fragments:
        where = "already in" if text_in_section(fragment.data, released) else "not in"
        lines.append(f"  {fragment.path}: {where} '{heading}'")
    lines.append(RECOVERY_TEXT)
    return lines


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

    Everything is checked before anything is written, and every fragment is
    read again just before the write: if one changed or disappeared, nothing is
    written. The changelog is written first (to a temporary file, then renamed
    over), then each fragment read in this run is deleted while its bytes are
    still the bytes that were read; a changed or missing one is left in place
    and reported with CleanupIncomplete. That is two steps, not one atomic
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
        message += _recovery_lines(root, existing, section_text(text, existing))
        for problem in problems:
            message.append(f"  invalid fragment {problem}")
        raise CollectError("\n".join(message))
    if problems:
        raise CollectError("invalid fragments; fix them before collecting:\n" + "\n".join(f"  {p}" for p in problems))
    bindings = bind_fragments(root, fragments, ref, pr_overrides)
    numbers = {path: b.number for path, b in bindings.items() if b.number is not None}
    missing = {path: b.reason for path, b in bindings.items() if b.number is None}
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
    changed = [fragment.path for fragment in fragments if _read_regular(root / fragment.path) != fragment.data]
    if changed:
        raise CollectError(
            "these fragments changed or disappeared after they were read; nothing was written:\n"
            + "\n".join(f"  {path}" for path in changed)
        )
    _write_changelog(changelog_path, "\n".join(merged))

    result = CollectResult(heading=heading, numbers={path: numbers[path] for path in sorted(numbers)})
    left: list[str] = []
    for fragment in sorted(fragments, key=lambda item: item.path):
        path = root / fragment.path
        current = _read_regular(path)
        if current != fragment.data:
            left.append(f"  {fragment.path}: {'gone' if current is None else 'changed'} after it was read")
            continue
        try:
            _remove_fragment(path)
        except FileNotFoundError:
            left.append(f"  {fragment.path}: gone after it was read")
            continue
        result.consumed.append(fragment.path)
    if left:
        message = [
            f"Wrote '{heading}' to {CHANGELOG_PATH} and deleted {len(result.consumed)} fragment(s), but left "
            "these in place because they are no longer the bytes that were read:",
            *left,
            *_recovery_lines(root, heading, section_text("\n".join(merged), heading)),
        ]
        if RECOVERY_TEXT not in message:
            message.append(RECOVERY_TEXT)
        raise CleanupIncomplete("\n".join(message), result)
    return result


def add_pr_option(parser: argparse.ArgumentParser) -> None:
    """The one --pr option shared by preview, collect and the coverage check."""

    parser.add_argument(
        "--pr",
        action="append",
        default=[],
        metavar="FILE=N",
        help=(
            "Pull request number for a fragment that history does not bind. Repeatable. Give it for every "
            "fragment the same commit added, or for none of them."
        ),
    )


def parse_pr_option(values: list[str]) -> dict[str, int]:
    """Parse repeated ``--pr <file>=<N>`` values; validation happens when binding."""

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
    preview_parser = commands.add_parser(
        "preview",
        help="Print the Unreleased section with every fragment merged in; unbound ones show without (#N).",
    )
    add_pr_option(preview_parser)
    collect_parser = commands.add_parser(
        "collect",
        help="Write the fragments into CHANGELOG.md as a release section and delete them.",
    )
    collect_parser.add_argument("--version", required=True, help="Release version, X.Y.Z or X.Y.Z-pre.")
    collect_parser.add_argument("--date", required=True, help="Release date, YYYY-MM-DD.")
    add_pr_option(collect_parser)
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
            combined, notes = combined_changelog(root, text, ref=args.ref, pr_overrides=parse_pr_option(args.pr))
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
            pr_overrides=parse_pr_option(args.pr),
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
