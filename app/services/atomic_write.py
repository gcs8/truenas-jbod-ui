"""Replace a small file through a temporary file that only this writer owns.

Several stores used to stage writes at one fixed sibling path
(``file_path.with_suffix(".tmp")``). Store instances do not share a lock, so
two writers to the same file could stage into the same temporary path, and the
second ``replace()`` then failed with ``FileNotFoundError`` because the first
had already renamed the shared temporary file into place.

``write_atomically`` creates a uniquely named temporary file in the target's
directory with ``O_CREAT | O_EXCL`` (``open(mode="x")``), keeps the target's
existing permission bits, fsyncs it, and publishes it with ``replace()``. The
temporary file is removed on every failure path, including a commit guard
that declines publication. Exceptions from the write or the publication reach
the caller unchanged so each store keeps its own error mapping.
"""
from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import IO, Any

TEMP_SUFFIX = ".tmp"
_CREATE_ATTEMPTS = 128


def temporary_path_for(path: Path) -> Path:
    """Return a fresh unique staging path beside ``path``.

    The name still ends in ``.tmp`` so existing ignore rules (key listing,
    ``.gitignore`` and the non-root stale-artifact check) keep matching it.
    """

    return path.parent / f"{path.name}.{secrets.token_hex(16)}{TEMP_SUFFIX}"


def _existing_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def _create_temporary(path: Path, open_mode: str, open_kwargs: dict[str, Any]) -> tuple[Path, IO[Any]]:
    for _attempt in range(_CREATE_ATTEMPTS):
        candidate = temporary_path_for(path)
        try:
            return candidate, candidate.open(open_mode, **open_kwargs)
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create a unique temporary file beside {path.name}.")


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Never mask the original failure with a cleanup failure.
        pass


def write_atomically(
    path: Path,
    write: Callable[[IO[Any]], object],
    *,
    binary: bool = False,
    encoding: str = "utf-8",
    newline: str | None = None,
    commit_guard: Callable[[], AbstractContextManager[bool]] | None = None,
) -> bool:
    """Write ``path`` through a private temporary file and replace it.

    ``write`` receives the open temporary handle. When ``commit_guard`` is
    given, the replace happens inside it and only when it yields ``True``;
    otherwise the temporary file is discarded and ``False`` is returned.

    A new file gets the same mode an ordinary ``open()`` would give it. An
    existing file keeps its permission bits: the replacement is set to the
    old mode before it is published. Hosts that refuse ``chmod`` on the
    temporary file keep the ordinary new-file mode, as before this helper.
    """

    path = Path(path)
    if binary:
        open_mode, open_kwargs = "xb", {}
    else:
        open_mode, open_kwargs = "x", {"encoding": encoding, "newline": newline}
    preserved_mode = _existing_mode(path)
    temp_path, handle = _create_temporary(path, open_mode, open_kwargs)
    try:
        with handle:
            if preserved_mode is not None and hasattr(os, "fchmod"):
                try:
                    os.fchmod(handle.fileno(), preserved_mode)
                except OSError:
                    pass
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        with commit_guard() if commit_guard is not None else nullcontext(True) as valid:
            if valid:
                temp_path.replace(path)
                return True
        _remove_quietly(temp_path)
        return False
    except BaseException:
        _remove_quietly(temp_path)
        raise
