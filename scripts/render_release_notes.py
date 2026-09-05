"""Print a CHANGELOG.md section's Highlights and Upgrade notes as release-body text.

Usage::

    python scripts/render_release_notes.py "## v0.22.3 - 2026-09-30" > notes.md
    gh release create v0.22.3 --generate-notes --notes-file notes.md

GitHub appends its generated, label-categorized pull request list (configured
in ``.github/release.yml``) after the ``--notes-file`` text, so the release body
becomes Highlights, Upgrade notes, then the categorized list. The target section
must contain ``### Highlights``; ``### Upgrade notes`` is included when present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CHANGELOG_PATH = Path("CHANGELOG.md")
HIGHLIGHTS = "### Highlights"
UPGRADE_NOTES = "### Upgrade notes"


class RenderError(Exception):
    """Raised when the section cannot be rendered."""


def section_body(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    wanted = header.strip()
    start = next((index for index, line in enumerate(lines) if line.strip() == wanted), None)
    if start is None:
        raise RenderError(f"no section header {header!r} in CHANGELOG.md")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def subsection_body(section: list[str], heading: str) -> list[str] | None:
    start = next((index for index, line in enumerate(section) if line.strip() == heading), None)
    if start is None:
        return None
    body: list[str] = []
    for line in section[start + 1 :]:
        if line.startswith("### "):
            break
        body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return body


def render(text: str, header: str) -> str:
    section = section_body(text, header)
    highlights = subsection_body(section, HIGHLIGHTS)
    if not highlights:
        raise RenderError(f"{header!r} has no non-empty {HIGHLIGHTS!r} subsection")
    parts = ["## Highlights", "", *highlights, ""]
    upgrade = subsection_body(section, UPGRADE_NOTES)
    if upgrade:
        parts.extend(["## Upgrade notes", "", *upgrade, ""])
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("section_header", help='Exact section header, for example "## v0.22.3 - 2026-09-30".')
    parser.add_argument("--changelog", type=Path, default=CHANGELOG_PATH, help="Path to CHANGELOG.md.")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(render(args.changelog.read_text(encoding="utf-8"), args.section_header))
    except (RenderError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
