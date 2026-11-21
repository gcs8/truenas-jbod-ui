"""Whole-file read budget for the application's JSON state files (#1286).

The slot detail cache, the SAS Fabric alias store and the mapping store are
small JSON files that a backup restores as the archive held them and that
the application then reads in one piece (as does a debug bundle that
includes them). Many small values cost far more as Python objects than as
text, so the file size alone does not bound what a read builds. Every read
of one of these files goes through this module, which refuses a file over
either limit while reading it, before anything is built. Backup validation
runs the same scan on the archive member, so a member the application would
refuse to load is refused at restore.

Files are read as bytes and the budget counts the bytes each read returns,
so multibyte text cannot pass a byte limit as characters, and a file is read
only to the size it had when opened: one that is not a regular file, or that
changes size while it is read, is refused like an unreadable file.

The item count is taken from the file's bytes as they are read: one plus the
number of commas, colons, ``{`` and ``[``. In valid JSON each key has
exactly one colon and a container with k > 0 children has k - 1 commas, so
this bounds the number of keys plus values from above; separators inside
strings only add to it.

Sizing: the largest slot detail entry the inventory writes (every stable
slot and SMART field, a two-path multipath block) is about 3 KB and 155
counted items in the stored format. Sixteen fully populated 1,024-disk
expanders of those (16,384 bays) come to about 49 MB and 2.5 million items;
the limits admit about 22,000. Under them a read builds at most a few
hundred MB even of the costliest values (about 300 MB for 4 million empty
objects), where before it was bounded only by the archive member limit.
"""

from __future__ import annotations

import os
import re
import stat
from itertools import accumulate
from typing import Any, IO

MAX_JSON_STATE_FILE_BYTES = 64 * 1024 * 1024
MAX_JSON_STATE_FILE_ITEMS = 4 * 1024 * 1024
JSON_STATE_READ_CHUNK_BYTES = 1024 * 1024
# Container nesting a state file may use. The files this application writes
# nest about eight levels; backup validation already refuses more than 64.
# Checked on the bytes before parsing, so no parser recursion limit (which
# depends on the interpreter and its stack) decides whether a file is read.
MAX_JSON_STATE_FILE_DEPTH = 128
_ITEM_MARKERS = (b",", b":", b"{", b"[")
_JSON_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.DOTALL)
_NOT_BRACKETS = bytes(byte for byte in range(256) if byte not in b"[]{}")
_BRACKET_STEP = tuple(1 if byte in b"[{" else -1 for byte in range(256))


class JsonStateFileRefused(ValueError):
    """A JSON state file was not read: over the budget, or not a stable file.

    ``name`` is what the caller called the file (a file name or an archive
    path, never a host path in public text); ``detail`` says why.
    """

    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name} is {detail} and was not read.")


class JsonStateFileTooLarge(JsonStateFileRefused):
    """A JSON state file is over the whole-file budget and was not read."""

    def __init__(self, name: str, limit: int, unit: str) -> None:
        self.limit = limit
        self.unit = unit
        super().__init__(name, f"over the limit of {limit:,} {unit} for a JSON state file")


class JsonStateFileChanged(JsonStateFileRefused):
    """The source is not a regular file, or not the size it had when opened."""


def _regular_size(source: Any, name: str) -> int | None:
    """The size of an open regular file; refuse any other kind of file.

    ``None`` when ``source`` has no descriptor (an in-memory buffer).
    """
    try:
        descriptor = source.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise JsonStateFileChanged(name, "not a regular file")
    return metadata.st_size


def _scan(source: IO[bytes], name: str, *, retain: bool, size: int | None) -> bytes | None:
    """Read ``source`` to its end under the budget; keep the chunks if ``retain``.

    The budget counts the encoded bytes each read returns, so a source must be
    binary. A source with a known size (the caller's own ``fstat``, else this
    one's) is read to exactly that size: a source that returns more or fewer
    bytes changed while it was read and is refused, never read past its
    checked size.
    """
    max_bytes = MAX_JSON_STATE_FILE_BYTES
    max_items = MAX_JSON_STATE_FILE_ITEMS
    if size is None:
        size = _regular_size(source, name)
    if size is not None and size > max_bytes:
        raise JsonStateFileTooLarge(name, max_bytes, "bytes")
    limit = max_bytes if size is None else size
    chunks: list[bytes] = []
    total = 0
    items = 1
    while True:
        chunk = source.read(min(JSON_STATE_READ_CHUNK_BYTES, limit + 1 - total))
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("A JSON state file is read as bytes, so its budget counts bytes.")
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            if size is None:
                raise JsonStateFileTooLarge(name, max_bytes, "bytes")
            raise JsonStateFileChanged(name, "larger than when it was opened")
        items += sum(chunk.count(marker) for marker in _ITEM_MARKERS)
        if items > max_items:
            raise JsonStateFileTooLarge(name, max_items, "keys and values")
        if retain:
            chunks.append(bytes(chunk))
    if size is not None and total != size:
        raise JsonStateFileChanged(name, "smaller than when it was opened")
    return b"".join(chunks) if retain else None


def _nesting_depth(data: bytes) -> int:
    """The deepest container nesting in ``data``; brackets in strings do not count."""
    brackets = _JSON_STRING.sub(b"", data).translate(None, _NOT_BRACKETS)
    return max(accumulate(map(_BRACKET_STEP.__getitem__, brackets)), default=0)


def read_json_state(source: IO[bytes], name: str, *, size: int | None = None) -> bytes:
    """Return all of binary ``source``, or raise ``JsonStateFileRefused``.

    ``size`` is the caller's own ``fstat`` size of a file it has already
    checked is regular; otherwise the size and kind are taken from ``source``.
    Either way the bytes read are counted too. A file nested deeper than
    ``MAX_JSON_STATE_FILE_DEPTH`` is refused before it is parsed.
    """
    data = _scan(source, name, retain=True, size=size)
    assert data is not None
    if _nesting_depth(data) > MAX_JSON_STATE_FILE_DEPTH:
        raise JsonStateFileTooLarge(name, MAX_JSON_STATE_FILE_DEPTH, "levels of nesting")
    return data


def check_json_state(source: IO[bytes], name: str, *, size: int | None = None) -> None:
    """Apply the same budget as ``read_json_state`` without keeping the data."""
    _scan(source, name, retain=False, size=size)


class BudgetedJsonSource:
    """An open binary state file handed to ``json.load`` through the budget.

    ``read()`` returns the whole file decoded as UTF-8, as a UTF-8 text-mode
    read would, or raises ``JsonStateFileRefused`` before anything is decoded
    or parsed. The budget counts the encoded bytes the handle returns.
    """

    def __init__(self, handle: IO[Any], name: str) -> None:
        self._handle = handle
        self._label = name

    @property
    def name(self) -> Any:
        return getattr(self._handle, "name", self._label)

    def read(self, _size: int = -1) -> str:
        return read_json_state(self._handle, self._label).decode("utf-8")
