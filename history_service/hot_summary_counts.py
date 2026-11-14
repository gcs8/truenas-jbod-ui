"""Per-system row counts of the hot history database without a full rescan (#664).

The admin orphaned-history preview needs, for every system, the row count and
the greatest ``system_label`` of each history table. Counting the hot database
in full on every request reads all of ``metric_samples`` and ``slot_events``,
which are by far the largest tables.

Those two tables are append-only: ``AUTOINCREMENT`` ids only grow, collectors
never rewrite a row, and retention, rotation and purges only delete. The one
in-place rewrite (system adoption) runs through ``HistoryStore``, which clears
this cache. So their counts are kept per id chunk and brought up to date with:

* the rows above the cached high id, read by rowid range;
* the trigger-maintained ``history_table_counts`` total, which proves that the
  remaining cached chunks lost no rows. Cached chunk counts can only be too
  high (rows are deleted, never inserted below the high id), so when the sum of
  cached chunks plus new rows equals the exact total, every chunk is exact.
  When it is larger, chunks are recounted from the oldest id (where retention
  deletes) until the totals agree.

Anything unexpected (no counter row, a counter that disagrees with a full
count, a sequence that went backwards, a replaced file, a non-text id)
falls back to the full count, so results always match a full ``GROUP BY``.
``slot_state_current`` and ``metric_rollups`` are rewritten in place and are
counted in full on every call; they are much smaller.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from contextlib import contextmanager

# (table, summary key) in the order every summary reader visits them.
SUMMARY_TABLE_KEYS: tuple[tuple[str, str], ...] = (
    ("slot_state_current", "tracked_slots"),
    ("slot_events", "event_count"),
    ("metric_samples", "metric_sample_count"),
    ("metric_rollups", "metric_rollup_count"),
)
APPEND_ONLY_TABLES = frozenset({"slot_events", "metric_samples"})
DEFAULT_CHUNK_BITS = 14

# {system_id: (row_count, MAX(system_label), normally text or None)}
SystemCounts = dict[str, tuple[int, Any]]


class _Uncacheable(Exception):
    """The table cannot be counted incrementally; count it in full."""


def sqlite_order(value: Any) -> tuple[int, Any]:
    """Sort key matching SQLite's ordering: NULL, numbers, text, then blobs."""

    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        # Python orders str by code point, which is BINARY order on UTF-8.
        return (2, value)
    return (3, bytes(value))


def sqlite_max(left: Any, right: Any) -> Any:
    """SQLite ``MAX`` over two values, ignoring NULL as aggregate MAX does."""

    if left is None:
        return right
    if right is None:
        return left
    return left if sqlite_order(left) >= sqlite_order(right) else right


def _merge(target: SystemCounts, system_id: Any, count: int, label: Any) -> None:
    if not isinstance(system_id, str):
        raise _Uncacheable("non-text system id")
    previous = target.get(system_id)
    if previous is None:
        target[system_id] = (int(count), label)
    else:
        target[system_id] = (previous[0] + int(count), sqlite_max(previous[1], label))


@dataclass
class _TableCache:
    identity: tuple[int, int]
    high_id: int
    chunks: dict[int, SystemCounts] = field(default_factory=dict)


class HotSystemSummaryCounts:
    """Thread-safe cache of exact per-system counts for one hot database."""

    def __init__(self, *, chunk_bits: int = DEFAULT_CHUNK_BITS) -> None:
        if type(chunk_bits) is not int or not 4 <= chunk_bits <= 30:
            raise ValueError("chunk_bits must be an integer from 4 to 30")
        self.chunk_bits = chunk_bits
        self._lock = threading.Lock()
        self._tables: dict[tuple[str, str], _TableCache] = {}
        # Observable for tests and diagnostics.
        self.full_counts = 0
        self.chunk_recounts = 0

    def clear(self) -> None:
        with self._lock:
            self._tables.clear()

    def table_counts(
        self, connection: sqlite3.Connection, path: Path, *,
        opened_identity: tuple[int, int] | None,
    ) -> dict[str, SystemCounts]:
        """Return ``{table: {system_id: (count, max_label)}}`` for the summary tables.

        Every value equals ``SELECT system_id, COUNT(*), MAX(system_label)
        FROM <table> GROUP BY system_id`` on one read snapshot of
        ``connection``. The caller captures ``opened_identity`` before opening
        the connection, so an old connection cannot seed a replacement's cache.
        """

        key_path = str(Path(path).absolute())
        with self._lock, self._read_snapshot(connection):
            identity = opened_identity
            if identity is None or self.file_identity(path) != identity:
                # Restore/rotation may replace the path after connection open.
                # Answer from that connection, but neither use nor publish cache
                # entries for a file it may not have opened.
                for table_name in APPEND_ONLY_TABLES:
                    self._tables.pop((key_path, table_name), None)
                identity = None
            result: dict[str, SystemCounts] = {}
            for table_name, _key in SUMMARY_TABLE_KEYS:
                if table_name in APPEND_ONLY_TABLES and identity is not None:
                    try:
                        result[table_name] = self._incremental(
                            connection, (key_path, table_name), identity
                        )
                        continue
                    except _Uncacheable:
                        self._tables.pop((key_path, table_name), None)
                result[table_name] = self._full(connection, table_name)
            if identity is not None and self.file_identity(path) != identity:
                # The file was replaced while it was read; trust nothing cached.
                for table_name in APPEND_ONLY_TABLES:
                    self._tables.pop((key_path, table_name), None)
            return result

    @staticmethod
    def file_identity(path: Path) -> tuple[int, int] | None:
        """Capture the path's identity before opening its SQLite connection."""
        try:
            metadata = os.stat(path)
        except OSError:
            return None
        return int(metadata.st_dev), int(metadata.st_ino)

    @staticmethod
    @contextmanager
    def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
        # Every statement must see one snapshot, so the counter row agrees with
        # the rows it counts.
        if connection.in_transaction:
            yield
            return
        connection.execute("BEGIN")
        try:
            yield
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")

    def _full(self, connection: sqlite3.Connection, table_name: str) -> SystemCounts:
        self.full_counts += 1
        counts: SystemCounts = {}
        for system_id, label, count in connection.execute(
            f"""
            SELECT system_id, MAX(system_label), COUNT(*)
            FROM {table_name}
            GROUP BY system_id
            """
        ):
            counts[system_id] = (int(count), label)
        return counts

    def _chunk_rows(
        self, connection: sqlite3.Connection, table_name: str, low: int, high: int
    ) -> SystemCounts:
        counts: SystemCounts = {}
        # NOT INDEXED keeps the rowid range scan; a scope index would read the
        # whole index to group by system_id.
        for system_id, label, count in connection.execute(
            f"""
            SELECT system_id, MAX(system_label), COUNT(*)
            FROM {table_name} NOT INDEXED
            WHERE id BETWEEN ? AND ?
            GROUP BY system_id
            """,
            (low, high),
        ):
            _merge(counts, system_id, count, label)
        return counts

    def _chunk_bounds(self, chunk: int, high_id: int) -> tuple[int, int]:
        return chunk << self.chunk_bits, min(((chunk + 1) << self.chunk_bits) - 1, high_id)

    def _build(
        self, connection: sqlite3.Connection, table_name: str, identity: tuple[int, int],
        low: int | None, high: int | None,
    ) -> _TableCache:
        self.full_counts += 1
        cache = _TableCache(identity=identity, high_id=int(high or 0))
        if low is None or high is None:
            return cache
        next_id: int | None = int(low)
        while next_id is not None and next_id <= int(high):
            # Seek to the next live id, so sparse ids (a restored or edited
            # file) cost one query per non-empty chunk, never per empty one.
            chunk = next_id >> self.chunk_bits
            chunk_low, chunk_high = self._chunk_bounds(chunk, int(high))
            counts = self._chunk_rows(connection, table_name, chunk_low, chunk_high)
            if counts:
                cache.chunks[chunk] = counts
            following = connection.execute(
                f"SELECT MIN(id) FROM {table_name} WHERE id > ?", (chunk_high,)
            ).fetchone()[0]
            next_id = None if following is None else int(following)
        return cache

    @staticmethod
    def _total(chunks: dict[int, SystemCounts]) -> int:
        return sum(count for counts in chunks.values() for count, _label in counts.values())

    def _incremental(
        self, connection: sqlite3.Connection, cache_key: tuple[str, str], identity: tuple[int, int]
    ) -> SystemCounts:
        table_name = cache_key[1]
        try:
            counter = connection.execute(
                "SELECT row_count FROM history_table_counts WHERE table_name = ?", (table_name,)
            ).fetchone()
            sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (table_name,)
            ).fetchone()
        except sqlite3.OperationalError as exc:  # legacy file without the counter table
            raise _Uncacheable(str(exc)) from exc
        if counter is None or counter[0] is None:
            raise _Uncacheable("no tracked row count")
        # The counter proves nothing unless both triggers keep it current.
        triggers = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? AND name IN (?, ?)",
            (table_name, f"count_{table_name}_insert", f"count_{table_name}_delete"),
        ).fetchone()[0]
        if int(triggers) != 2:
            raise _Uncacheable("row count triggers missing")
        expected_total = int(counter[0])
        sequence_value = int(sequence[0]) if sequence is not None and sequence[0] is not None else 0
        high = connection.execute(f"SELECT MAX(id) FROM {table_name}").fetchone()[0]
        low = connection.execute(f"SELECT MIN(id) FROM {table_name}").fetchone()[0]

        cache = self._tables.get(cache_key)
        if cache is not None and (cache.identity != identity or sequence_value < cache.high_id):
            cache = None
        if cache is None:
            cache = self._build(connection, table_name, identity, low, high)
            if self._total(cache.chunks) != expected_total:
                # The counter disagrees with the rows; answer exactly, cache nothing.
                self._tables.pop(cache_key, None)
                return self._flatten(cache.chunks)
            self._tables[cache_key] = cache
            return self._flatten(cache.chunks)

        chunks = {chunk: dict(counts) for chunk, counts in cache.chunks.items()}
        if low is None:
            chunks.clear()
        else:
            # Chunks wholly below the oldest remaining id lost every row.
            first_live = int(low) >> self.chunk_bits
            for chunk in [chunk for chunk in chunks if chunk < first_live]:
                del chunks[chunk]
        new_high = max(cache.high_id, int(high or 0))
        added: dict[int, SystemCounts] = {}
        if new_high > cache.high_id:
            for chunk, system_id, label, count in connection.execute(
                f"""
                SELECT id >> ?, system_id, MAX(system_label), COUNT(*)
                FROM {table_name} NOT INDEXED
                WHERE id > ?
                GROUP BY 1, 2
                """,
                (self.chunk_bits, cache.high_id),
            ):
                _merge(added.setdefault(int(chunk), {}), system_id, count, label)

        excess = self._total(chunks) + self._total(added) - expected_total
        if excess:
            for chunk in sorted(chunks):
                if excess <= 0:
                    break
                cached_total = self._total({chunk: chunks[chunk]})
                actual = self._chunk_rows(connection, table_name, *self._chunk_bounds(chunk, cache.high_id))
                self.chunk_recounts += 1
                actual_total = self._total({chunk: actual})
                if actual_total > cached_total:
                    raise _Uncacheable("rows appeared below the cached high id")
                excess -= cached_total - actual_total
                if actual:
                    chunks[chunk] = actual
                else:
                    del chunks[chunk]
        if excess != 0:
            raise _Uncacheable("cached counts do not reconcile with the tracked total")

        for chunk, counts in added.items():
            target = chunks.setdefault(chunk, {})
            for system_id, (count, label) in counts.items():
                _merge(target, system_id, count, label)
        self._tables[cache_key] = _TableCache(identity=identity, high_id=new_high, chunks=chunks)
        return self._flatten(chunks)

    @staticmethod
    def _flatten(chunks: dict[int, SystemCounts]) -> SystemCounts:
        counts: SystemCounts = {}
        for chunk in sorted(chunks):
            for system_id, (count, label) in chunks[chunk].items():
                _merge(counts, system_id, count, label)
        return counts
