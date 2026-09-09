"""Startup checks that turn a silent permission problem into one plain sentence.

The published Compose files run the main UI and the history service as a
non-root user, but nothing prepares the bind-mounted folders. When Docker or an
older release created them as root, the services either crash before serving a
byte or start up looking healthy while every save fails. These helpers probe
each folder once at startup and describe the failure in words a home NAS owner
can act on.
"""

from __future__ import annotations

import errno
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

UNWRITABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def is_unwritable_path_error(exc: BaseException) -> bool:
    """Return True when an OSError means a folder refused the write.

    Network failures are OSError subclasses too, so they are excluded: a
    connection reset must keep surfacing as what it is.
    """

    if not isinstance(exc, OSError) or isinstance(exc, (ConnectionError, TimeoutError)):
        return False
    return isinstance(exc, PermissionError) or exc.errno in UNWRITABLE_ERRNOS


def current_process_ids() -> tuple[int, int] | None:
    """Return (uid, gid) of this process, or None where the platform has no such idea."""

    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    if geteuid is None or getegid is None:
        return None
    return int(geteuid()), int(getegid())


def owner_uid_of(path: Path) -> int | None:
    """Return the uid that owns ``path`` or the nearest existing parent."""

    for candidate in (path, *path.parents):
        try:
            return int(candidate.stat().st_uid)
        except OSError:
            continue
    return None


def describe_unwritable_dir(
    location: str,
    error: OSError,
    *,
    owner_uid: int | None,
    process_ids: tuple[int, int] | None,
) -> str:
    """Build the one sentence an operator needs to fix an unwritable folder.

    ``location`` is the folder exactly as configured, so the sentence names the
    same path the operator sees in docker-compose.yml.
    """

    folder = os.path.basename(location.rstrip("/\\")) or location
    if error.errno == errno.EROFS:
        return (
            f"Cannot write to {location} because it is mounted read-only. "
            f"Check the volumes line for {folder} in docker-compose.yml on the Docker host."
        )
    if process_ids is not None:
        uid, gid = process_ids
        owner_text = f"owned by uid {owner_uid}, " if owner_uid is not None else ""
        return (
            f"Cannot write to {location} ({owner_text}running as uid {uid}). "
            f"On the Docker host run: sudo chown -R {uid}:{gid} {folder}"
        )
    reason = error.strerror or type(error).__name__
    return (
        f"Cannot write to {location} ({reason}). "
        f"Make sure {folder} exists and is writable by the app."
    )


def check_writable_dirs(paths: Iterable[str | os.PathLike[str] | None]) -> list[str]:
    """Try to create and delete a probe file in each folder; return one sentence per failure."""

    problems: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        if not raw_path:
            continue
        location = os.fspath(raw_path)
        directory = Path(location)
        key = os.path.normcase(os.path.abspath(location))
        if key in seen:
            continue
        seen.add(key)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, b"probe")
            finally:
                os.close(descriptor)
            probe.unlink()
        except OSError as exc:
            problems.append(
                describe_unwritable_dir(
                    location,
                    exc,
                    owner_uid=owner_uid_of(directory),
                    process_ids=current_process_ids(),
                )
            )
    return problems


def ui_writable_directories(settings: Any) -> list[str]:
    """Folders the main UI must be able to write: the data folder and the logs folder.

    The config folder is left out on purpose: the main UI only reads
    ``profiles.yaml`` and ``config.yaml``, and the published Compose files mount
    it read-only.
    """

    paths = settings.paths
    candidates = [
        os.path.dirname(paths.mapping_file),
        os.path.dirname(paths.sas_fabric_alias_file),
        os.path.dirname(paths.slot_detail_cache_file),
        os.path.dirname(paths.log_file),
    ]
    known_hosts_path = getattr(getattr(settings, "ssh", None), "known_hosts_path", None)
    if known_hosts_path:
        candidates.append(os.path.dirname(known_hosts_path))
    return _unique_paths(candidates)


def history_writable_directories(settings: Any) -> list[str]:
    """Folders the history service must be able to write: the database folder and its backups."""

    candidates = [os.path.dirname(settings.sqlite_path), settings.backup_dir]
    long_term_backup_dir = getattr(settings, "long_term_backup_dir", None)
    if long_term_backup_dir:
        candidates.append(long_term_backup_dir)
    return _unique_paths(candidates)


def _unique_paths(candidates: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        if not text:
            continue
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered
