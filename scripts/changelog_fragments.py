"""Validate, preview and collect changelog fragments under changelog.d/.

A pull request records itself in one small file, named for its own pull
request number, instead of editing ``CHANGELOG.md``::

    changelog.d/<N>.<section>.md
    changelog.d/<N>.<section>.<k>.md   (a second and later entry, k from 2)

``<N>`` is the pull request number and ``<k>`` a counter, both decimal without
leading zeros. ``<section>`` is one of ``breaking``, ``upgrade``,
``security``, ``added``, ``changed``, ``fixed``, ``performance``, ``docs`` or
``internal``. The file holds the text of one bullet as one inline paragraph:
past tense, no leading ``- ``, no heading, no HTML comment, no raw HTML outside
backticks, no link reference definition, at most 4096 bytes, and no ``#N``
inside parentheses (the release tooling reads every ``(#N)`` as a pull request
reference; a plain mention such as ``see #123`` is allowed). It may wrap over
several lines. The collector appends `` (#N)`` from the file name; nothing
reads Git history.

Usage::

    python scripts/changelog_fragments.py check
    python scripts/changelog_fragments.py preview
    python scripts/changelog_fragments.py collect --version 0.25.0 --date 2026-12-01

``collect`` writes the fragments into ``CHANGELOG.md`` under ``## Unreleased``,
renames that heading to ``## vX.Y.Z - YYYY-MM-DD``, deletes the fragment files
it read and prints them. It re-reads every fragment just before writing and
refuses if one changed; it deletes a file only while its bytes are still the
bytes it read. If a deletion fails after the write, it goes on with the rest
and lists the files still present. It refuses a version that already has a
section, so a second run cannot insert the same entries twice.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import os
import re
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

NUMBER = re.compile(r"[1-9][0-9]*")
COUNTER = re.compile(r"[2-9]|[1-9][0-9]+")
# Checked on the stripped lines the renderer receives, so indentation cannot hide them.
HEADING_LINE = re.compile(r"^#{1,6}(?:\s|$)")
SETEXT_UNDERLINE = re.compile(r"^(?:=+|-+)$")
LINK_DEFINITION = re.compile(r"^\[(?:[^\]\\]|\\.)+\]:")
COMMENT_DELIMITER = ("<!--", "-->")
RAW_HTML = re.compile(r"<[A-Za-z/!?]")
BACKTICKS = re.compile(r"`+")
FENCE_LINE = re.compile(r"^\s*(?:```|~~~)")
LIST_MARKER = re.compile(r"^\s*[-*+]\s")
PARENTHETICAL = re.compile(r"\(([^()]*)\)")
PR_TOKEN = re.compile(r"(?<![\w#])#(\d+)(?!\d)")
COMMENT_SPAN = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
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
    path: str  # repository-relative, for example changelog.d/123.fixed.md
    number: int
    section: str
    index: int  # 1 for <N>.<section>.md, k for <N>.<section>.<k>.md
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
    if (
        len(pieces) not in (3, 4)
        or pieces[-1] != "md"
        or NUMBER.fullmatch(pieces[0]) is None
        or (len(pieces) == 4 and COUNTER.fullmatch(pieces[2]) is None)
    ):
        return (
            f"bad name; use {FRAGMENT_DIR}/<N>.<section>.md, or {FRAGMENT_DIR}/<N>.<section>.<k>.md for a "
            "second and later entry in the same section, where <N> is the pull request number and <k> "
            "counts from 2, both without leading zeros"
        )
    if pieces[1] not in SECTIONS:
        return f"unknown section '{pieces[1]}'; use one of: {section_names()}"
    return None


def name_number(path: str) -> int | None:
    """The pull request number a file name in changelog.d/ starts with, or None."""

    match = re.match(r"([0-9]+)\.", path.replace("\\", "/").rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None


def _outside_code_spans(text: str) -> str:
    """``text`` with every inline code span blanked out.

    A run of backticks opens a span that the next run of the same length
    closes; a run without a partner is literal text.
    """

    pieces: list[str] = []
    position = 0
    while True:
        opening = BACKTICKS.search(text, position)
        if opening is None:
            pieces.append(text[position:])
            return "".join(pieces)
        closing = next(
            (run for run in BACKTICKS.finditer(text, opening.end()) if len(run.group()) == len(opening.group())),
            None,
        )
        if closing is None:
            pieces.append(text[position:opening.end()])
            position = opening.end()
            continue
        pieces.append(text[position:opening.start()] + " ")
        position = closing.end()


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
    if RAW_HTML.search(_outside_code_spans("\n".join(normalized))):
        return (), (
            "contains raw HTML ('<' followed by a letter, '/', '!' or '?'); it can change the entries "
            "around it, so put that text in backticks"
        )
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
                "collector appends it from the file name) and mention another issue or pull request "
                "without parentheses, for example 'see #N'"
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
    pieces = path.replace("\\", "/").split("/", 1)[1].split(".")
    index = int(pieces[2]) if len(pieces) == 4 else 1
    return Fragment(
        path=path, number=int(pieces[0]), section=pieces[1], index=index, lines=text_lines(data)[0], data=data
    )


def render_bullet(fragment: Fragment) -> list[str]:
    lines = [f"- {fragment.lines[0]}", *(f"  {line}" for line in fragment.lines[1:])]
    last = lines[-1]
    if last.endswith("."):
        lines[-1] = f"{last[:-1]} (#{fragment.number})."
    else:
        lines[-1] = f"{last} (#{fragment.number})"
    return lines


def _normalized(lines: list[str]) -> str:
    return " ".join(" ".join(lines).split())


def section_bullets(section: str) -> dict[str, list[str]]:
    """Whole top-level bullets per ``### `` subsection of one release section.

    Each bullet is its ``- `` line joined with its indented continuation lines,
    whitespace normalized. HTML comments and fenced blocks are skipped, so text
    inside them is never a bullet.
    """

    text = COMMENT_SPAN.sub(lambda match: "\n" * match.group().count("\n"), section)
    bullets: dict[str, list[str]] = {}
    subsection = ""
    current: list[str] = []
    fenced = False

    def flush() -> None:
        if current:
            bullets.setdefault(subsection, []).append(_normalized(current))
            current.clear()

    for raw in text.split("\n"):
        if FENCE_LINE.match(raw):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if raw.startswith("### "):
            flush()
            subsection = raw[4:].strip()
        elif raw.startswith("- "):
            flush()
            current.append(raw)
        elif current and raw.startswith("  ") and raw.strip():
            current.append(raw)
        else:
            flush()
    flush()
    return bullets


def bullet_in_section(fragment: Fragment, section: str) -> bool:
    """True when the bullet ``fragment`` renders to is one whole bullet of its
    subsection in ``section``, compared after whitespace normalization."""

    return _normalized(render_bullet(fragment)) in section_bullets(section).get(fragment.heading, [])


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


def bullets_by_section(fragments: list[Fragment]) -> dict[str, list[list[str]]]:
    """Rendered bullets per section, ordered by pull request number, then counter."""

    result: dict[str, list[list[str]]] = {}
    for fragment in sorted(fragments, key=lambda item: (item.number, item.index, item.path)):
        result.setdefault(fragment.section, []).append(render_bullet(fragment))
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


def combined_view(root: Path, text: str) -> tuple[str, list[str], list[Fragment]]:
    """``text`` with every valid fragment under ``root`` merged into Unreleased.

    Returns the text, a note for each invalid fragment, and the fragments.
    """

    fragments, problems = load_fragments(root)
    notes = [f"invalid fragment {problem}" for problem in problems]
    return merge_into_unreleased(text, bullets_by_section(fragments)), notes, fragments


def combined_changelog(root: Path, text: str) -> tuple[str, list[str]]:
    """``combined_view`` without the fragments."""

    combined, notes, _fragments = combined_view(root, text)
    return combined, notes


def section_text(text: str, heading: str) -> str:
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line.strip() == heading.strip()), None)
    if start is None:
        return ""
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip("\n") + "\n"


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
    """Each fragment still on disk, whether its bullet is in ``released``, and what to do."""

    fragments, _problems = load_fragments(root)
    if not fragments:
        return []
    lines = [
        "These fragments are still in changelog.d/ (a previous collect may have been interrupted "
        "after writing CHANGELOG.md):"
    ]
    for fragment in fragments:
        where = "already in" if bullet_in_section(fragment, released) else "not in"
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


def _cleanup(root: Path, fragments: list[Fragment], heading: str, result: CollectResult) -> list[str]:
    """Delete each fragment whose bytes are still the ones read; describe the rest.

    Runs after ``CHANGELOG.md`` was written, so a failure on one file never
    stops the others.
    """

    left: list[str] = []
    for fragment in sorted(fragments, key=lambda item: item.path):
        path = root / fragment.path
        try:
            current = _read_regular(path)
            if current == fragment.data:
                _remove_fragment(path)
                result.consumed.append(fragment.path)
                continue
        except Exception as exc:  # reported per file: the changelog is already written
            if os.path.lexists(path):
                left.append(
                    f"  {fragment.path}: still present (could not check and delete it: "
                    f"{type(exc).__name__}: {exc}); its text is already in '{heading}'"
                )
            else:
                left.append(f"  {fragment.path}: gone after it was read")
            continue
        if current is None:
            left.append(f"  {fragment.path}: gone after it was read")
        else:
            left.append(
                f"  {fragment.path}: still present, changed after it was read; its new text is not in '{heading}'"
            )
    return left


def collect(root: Path, *, version: str, date: str) -> CollectResult:
    """Fold every fragment into ``CHANGELOG.md`` as the ``version`` section.

    Everything is checked before anything is written, and every fragment is
    read again just before the write: if one changed or disappeared, nothing is
    written. The changelog is written first (to a temporary file, then renamed
    over), then each fragment read in this run is deleted while its bytes are
    still the bytes that were read. A changed or missing one, or one that
    cannot be read or deleted, is left in place and reported with
    CleanupIncomplete after the others are done. That is two steps, not one
    atomic change: if the deletions are interrupted, the next run for the same
    version refuses and lists the fragments that are already in the section.
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
    if _unreleased_bounds(lines) is None and not fragments:
        raise CollectError(f"nothing to collect: no '{UNRELEASED_HEADING}' section and no fragments")

    merged = merge_into_unreleased(text, bullets_by_section(fragments)).split("\n")
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

    result = CollectResult(heading=heading, numbers={fragment.path: fragment.number for fragment in fragments})
    left = _cleanup(root, fragments, heading, result)
    if left:
        message = [
            f"Wrote '{heading}' to {CHANGELOG_PATH} and deleted {len(result.consumed)} fragment(s), but these "
            "were not deleted:",
            *left,
            RECOVERY_TEXT,
        ]
        raise CleanupIncomplete("\n".join(message), result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root (default: cwd).")
    commands = parser.add_subparsers(dest="command", required=True, metavar="{check,preview,collect}")
    commands.add_parser("check", help="Validate every fragment in changelog.d/.")
    commands.add_parser(
        "preview",
        help="Print the Unreleased section with every fragment merged in, each ending in (#N) from its file name.",
    )
    collect_parser = commands.add_parser(
        "collect",
        help="Write the fragments into CHANGELOG.md as a release section and delete them.",
    )
    collect_parser.add_argument("--version", required=True, help="Release version, X.Y.Z or X.Y.Z-pre.")
    collect_parser.add_argument("--date", required=True, help="Release date, YYYY-MM-DD.")
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
            combined, notes = combined_changelog(root, text)
        except (OSError, CollectError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        sys.stdout.write(section_text(combined, UNRELEASED_HEADING))
        for note in notes:
            print(f"note: {note}", file=sys.stderr)
        return 0

    try:
        result = collect(root, version=args.version, date=args.date)
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
