"""Whole-file read budget for the application's JSON state files (#1286).

The slot detail cache, the SAS Fabric alias store and the mapping store are
small JSON files that a backup restores as the archive held them and that the
application then reads in one piece. Many small values cost far more as Python
objects than as text, so the file size alone does not bound what a read
builds. Every read of one of these files goes through this module, which
refuses a file over either limit while reading it, before anything is built.
Backup validation runs the same scan on the archive member, so a member the
application would refuse to load is refused at restore.

The item count is taken from the file's text as it is read: one plus the number of commas,
colons, ``{`` and ``[``. In valid JSON each key has exactly one colon and a
container with k > 0 children has k - 1 commas, so this bounds the number of
keys plus values from above; separators inside strings only add to it.

Sizing: the largest slot detail entry the inventory writes (every stable slot
and SMART field, a two-path multipath block) is about 3 KB and 155 counted
items in the stored format. Sixteen fully populated 1,024-disk expanders of
those (16,384 bays) come to about 49 MB and 2.5 million items; the limits
admit about 22,000. Under them a read builds at most a few hundred MB even of
the costliest values (about 300 MB for 4 million empty objects), where before
it was bounded only by the archive member limit.
"""

from __future__ import annotations

import os
from typing import Any, AnyStr, IO

MAX_JSON_STATE_FILE_BYTES = 64 * 1024 * 1024
MAX_JSON_STATE_FILE_ITEMS = 4 * 1024 * 1024
JSON_STATE_READ_CHUNK_BYTES = 1024 * 1024
_ITEM_MARKERS = (b",", b":", b"{", b"[")
_TEXT_ITEM_MARKERS = (",", ":", "{", "[")


class JsonStateFileTooLarge(ValueError):
    """A JSON state file is over the whole-file budget and was not read.

    ``name`` is what the caller called the file (a file name or an archive
    path, never a host path in public text); ``detail`` names the limit.
    """

    def __init__(self, name: str, limit: int, unit: str) -> None:
        self.name = name
        self.limit = limit
        self.unit = unit
        self.detail = f"over the limit of {limit:,} {unit} for a JSON state file"
        super().__init__(f"{name} is {self.detail} and was not read.")


def _size_hint(source: Any) -> int | None:
    try:
        return os.fstat(source.fileno()).st_size
    except (AttributeError, OSError, ValueError):
        return None


def _scan(source: IO[AnyStr], name: str, *, retain: bool, size: int | None) -> AnyStr | None:
    """Read ``source`` to its end under the budget; keep the chunks if ``retain``.

    A text source is limited by characters while reading and by bytes through
    its file size. Markers are ASCII, so a UTF-8 file counts the same items as
    text and as bytes.
    """
    max_bytes = MAX_JSON_STATE_FILE_BYTES
    max_items = MAX_JSON_STATE_FILE_ITEMS
    if size is None:
        size = _size_hint(source)
    if size is not None and size > max_bytes:
        raise JsonStateFileTooLarge(name, max_bytes, "bytes")
    chunks: list[Any] = []
    total = 0
    items = 1
    while True:
        chunk = source.read(min(JSON_STATE_READ_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise JsonStateFileTooLarge(name, max_bytes, "bytes")
        markers: tuple[Any, ...] = _TEXT_ITEM_MARKERS if isinstance(chunk, str) else _ITEM_MARKERS
        items += sum(chunk.count(marker) for marker in markers)
        if items > max_items:
            raise JsonStateFileTooLarge(name, max_items, "keys and values")
        if retain:
            chunks.append(chunk)
    if not retain:
        return None
    if chunks and isinstance(chunks[0], str):
        return "".join(chunks)  # type: ignore[return-value]
    return b"".join(chunks)  # type: ignore[return-value]


def read_json_state(source: IO[AnyStr], name: str, *, size: int | None = None) -> AnyStr:
    """Return all of ``source``, or raise ``JsonStateFileTooLarge``.

    ``size`` is the caller's own ``fstat`` size, when it has one; otherwise it
    is taken from ``source``. Either way the read itself is bounded too.
    """
    data = _scan(source, name, retain=True, size=size)
    assert data is not None
    return data


def check_json_state(source: IO[Any], name: str, *, size: int | None = None) -> None:
    """Apply the same budget as ``read_json_state`` without keeping the data."""
    _scan(source, name, retain=False, size=size)


class BudgetedJsonSource:
    """An open state file handed to ``json.load`` through the budget.

    ``read()`` returns the whole file or raises ``JsonStateFileTooLarge``
    before anything is parsed.
    """

    def __init__(self, handle: IO[Any], name: str) -> None:
        self._handle = handle
        self._label = name

    @property
    def name(self) -> Any:
        return getattr(self._handle, "name", self._label)

    def read(self, _size: int = -1) -> Any:
        return read_json_state(self._handle, self._label)
