"""One place to recognise and explain an unwritable data or history directory.

Docker creates a bind mount as root:root; the containers run as uid 10001, so
every write fails with EACCES until the host directory is chowned. The strings
here are what an operator reads, in the log and in the UI.
"""
from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

UNWRITABLE_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
    }
)

SAVE_DETAIL = "Could not save: the data folder is not writable by the app. See Troubleshooting."

# SQLite reports a read-only directory as a message with no errno, so the
# errno test alone misses exactly the startup failure this module explains.
# Only these two messages are permission failures; "database is locked" and a
# malformed image keep their own error.
SQLITE_UNWRITABLE_MESSAGES = (
    "attempt to write a readonly database",
    "unable to open database file",
)

# The operator runs the repair on the Docker host, where the container paths
# do not exist. Compose binds these host sources, so name those instead.
CONTAINER_BIND_SOURCES = (
    ("/app/backup-status", "./backup-status"),
    ("/app/config", "./config"),
    ("/app/data", "./data"),
    ("/app/history", "./history"),
    ("/app/logs", "./logs"),
)


def host_repair_path(directory: Path | str) -> str:
    """The path an operator types on the Docker host for `directory`."""
    text = Path(directory).as_posix()
    for container, host in CONTAINER_BIND_SOURCES:
        if text == container:
            return host
        if text.startswith(container + "/"):
            return host + text[len(container):]
    return str(directory)


def _running_identity() -> tuple[int | None, int | None]:
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    return (
        geteuid() if callable(geteuid) else None,
        getegid() if callable(getegid) else None,
    )


def describe_unwritable_directory(directory: Path | str) -> str:
    """Return the one line that tells an operator what to do about `directory`."""
    path = Path(directory)
    running_uid, running_gid = _running_identity()
    try:
        owner_uid: int | None = path.stat().st_uid
    except OSError:
        owner_uid = None

    owner = f"owned by uid {owner_uid}" if owner_uid is not None else "owner unknown"
    running = f"running as uid {running_uid}" if running_uid is not None else "running as this user"
    target = host_repair_path(path)
    if running_uid is None or running_gid is None:
        remedy = f"On the Docker host, give the app user write access to {target}."
    else:
        remedy = (
            f"On the Docker host run: sudo chown -R {running_uid}:{running_gid} {target}"
        )
    return f"Cannot write to {path} ({owner}, {running}). {remedy}"


class StorageDirectoryUnwritable(RuntimeError):
    """A save failed because its directory is not writable by this process."""

    error_code = "storage_directory_unwritable"
    public_detail = SAVE_DETAIL

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.operator_message = describe_unwritable_directory(self.directory)
        super().__init__(self.operator_message)


def is_unwritable_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno in UNWRITABLE_ERRNOS:
        return True
    if type(exc) is sqlite3.OperationalError:
        return str(exc).strip().lower() in SQLITE_UNWRITABLE_MESSAGES
    return False


def unwritable_directory_error(
    exc: BaseException,
    directory: Path | str,
) -> StorageDirectoryUnwritable | None:
    """Translate a permission-shaped OSError into the named error, or return None."""
    if not is_unwritable_error(exc):
        return None
    return StorageDirectoryUnwritable(directory)
