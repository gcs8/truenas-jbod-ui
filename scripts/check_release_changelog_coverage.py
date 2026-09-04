"""Release gate: every pull request merged since a tag has a ``(#N)`` line.

Usage::

    python scripts/check_release_changelog_coverage.py v0.22.2 "## Unreleased" \
        --merged-prs-json merged.json --wiki-commit <sha>

Merged pull request numbers come from two sources, and the gate takes their
union because neither is complete on its own:

* ``git log <tag>..HEAD --format=%s`` -- squash subjects that end in ``(#N)``
  and merge commits whose subject starts ``Merge pull request #N``.
* ``--merged-prs-json <file>`` -- the release operator produces it with
  ``gh pr list -R <owner>/<repo> --state merged --search "merged:>=<tag date>"
  --limit 1000 --json number,mergedAt,labels,mergeCommit`` (either a JSON list
  of objects with a ``number`` field or a bare list of integers). Object entries
  whose merge commit is outside ``<tag>..HEAD`` are ignored. This ancestry
  filter includes pull requests that reached the candidate through an
  intermediate branch without including unrelated branch merges. Squash
  subjects lose the number when a contributor edits the merge title, so this
  file is the safety net.

Numbers whose commit already sits at or below the tag are dropped, so a JSON
list built from a same-day date filter does not fail the gate for pull requests
that shipped in the previous release.

Every collected number must appear as ``(#N)`` (alone or inside a longer
parenthetical such as ``(#12, #13, and #14)``) somewhere in the target
``CHANGELOG.md`` section. Missing numbers are printed and the gate exits 1.

When ``wiki/`` changed since the tag, the gate also requires ``--wiki-commit
<sha>`` and confirms with ``git ls-remote`` that the sha is a branch tip of the
GitHub wiki repository. ``--offline`` can inspect the candidate but cannot pass
that release gate or emit publish evidence. Nothing is cloned.
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
WIKI_DIRECTORY = "wiki/"

SQUASH_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")
MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) from ")
PARENTHETICAL = re.compile(r"\(([^()]*)\)")
PR_TOKEN = re.compile(r"(?<![\w#])#(\d+)(?!\d)")
SHA_TOKEN = re.compile(r"^[0-9a-f]{7,40}$")
CHANGELOG_SKIP_LABELS = {"no-changelog", "dependencies"}


class CoverageError(Exception):
    """Raised when the gate cannot evaluate the repository."""


@dataclass
class CoverageResult:
    ok: bool
    covered: set[int] = field(default_factory=set)
    missing: set[int] = field(default_factory=set)
    wiki_changed: bool = False
    wiki_commit: str | None = None
    messages: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise CoverageError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed


def pr_numbers_from_subjects(subjects: list[str]) -> set[int]:
    numbers: set[int] = set()
    for subject in subjects:
        squash = SQUASH_SUBJECT.search(subject)
        if squash:
            numbers.add(int(squash.group(1)))
            continue
        merge = MERGE_SUBJECT.match(subject)
        if merge:
            numbers.add(int(merge.group(1)))
    return numbers


def merged_pr_numbers_from_git(repo: Path, previous_tag: str, head: str = "HEAD") -> set[int]:
    output = _git(repo, "log", f"{previous_tag}..{head}", "--format=%s").stdout
    return pr_numbers_from_subjects(output.splitlines())


def released_pr_numbers(repo: Path, previous_tag: str) -> set[int]:
    """Numbers already named by commits at or below ``previous_tag``."""

    output = _git(repo, "log", previous_tag, "--format=%s").stdout
    return pr_numbers_from_subjects(output.splitlines())


def tag_commit_time(repo: Path, previous_tag: str) -> str:
    return _git(repo, "log", "-1", "--format=%cI", previous_tag).stdout.strip()


def commit_is_in_range(repo: Path, commit: str, previous_tag: str, head: str) -> bool:
    """Return whether ``commit`` exists in the candidate range ``tag..head``."""

    exists = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    if exists.returncode != 0:
        return False

    def is_ancestor(descendant: str) -> bool:
        completed = _git(repo, "merge-base", "--is-ancestor", commit, descendant, check=False)
        if completed.returncode in {0, 1}:
            return completed.returncode == 0
        raise CoverageError(
            f"git merge-base --is-ancestor {commit} {descendant} failed: {completed.stderr.strip()}"
        )

    return is_ancestor(head) and not is_ancestor(previous_tag)


def pr_number_sets_from_json(
    path: Path,
    *,
    repo: Path | None = None,
    previous_tag: str | None = None,
    head: str = "HEAD",
    not_after: str | None = None,
) -> tuple[set[int], set[int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise CoverageError(f"{path} must hold a JSON list")
    numbers: set[int] = set()
    excluded: set[int] = set()
    for item in payload:
        if isinstance(item, bool):
            raise CoverageError(f"{path} holds a boolean where a PR number was expected")
        if isinstance(item, int):
            numbers.add(item)
            continue
        if not isinstance(item, dict) or "number" not in item:
            raise CoverageError(f"{path} entries must be integers or objects with a 'number' field")
        raw_labels = item.get("labels", [])
        if not isinstance(raw_labels, list):
            raise CoverageError(f"{path} entry labels must be a JSON list")
        label_names: set[str] = set()
        for label in raw_labels:
            if isinstance(label, str):
                label_names.add(label)
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                label_names.add(label["name"])
            else:
                raise CoverageError(f"{path} entry labels must be strings or objects with a 'name' field")
        if label_names & CHANGELOG_SKIP_LABELS:
            excluded.add(int(item["number"]))
            continue
        merged_at = item.get("mergedAt")
        if not_after and isinstance(merged_at, str) and _iso_key(merged_at) <= _iso_key(not_after):
            continue
        merge_commit = item.get("mergeCommit")
        if merge_commit is not None:
            if not isinstance(merge_commit, dict) or not isinstance(merge_commit.get("oid"), str):
                raise CoverageError(f"{path} entry mergeCommit must be null or an object with an 'oid' field")
            if repo is None or previous_tag is None:
                raise CoverageError("mergeCommit ancestry filtering requires a repository and previous tag")
            if not commit_is_in_range(repo, merge_commit["oid"], previous_tag, head):
                continue
        numbers.add(int(item["number"]))
    return numbers, excluded


def _iso_key(value: str) -> str:
    """Comparable UTC key for ISO-8601 timestamps with 'Z' or an offset."""

    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def changelog_section(text: str, header: str) -> str:
    lines = text.splitlines()
    wanted = header.strip()
    start = next((index for index, line in enumerate(lines) if line.strip() == wanted), None)
    if start is None:
        raise CoverageError(f"{CHANGELOG_PATH} has no section header {header!r}")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def pr_numbers_in_section(section: str) -> set[int]:
    numbers: set[int] = set()
    for group in PARENTHETICAL.finditer(" ".join(section.split())):
        for token in PR_TOKEN.finditer(group.group(1)):
            numbers.add(int(token.group(1)))
    return numbers


def wiki_changed_since(repo: Path, previous_tag: str, head: str = "HEAD") -> bool:
    completed = _git(repo, "diff", "--quiet", f"{previous_tag}..{head}", "--", WIKI_DIRECTORY, check=False)
    if completed.returncode == 0:
        return False
    if completed.returncode == 1:
        return True
    raise CoverageError(f"git diff --quiet failed: {completed.stderr.strip()}")


def default_wiki_remote(repo: Path) -> str:
    origin = _git(repo, "remote", "get-url", "origin").stdout.strip()
    if origin.endswith(".git"):
        origin = origin[:-4]
    return origin + ".wiki.git"


def wiki_commit_is_published(repo: Path, remote: str, sha: str) -> bool:
    completed = _git(repo, "ls-remote", remote, check=False)
    if completed.returncode != 0:
        raise CoverageError(f"git ls-remote {remote} failed: {completed.stderr.strip()}")
    tips = {line.split()[0] for line in completed.stdout.splitlines() if line.strip()}
    return any(tip.startswith(sha) for tip in tips)


def evaluate(
    repo: Path,
    *,
    previous_tag: str,
    section_header: str,
    head: str = "HEAD",
    merged_prs_json: Path | None = None,
    wiki_commit: str | None = None,
    offline: bool = False,
    wiki_remote: str | None = None,
    changelog_path: Path | None = None,
) -> CoverageResult:
    result = CoverageResult(ok=True)

    expected = merged_pr_numbers_from_git(repo, previous_tag, head)
    if merged_prs_json is not None:
        json_numbers, excluded = pr_number_sets_from_json(
            merged_prs_json,
            repo=repo,
            previous_tag=previous_tag,
            head=head,
            not_after=tag_commit_time(repo, previous_tag),
        )
        expected |= json_numbers
        expected -= excluded
    expected -= released_pr_numbers(repo, previous_tag)

    changelog = (changelog_path or repo / CHANGELOG_PATH).read_text(encoding="utf-8")
    present = pr_numbers_in_section(changelog_section(changelog, section_header))
    result.covered = expected & present
    result.missing = expected - present
    if result.missing:
        result.ok = False
        result.messages.append(
            f"Changelog coverage: FAIL ({len(result.missing)} of {len(expected)} PRs missing from "
            f"{section_header!r}): " + ", ".join(f"#{number}" for number in sorted(result.missing))
        )
        result.messages.append(
            f"Add one '- ... (#N)' line per missing pull request to {CHANGELOG_PATH} under "
            f"{section_header!r}, or apply an approved changelog escape label before release."
        )
    else:
        result.messages.append(f"Changelog coverage: pass ({len(expected)} PRs)")

    result.wiki_changed = wiki_changed_since(repo, previous_tag, head)
    if result.wiki_changed:
        if not wiki_commit:
            result.ok = False
            result.messages.append(
                f"{WIKI_DIRECTORY} changed since {previous_tag}; pass --wiki-commit <sha> with the "
                "commit the GitHub wiki repository now points at after publishing wiki/."
            )
        elif not SHA_TOKEN.match(wiki_commit.lower()):
            result.ok = False
            result.messages.append("--wiki-commit must be a 7 to 40 character hexadecimal sha.")
        else:
            sha = wiki_commit.lower()
            if offline:
                result.ok = False
                result.messages.append(f"External wiki commit candidate: {sha} (not verified: --offline)")
            else:
                remote = wiki_remote or default_wiki_remote(repo)
                if wiki_commit_is_published(repo, remote, sha):
                    result.wiki_commit = sha
                    result.messages.append(f"External wiki commit: {sha} (branch tip on {remote})")
                else:
                    result.ok = False
                    result.messages.append(
                        f"External wiki commit {sha} is not a branch tip on {remote}; publish wiki/ "
                        "first and pass the resulting commit."
                    )
    else:
        result.messages.append(f"{WIKI_DIRECTORY} unchanged since {previous_tag}; no wiki commit required.")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("previous_tag", help="Previous release tag, for example v0.22.2.")
    parser.add_argument(
        "section_header",
        help='Exact CHANGELOG.md section header, for example "## Unreleased" or "## v0.22.3 - 2026-09-30".',
    )
    parser.add_argument("--head", default="HEAD", help="Release candidate ref (default: HEAD).")
    parser.add_argument(
        "--merged-prs-json",
        type=Path,
        help="JSON list from gh pr list --state merged ... --json number,mergedAt,labels,mergeCommit.",
    )
    parser.add_argument("--wiki-commit", help="Published GitHub wiki commit sha, required when wiki/ changed.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Inspect without git ls-remote; cannot pass when wiki publication evidence is required.",
    )
    parser.add_argument("--wiki-remote", help="Wiki repository URL (default: origin URL with .wiki.git).")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root (default: cwd).")
    args = parser.parse_args(argv)

    try:
        result = evaluate(
            args.repo,
            previous_tag=args.previous_tag,
            section_header=args.section_header,
            head=args.head,
            merged_prs_json=args.merged_prs_json,
            wiki_commit=args.wiki_commit,
            offline=args.offline,
            wiki_remote=args.wiki_remote,
        )
    except (CoverageError, OSError, ValueError) as exc:
        print(str(exc))
        return 2

    for message in result.messages:
        print(message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
