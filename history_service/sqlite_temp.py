"""Where history processes put SQLite's temporary files (#1094).

Index builds, the startup schema upgrade and VACUUM spill sort runs and
transient databases to temporary files (#1092, #1093). Those files can be as
large as the tables they sort, so they belong on the history volume, not on a
memory-backed ``/tmp``. Without help, SQLite's Unix VFS picks the directory
from ``SQLITE_TMPDIR``, then ``TMPDIR``, then ``/var/tmp``, ``/usr/tmp``,
``/tmp`` and ``.``. An older or customised Compose file may not set ``TMPDIR``
for the history service, and a hardened container has a read-only root
filesystem and a tmpfs ``/tmp``, so the spill would land in RAM again.

History entry points therefore choose a private directory on the history
volume themselves, ``<history directory>/.sqlite-tmp``, before they open the
history database. An operator who sets ``SQLITE_TMPDIR`` keeps that choice;
``TMPDIR`` is ignored for this purpose.

CPython initialises SQLite when ``sqlite3`` is imported, and SQLite reads
``SQLITE_TMPDIR`` and ``TMPDIR`` once, at that moment. Changing the
environment afterwards has no effect on this process, so the directory is
applied through SQLite's process-wide ``temp_store_directory`` setting. SQLite
documents that setting as not thread-safe; set it at process start, before
any other thread uses SQLite.
"""

from __future__ import annotations

import errno
import logging
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from app.services.storage_writability import host_repair_path

logger = logging.getLogger(__name__)

# Dot-named so the backup exporter's non-dot ``*.sqlite3`` scans and the
# segment catalog never treat it as history data (#1094).
SQLITE_TEMPORARY_DIRECTORY_NAME = ".sqlite-tmp"
SQLITE_TEMPORARY_DIRECTORY_MODE = 0o700
# SQLite names every temporary file it creates ``etilqs_<random>``.
SQLITE_TEMPORARY_FILE_PREFIX = "etilqs_"
# SQLite's Unix temporary-directory search order after SQLITE_TMPDIR and TMPDIR.
SQLITE_FALLBACK_TEMP_DIRECTORIES = ("/var/tmp", "/usr/tmp", "/tmp", ".")
# Startup cleanup inspects at most this many entries; SQLite unlinks each
# temporary file right after opening it, so the directory is normally empty.
MAX_STALE_TEMPORARY_ENTRIES = 4096

_configured_lock = threading.Lock()
_configured_directory: Path | None = None
# Set when this process created the directory, for maintenance_sqlite_temporary_directory.
_created_directory: Path | None = None


def sqlite_temporary_directory_path(history_directory: str | os.PathLike[str]) -> Path:
    """``<history directory>/.sqlite-tmp`` as an absolute path."""

    return Path(os.path.abspath(os.fspath(history_directory))) / SQLITE_TEMPORARY_DIRECTORY_NAME


def operator_sqlite_temporary_directory() -> str | None:
    """The operator's explicit ``SQLITE_TMPDIR``, or None when it is unset or blank."""

    value = os.environ.get("SQLITE_TMPDIR")
    return value if value else None


def prepare_sqlite_temporary_directory(history_directory: str | os.PathLike[str]) -> Path:
    """Create or adopt ``<history directory>/.sqlite-tmp`` and empty it of stale files.

    The directory is a real directory (never a symlink), owned by this
    process's user, with exact mode 0700. Regular files SQLite left behind
    (``etilqs_*``, owned by this user) are removed; nothing else is touched.
    Raises OSError when the history directory cannot hold it (read-only,
    full, missing) and ValueError when the existing entry is not usable.
    """

    return _prepare(history_directory)[0]


def _prepare(history_directory: str | os.PathLike[str]) -> tuple[Path, bool]:
    target = sqlite_temporary_directory_path(history_directory)
    created = False
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    child_flags = parent_flags | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(target.parent, parent_flags)
    try:
        try:
            os.mkdir(SQLITE_TEMPORARY_DIRECTORY_NAME, SQLITE_TEMPORARY_DIRECTORY_MODE, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        try:
            descriptor = os.open(SQLITE_TEMPORARY_DIRECTORY_NAME, child_flags, dir_fd=parent_descriptor)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(f"{target} is not a directory.") from exc
            raise
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"{target} is not a directory.")
            effective_uid = os.geteuid()
            if metadata.st_uid != effective_uid:
                raise ValueError(
                    f"{target} is owned by uid {metadata.st_uid}, but this process runs as uid "
                    f"{effective_uid}."
                )
            if stat.S_IMODE(metadata.st_mode) != SQLITE_TEMPORARY_DIRECTORY_MODE:
                os.fchmod(descriptor, SQLITE_TEMPORARY_DIRECTORY_MODE)
            removed = _remove_stale_temporary_files(descriptor, effective_uid)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    if removed:
        logger.info("Removed %d stale SQLite temporary file(s) from %s.", removed, host_repair_path(target))
    return target, created


def _remove_stale_temporary_files(descriptor: int, owner_uid: int) -> int:
    removed = 0
    with os.scandir(descriptor) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > MAX_STALE_TEMPORARY_ENTRIES:
                break
            if not entry.name.startswith(SQLITE_TEMPORARY_FILE_PREFIX):
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner_uid:
                continue
            try:
                os.unlink(entry.name, dir_fd=descriptor)
            except FileNotFoundError:
                continue
            removed += 1
    return removed


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _apply_process_temporary_directory(directory: Path) -> bool:
    """Point SQLite's process-wide temporary directory at ``directory``; True if it took."""

    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute(f"PRAGMA temp_store_directory = {_sql_string_literal(str(directory))}")
            row = connection.execute("PRAGMA temp_store_directory").fetchone()
    except sqlite3.Error:
        logger.debug("SQLite refused temp_store_directory=%s.", directory, exc_info=True)
        return False
    # A build compiled without deprecated pragmas ignores both statements.
    return bool(row) and row[0] == str(directory)


def configure_sqlite_temporary_directory(history_directory: str | os.PathLike[str]) -> Path | None:
    """Send this process's SQLite temporary files to the history volume (#1094).

    Call it at process start, after the history directory exists and before
    the history database is opened or any other thread uses SQLite. Returns
    the directory now in use, or None when the operator's ``SQLITE_TMPDIR``
    wins or the directory cannot be used; in that case SQLite keeps its own
    search order and a warning names the fallback. Never raises.
    """

    global _configured_directory, _created_directory
    operator_choice = operator_sqlite_temporary_directory()
    if operator_choice is not None:
        logger.info(
            "SQLite temporary files follow SQLITE_TMPDIR=%s set by the operator.", operator_choice
        )
        return None
    if os.name != "posix":
        return None
    target = sqlite_temporary_directory_path(history_directory)
    with _configured_lock:
        try:
            prepared, created = _prepare(history_directory)
        except (OSError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            logger.warning(
                "Cannot use %s for SQLite temporary files (%s). They go to %s instead, which "
                "may be memory-backed; large index builds and upgrades then count against the "
                "container's memory.",
                host_repair_path(target),
                reason,
                _describe_directory(effective_sqlite_temporary_directory()),
            )
            return None
        if not _apply_process_temporary_directory(prepared):
            logger.warning(
                "This SQLite build does not accept temp_store_directory; SQLite temporary files "
                "go to %s. Set SQLITE_TMPDIR=%s in the service environment to keep them on the "
                "history volume.",
                _describe_directory(effective_sqlite_temporary_directory()),
                prepared,
            )
            return None
        _configured_directory = prepared
        _created_directory = prepared if created else None
    logger.info("SQLite temporary files go to %s.", host_repair_path(prepared))
    return prepared


@contextmanager
def maintenance_sqlite_temporary_directory(history_directory: str | os.PathLike[str]) -> Iterator[Path | None]:
    """``configure_sqlite_temporary_directory`` for a one-shot maintenance tool.

    If the tool fails (for example, it refuses a database it must not
    touch) and this call created the directory, the still-empty directory
    is removed again, so a refused run leaves the history directory as it
    found it.
    """

    directory = configure_sqlite_temporary_directory(history_directory)
    created = directory is not None and _created_directory == directory
    try:
        yield directory
    except BaseException:
        if created and directory is not None:
            try:
                os.rmdir(directory)
            except OSError:
                pass
        raise


def _describe_directory(directory: Path | None) -> str:
    return host_repair_path(directory) if directory is not None else "no writable directory"


def _usable_directory(candidate: str | os.PathLike[str] | None) -> Path | None:
    if not candidate:
        return None
    path = Path(candidate)
    try:
        if path.is_dir() and os.access(path, os.W_OK | os.X_OK):
            return path
    except OSError:
        return None
    return None


def effective_sqlite_temporary_directory() -> Path | None:
    """The directory SQLite's Unix VFS uses for temporary files, or None.

    Mirrors SQLite's own search: the directory configured here first, then
    SQLITE_TMPDIR, TMPDIR and the fixed fallbacks; the first existing
    directory this process can write wins. SQLite skips a configured
    directory that has since disappeared, and so does this.
    """

    candidates: list[str | os.PathLike[str] | None] = [
        _configured_directory,
        os.environ.get("SQLITE_TMPDIR"),
        os.environ.get("TMPDIR"),
    ]
    candidates.extend(SQLITE_FALLBACK_TEMP_DIRECTORIES)
    for candidate in candidates:
        usable = _usable_directory(candidate)
        if usable is not None:
            return usable
    return None
