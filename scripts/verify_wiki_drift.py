#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
REGULAR_FILE_MODES = {"100644", "100755"}


class WikiVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangedFile:
    path: str
    repository_sha256: str
    external_sha256: str


@dataclass(frozen=True)
class WikiVerificationResult:
    repository_commit: str
    external_wiki_commit: str
    compared_files: int
    missing_from_external: tuple[str, ...]
    extra_in_external: tuple[str, ...]
    changed: tuple[ChangedFile, ...]

    @property
    def matches(self) -> bool:
        return not (self.missing_from_external or self.extra_in_external or self.changed)

    def release_evidence(self) -> str:
        if not self.matches:
            raise ValueError("cannot create PASS evidence from wiki drift")
        return "; ".join(
            (
                "Wiki drift verification: PASS",
                f"Repository commit: {self.repository_commit}",
                f"External wiki commit: {self.external_wiki_commit}",
                f"Compared files: {self.compared_files}",
            )
        )


def _git(repository: Path, *arguments: str, error_prefix: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            env=environment,
        )
    except OSError as exc:
        raise WikiVerificationError(f"{error_prefix}: git is unavailable") from exc
    if completed.returncode != 0:
        raise WikiVerificationError(error_prefix)
    return completed.stdout


def _resolve_commit(repository: Path, commit: str, *, label: str) -> str:
    if FULL_COMMIT_RE.fullmatch(commit) is None:
        raise WikiVerificationError(
            f"{label} commit must be exactly 40 lowercase hexadecimal characters"
        )
    resolved = _git(
        repository,
        "rev-parse",
        "--verify",
        f"{commit}^{{commit}}",
        error_prefix=f"{label} commit is unavailable",
    ).decode("ascii", errors="strict").strip()
    if resolved != commit:
        raise WikiVerificationError(f"{label} commit did not resolve exactly")
    return resolved


def _require_repository_authority(
    repository: Path,
    repository_commit: str,
    *,
    authority: str,
    authority_label: str,
) -> None:
    authority_commit = _git(
        repository,
        "rev-parse",
        "--verify",
        f"{authority}^{{commit}}",
        error_prefix=f"{authority_label} is unavailable",
    ).decode("ascii", errors="strict").strip()
    if repository_commit != authority_commit:
        raise WikiVerificationError(
            f"repository commit does not match {authority_label} {authority_commit}"
        )


def _scoped_path(path: bytes, *, repository_tree: bool) -> bytes | None:
    if repository_tree:
        if path.startswith(b"wiki/images/"):
            return path[len(b"wiki/") :]
        if path.startswith(b"wiki/") and b"/" not in path[len(b"wiki/") :] and path.endswith(b".md"):
            return path[len(b"wiki/") :]
        return None
    if path.startswith(b"images/") or (b"/" not in path and path.endswith(b".md")):
        return path
    return None


def _read_scoped_tree(
    repository: Path,
    commit: str,
    *,
    repository_tree: bool,
    label: str,
) -> dict[str, bytes]:
    arguments = ["ls-tree", "-rz", "--full-tree", commit]
    if repository_tree:
        arguments.extend(("--", "wiki"))
    raw_entries = _git(
        repository,
        *arguments,
        error_prefix=f"could not read {label} tree",
    )
    files: dict[str, bytes] = {}
    page_count = 0
    for raw_entry in raw_entries.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode_bytes, object_type, object_id = metadata.split(b" ", 2)
        except ValueError as exc:
            raise WikiVerificationError(f"{label} tree contains a malformed entry") from exc
        scoped_path = _scoped_path(raw_path, repository_tree=repository_tree)
        if scoped_path is None:
            continue
        try:
            path = scoped_path.decode("utf-8", errors="strict")
            mode = mode_bytes.decode("ascii", errors="strict")
            git_type = object_type.decode("ascii", errors="strict")
            oid = object_id.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise WikiVerificationError(f"{label} tree contains an invalid path or entry") from exc
        if git_type != "blob" or mode not in REGULAR_FILE_MODES:
            raise WikiVerificationError(f"{label} tree contains a non-regular file: {path}")
        files[path] = _git(
            repository,
            "cat-file",
            "blob",
            oid,
            error_prefix=f"could not read {label} file: {path}",
        )
        if path.endswith(".md") and "/" not in path:
            page_count += 1
    if repository_tree and page_count == 0:
        raise WikiVerificationError(f"{label} tree contains no root Markdown pages")
    return files


@contextmanager
def _external_repository(source: str) -> Iterator[Path]:
    local_source = Path(source)
    if local_source.exists():
        if not local_source.is_dir():
            raise WikiVerificationError("external wiki source is unavailable")
        _git(
            local_source,
            "rev-parse",
            "--git-dir",
            error_prefix="external wiki source is not a Git repository",
        )
        yield local_source
        return
    if "://" not in source:
        raise WikiVerificationError("external wiki source is unavailable")
    with tempfile.TemporaryDirectory(prefix="wiki-drift-") as temporary_directory:
        checkout = Path(temporary_directory) / "wiki.git"
        try:
            completed = subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", source, str(checkout)],
                check=False,
                capture_output=True,
            )
        except OSError as exc:
            raise WikiVerificationError("external wiki source is unavailable") from exc
        if completed.returncode != 0:
            raise WikiVerificationError("external wiki source is unavailable")
        yield checkout


def verify_wiki_drift(
    *,
    repository: Path,
    repository_commit: str,
    wiki_source: str,
    external_wiki_commit: str,
    repository_authority: str = "HEAD",
    repository_authority_label: str = "repository source HEAD",
) -> WikiVerificationResult:
    resolved_repository_commit = _resolve_commit(
        repository,
        repository_commit,
        label="repository",
    )
    _require_repository_authority(
        repository,
        resolved_repository_commit,
        authority=repository_authority,
        authority_label=repository_authority_label,
    )
    repository_files = _read_scoped_tree(
        repository,
        resolved_repository_commit,
        repository_tree=True,
        label="repository wiki",
    )
    with _external_repository(wiki_source) as external_repository:
        resolved_external_commit = _resolve_commit(
            external_repository,
            external_wiki_commit,
            label="external wiki",
        )
        source_head = _git(
            external_repository,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            error_prefix="external wiki source HEAD is unavailable",
        ).decode("ascii", errors="strict").strip()
        if source_head != resolved_external_commit:
            raise WikiVerificationError(
                "external wiki commit does not match external wiki source HEAD"
            )
        external_files = _read_scoped_tree(
            external_repository,
            resolved_external_commit,
            repository_tree=False,
            label="external wiki",
        )

    repository_paths = set(repository_files)
    external_paths = set(external_files)
    shared_paths = repository_paths & external_paths
    changed = tuple(
        ChangedFile(
            path,
            hashlib.sha256(repository_files[path]).hexdigest(),
            hashlib.sha256(external_files[path]).hexdigest(),
        )
        for path in sorted(shared_paths)
        if repository_files[path] != external_files[path]
    )
    return WikiVerificationResult(
        repository_commit=resolved_repository_commit,
        external_wiki_commit=resolved_external_commit,
        compared_files=len(repository_paths | external_paths),
        missing_from_external=tuple(sorted(repository_paths - external_paths)),
        extra_in_external=tuple(sorted(external_paths - repository_paths)),
        changed=changed,
    )


def _print_result(result: WikiVerificationResult) -> None:
    print(f"Wiki drift verification: {'PASS' if result.matches else 'FAIL'}")
    print(f"Repository commit: {result.repository_commit}")
    print(f"External wiki commit: {result.external_wiki_commit}")
    print(f"Compared files: {result.compared_files}")
    for path in result.missing_from_external:
        print(f"Missing from external wiki: {path}")
    for path in result.extra_in_external:
        print(f"Extra in external wiki: {path}")
    for changed_file in result.changed:
        print(
            f"Changed: {changed_file.path} "
            f"(repository sha256={changed_file.repository_sha256}, "
            f"external sha256={changed_file.external_sha256})"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare committed repository wiki pages and images with an external wiki commit."
    )
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--wiki-source", required=True)
    parser.add_argument("--external-wiki-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_wiki_drift(
            repository=args.repository,
            repository_commit=args.repository_commit,
            wiki_source=args.wiki_source,
            external_wiki_commit=args.external_wiki_commit,
        )
    except (OSError, UnicodeError, WikiVerificationError) as exc:
        print(f"Wiki drift verification: ERROR: {exc}", file=sys.stderr)
        return 2
    _print_result(result)
    return 0 if result.matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
