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

# Every history write lock used to resolve the path, stat it, and read and parse
# /proc/self/mountinfo again, which a slot bundle paid fifteen times over (#457).
# The validated address is cached on the file identity (device, inode, link
# count, mode) rather than on mtime: the live database is written constantly, so
# an mtime key would never hit, while every rejection this function raises
# depends on the identity, which changes when the file is replaced or mounted
# over.
LOCK_ADDRESS_CACHE_MAX_ENTRIES = 64
_lock_address_cache: dict[tuple[str, int, int, int, int], bytes] = {}
_lock_address_cache_lock = threading.Lock()


def clear_lock_address_cache() -> None:
    """Drop every cached lock address (used by tests and lifecycle changes)."""

    with _lock_address_cache_lock:
        _lock_address_cache.clear()


def lock_address_cache_size() -> int:
    with _lock_address_cache_lock:
        return len(_lock_address_cache)


def _decode_mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _database_path_is_mount_point(database_path: Path) -> bool:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("History migration locking requires Linux mountinfo.") from exc
    target = os.path.normpath(str(Path(database_path).absolute()))
    for line in lines:
        fields = line.split()
        if len(fields) > 4 and os.path.normpath(_decode_mountinfo_path(fields[4])) == target:
            return True
    return False


def _history_lock_address(database_path: Path) -> bytes:
    database_path = Path(database_path).absolute()
    try:
        metadata = os.stat(database_path, follow_symlinks=False)
    except FileNotFoundError:
        metadata = None
    cache_key: tuple[str, int, int, int, int] | None = None
    if metadata is not None:
        cache_key = (
            str(database_path),
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_nlink),
            int(metadata.st_mode),
        )
        with _lock_address_cache_lock:
            cached = _lock_address_cache.get(cache_key)
        if cached is not None:
            return cached
    canonical_parent = database_path.parent.resolve(strict=True)
    canonical_path = canonical_parent / database_path.name
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
    address = f"\0truenas-jbod-history-{digest}".encode("ascii")
    if cache_key is not None:
        with _lock_address_cache_lock:
            if len(_lock_address_cache) >= LOCK_ADDRESS_CACHE_MAX_ENTRIES:
                _lock_address_cache.clear()
            _lock_address_cache[cache_key] = address
    return address


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
