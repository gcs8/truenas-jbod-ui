from __future__ import annotations

import errno
import hashlib
import os
import socket
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - production containers are Linux
    raise RuntimeError("Segmented history migration locking requires POSIX flock support.") from exc

def history_lock_path(database_path: Path) -> Path:
    """Return the retired filesystem lock path for cleanup and compatibility checks."""
    return Path(f"{database_path}.migration.lock")


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


_mountinfo_cache: dict[str, frozenset[str]] = {}
_mountinfo_cache_lock = threading.Lock()


def _read_mountinfo_text() -> str:
    try:
        return Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("History migration locking requires Linux mountinfo.") from exc


def _parse_mountinfo_targets(text: str) -> frozenset[str]:
    targets: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4:
            targets.add(os.path.normpath(_decode_mountinfo_path(fields[4])))
    return frozenset(targets)


def _mount_point_targets() -> frozenset[str]:
    """Return every mount target, re-parsing mountinfo only when its text changed.

    Every connection takes the lock, and the mount check used to parse
    /proc/self/mountinfo each time. The text is still read on every check, so a
    file that becomes a mount point later (a bind mount, or a recreated file
    that reuses an inode) is never served a remembered verdict; only the parse
    of an unchanged mount table is skipped.
    """

    text = _read_mountinfo_text()
    with _mountinfo_cache_lock:
        targets = _mountinfo_cache.get(text)
    if targets is None:
        targets = _parse_mountinfo_targets(text)
        with _mountinfo_cache_lock:
            _mountinfo_cache.clear()
            _mountinfo_cache[text] = targets
    return targets


def _database_path_is_mount_point(database_path: Path) -> bool:
    target = os.path.normpath(str(Path(database_path).absolute()))
    return target in _mount_point_targets()


def _history_lock_address(database_path: Path) -> bytes:
    database_path = Path(database_path).absolute()
    canonical_parent = database_path.parent.resolve(strict=True)
    canonical_path = canonical_parent / database_path.name
    try:
        metadata = os.stat(database_path, follow_symlinks=False)
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if _database_path_is_mount_point(canonical_path):
            raise ValueError("History database file mount points are not supported; mount its parent directory.")
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("History database path must not be a symlink.")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("History database path must be a regular file.")
        if metadata.st_nlink != 1:
            raise ValueError("History database hard-link aliases are not supported.")
    digest = hashlib.sha256(os.fsencode(canonical_path)).hexdigest()[:40]
    return f"\0truenas-jbod-history-{digest}".encode("ascii")


def _history_lock_directory(database_path: Path) -> Path:
    return Path(database_path).absolute().parent.resolve(strict=True)


@contextmanager
def history_write_lock(database_path: Path, *, blocking: bool) -> Iterator[None]:
    address = _history_lock_address(database_path)
    lock_socket = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0),
    )
    directory_descriptor = -1
    try:
        while True:
            try:
                lock_socket.bind(address)
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                if not blocking:
                    raise sqlite3.OperationalError(
                        "History migration lock is held; write was not committed."
                    ) from exc
                time.sleep(0.05)
        directory_descriptor = os.open(
            _history_lock_directory(database_path),
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            raise ValueError("History database parent must be a directory.")
        directory_lock_operation = fcntl.LOCK_EX
        if not blocking:
            directory_lock_operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(directory_descriptor, directory_lock_operation)
        except BlockingIOError as exc:
            raise sqlite3.OperationalError(
                "History migration lock is held; write was not committed."
            ) from exc
        yield
    finally:
        if directory_descriptor >= 0:
            try:
                fcntl.flock(directory_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(directory_descriptor)
        lock_socket.close()
