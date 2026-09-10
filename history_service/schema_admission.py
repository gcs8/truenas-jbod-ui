"""Private checked SQLite handles; lifecycle ownership lasts until close.

These checks detect persistent out-of-band replacements, not hostile rename/ABA
races by an actor ignoring the history lifecycle lock. SQLite must use its real
pathname for WAL visibility; /proc descriptor aliases and immutable are not used.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from contextlib import ExitStack
from typing import Any


class DatabaseIdentity:
    def __init__(self, path: Path):
        self.path = path.absolute()
        self.parent = self.path.parent.stat()
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.metadata = os.fstat(self.fd)
        self.sidecars = {}
        try:
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        try:
            current = self.path.lstat()
            parent = self.path.parent.stat()
            pinned = os.fstat(self.fd)
        except OSError as exc:
            raise ValueError("History database identity changed.") from exc
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or pinned.st_nlink != 1
                or (current.st_dev, current.st_ino) != (self.metadata.st_dev, self.metadata.st_ino)
                or (parent.st_dev, parent.st_ino) != (self.parent.st_dev, self.parent.st_ino)):
            raise ValueError("History database identity changed.")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                sidecar = Path(str(self.path) + suffix).lstat()
            except FileNotFoundError:
                if suffix in self.sidecars:
                    raise ValueError("History database sidecar identity changed.")
                continue
            if not stat.S_ISREG(sidecar.st_mode) or sidecar.st_nlink != 1:
                raise ValueError("History database sidecar identity is unsafe.")
            # Rollback journals normally disappear on every commit. WAL and SHM
            # identities must remain stable while a live handle owns this lock.
            if suffix != "-journal":
                identity = (sidecar.st_dev, sidecar.st_ino)
                if self.sidecars.setdefault(suffix, identity) != identity:
                    raise ValueError("History database sidecar identity changed.")

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class CheckedCursor:
    def __init__(self, cursor, owner):
        self._cursor = cursor
        self._owner = owner

    @property
    def connection(self):
        return self._owner

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._cursor, name)
        if not callable(value):
            return value
        def checked(*args, **kwargs):
            self._owner.check()
            result = value(*args, **kwargs)
            return self if result is self._cursor else result
        return checked

    def __iter__(self):
        return self

    def __next__(self):
        self._owner.check()
        return next(self._cursor)


class AdmittedConnection:
    def __init__(self, connection, identity, marker_check):
        self._connection = connection
        self._identity = identity
        self._marker_check = marker_check
        self._lifecycle: ExitStack | None = None
        self._closed = False

    def check(self):
        if self._closed:
            raise ValueError("History database connection is closed.")
        self._identity.check()
        self._marker_check()

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._connection, name)
        if not callable(value):
            return value
        def checked(*args, **kwargs):
            self.check()
            result = value(*args, **kwargs)
            if name in ("execute", "executemany", "executescript", "cursor"):
                return CheckedCursor(result, self)
            return result
        return checked

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.check()
            return self._connection.__exit__(exc_type, exc, tb)
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
        finally:
            self._identity.close()
            if self._lifecycle is not None:
                self._lifecycle.close()

    def __del__(self):
        self.close()
