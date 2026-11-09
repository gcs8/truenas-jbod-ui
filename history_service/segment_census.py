"""Read-only census and candidate validator for older history segments (#864).

Segments sealed before #875 keep their exact bytes and lack the released
chronological indexes, so tiny limited reads of them still scan and sort. A
later change may convert them into an indexed copy kept under the same segment
ID. This module is the read-only first step:

* :class:`SegmentCensus` authenticates every catalogued segment against its
  existing catalog entry (size, SHA-256 and a pinned file identity) and counts
  which ones already have the indexes their columns allow. The result is
  cached; a health request only stats files.
* :func:`validate_conversion_candidate` compares a supplied candidate copy with
  its catalog-authenticated original.

Nothing here writes. It never constructs a ``HistoryStore`` (its constructor
creates and repairs files), never creates staging or state files and never
repairs permissions. SQLite files are opened through a pinned descriptor with
``mode=ro&immutable=1``, so no journal or WAL sidecar can appear.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import string
import struct
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from history_service.segment_catalog import (
    MAX_HISTORY_SEGMENT_BYTES,
    MIGRATION_PENDING_MARKER,
    SEGMENT_ID_PATTERN,
    activation_pending_path,
    path_entry_exists,
)
from history_service.segment_sealer import HISTORY_TABLE_TIMESTAMPS
from history_service.store import CHRONOLOGICAL_INDEXES

logger = logging.getLogger(__name__)

CENSUS_STATE = "census"
HASH_CHUNK_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_REPORTED_ERRORS = 20
DEFAULT_MIN_CENSUS_INTERVAL_SECONDS = 300.0
_SHA256_HEX = frozenset(string.hexdigits.lower())

# Columns each released chronological index needs. A legacy segment whose
# metric_samples table predates disk_identity_key is not expected to gain the
# two disk indexes. tests.test_segment_census pins this map to
# CHRONOLOGICAL_INDEXES.
CHRONOLOGICAL_INDEX_COLUMNS: dict[str, tuple[str, frozenset[str]]] = {
    "idx_slot_events_scope_chronological": (
        "slot_events", frozenset({"system_id", "enclosure_key", "slot", "observed_at", "id"}),
    ),
    "idx_metric_samples_slot_chronological": (
        "metric_samples", frozenset({"system_id", "enclosure_key", "slot", "observed_at", "id"}),
    ),
    "idx_metric_samples_scope_chronological": (
        "metric_samples",
        frozenset({"system_id", "enclosure_key", "slot", "metric_name", "observed_at", "id"}),
    ),
    "idx_metric_samples_disk_chronological": (
        "metric_samples", frozenset({"disk_identity_key", "observed_at", "id"}),
    ),
    "idx_metric_samples_disk_metric_chronological": (
        "metric_samples", frozenset({"disk_identity_key", "metric_name", "observed_at", "id"}),
    ),
}

# The newest-first event and raw-sample LIMIT shapes the segment reader runs
# (list_slot_events and _list_raw_metric_samples_by_filter). Rollup reads order
# by julianday(bucket_start), which no released index serves, so they are out
# of scope here (Sol review #5); their results are covered by equivalence tests.
_LIMITED_READ_ORDER = " ORDER BY julianday(observed_at) DESC, id DESC LIMIT ?"
_SINCE_CLAUSE = " AND julianday(observed_at) >= julianday(?)"
LIMITED_READ_SHAPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "slot_events_by_slot": ("slot_events", ("system_id", "enclosure_key", "slot")),
    "metric_samples_by_slot": ("metric_samples", ("system_id", "enclosure_key", "slot")),
    "metric_samples_by_slot_metric": (
        "metric_samples", ("system_id", "enclosure_key", "slot", "metric_name"),
    ),
    "metric_samples_by_disk": ("metric_samples", ("disk_identity_key",)),
    "metric_samples_by_disk_metric": ("metric_samples", ("disk_identity_key", "metric_name")),
}


def released_index_sql(name: str) -> str:
    """The text SQLite stores in sqlite_master for a released chronological index."""

    statement = dict(CHRONOLOGICAL_INDEXES)[name]
    return statement.replace("CREATE INDEX IF NOT EXISTS ", "CREATE INDEX ", 1)


class SegmentAuthenticationError(ValueError):
    """A segment does not match its catalog entry. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    owner: int
    group: int
    size_bytes: int
    modified_at_ns: int
    changed_at_ns: int

    @classmethod
    def of(cls, metadata: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            mode=int(metadata.st_mode),
            link_count=int(metadata.st_nlink),
            owner=int(metadata.st_uid),
            group=int(metadata.st_gid),
            size_bytes=int(metadata.st_size),
            modified_at_ns=int(metadata.st_mtime_ns),
            changed_at_ns=int(metadata.st_ctime_ns),
        )


@dataclass(frozen=True)
class CatalogEntry:
    segment_id: str
    file_name: str
    size_bytes: int
    sha256: str
    coverage_start: str | None
    coverage_end: str | None
    row_counts: dict[str, int] | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class LoadedCatalog:
    identity: FileIdentity
    entries: tuple[CatalogEntry, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate key")
        payload[key] = value
    return payload


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp")
    return parsed


def _open_no_follow(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )


def _catalog_entry(entry: Any, seen: set[str]) -> CatalogEntry:
    if not isinstance(entry, dict):
        raise ValueError("entry")
    segment_id = entry.get("segment_id")
    file_name = entry.get("file_name")
    size_bytes = entry.get("size_bytes")
    sha256 = entry.get("sha256")
    if (
        not isinstance(segment_id, str)
        or not SEGMENT_ID_PATTERN.fullmatch(segment_id)
        or segment_id in seen
        or file_name != f"{segment_id}.sqlite3"
        or type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or not set(sha256) <= _SHA256_HEX
    ):
        raise ValueError("entry")
    seen.add(segment_id)
    coverage_start = entry.get("coverage_start")
    coverage_end = entry.get("coverage_end")
    if (coverage_start is None) != (coverage_end is None):
        raise ValueError("coverage")
    if coverage_start is not None and _parse_timestamp(coverage_end) < _parse_timestamp(coverage_start):
        raise ValueError("coverage")
    row_counts = entry.get("row_counts")
    if row_counts is not None and (
        not isinstance(row_counts, dict)
        or any(not isinstance(key, str) or type(value) is not int for key, value in row_counts.items())
    ):
        raise ValueError("row_counts")
    return CatalogEntry(
        segment_id=segment_id,
        file_name=f"{segment_id}.sqlite3",
        size_bytes=size_bytes,
        sha256=sha256,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        row_counts=dict(row_counts) if row_counts is not None else None,
        raw=entry,
    )


def load_catalog(catalog_path: Path, *, hot_path: Path | None = None) -> LoadedCatalog:
    """Read and check the catalog without writing; raises SegmentAuthenticationError."""

    catalog_path = Path(catalog_path)
    if hot_path is not None and path_entry_exists(activation_pending_path(hot_path)):
        raise SegmentAuthenticationError("activation_pending")
    if path_entry_exists(catalog_path.parent / MIGRATION_PENDING_MARKER):
        raise SegmentAuthenticationError("recovery_pending")
    try:
        descriptor = _open_no_follow(catalog_path)
    except FileNotFoundError as exc:
        raise SegmentAuthenticationError("catalog_missing") from exc
    except OSError as exc:
        raise SegmentAuthenticationError("catalog_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CATALOG_BYTES:
            raise SegmentAuthenticationError("catalog_invalid")
        chunks: list[bytes] = []
        remaining = MAX_CATALOG_BYTES + 1
        while remaining > 0 and (chunk := os.read(descriptor, min(HASH_CHUNK_BYTES, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
        identity = FileIdentity.of(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if (
            not isinstance(payload, dict)
            or payload.get("catalog_version") != 1
            or payload.get("complete") is not True
            or not isinstance(payload.get("segments"), list)
        ):
            raise ValueError("catalog")
        seen: set[str] = set()
        entries = tuple(_catalog_entry(entry, seen) for entry in payload["segments"])
    except (UnicodeDecodeError, ValueError) as exc:
        raise SegmentAuthenticationError("catalog_invalid") from exc
    return LoadedCatalog(identity=identity, entries=entries)


def _path_identity(path: Path) -> FileIdentity:
    return FileIdentity.of(os.stat(path, follow_symlinks=False))


def _require_no_sidecars(path: Path, *, reason: str) -> None:
    if any(_sidecar_presence(path)):
        raise SegmentAuthenticationError(reason)


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _sidecar_presence(path: Path) -> tuple[bool, ...]:
    return tuple(path_entry_exists(Path(f"{path}{suffix}")) for suffix in ("-wal", "-shm", "-journal"))


@dataclass
class PinnedFile:
    path: Path
    descriptor: int
    identity: FileIdentity
    sha256: str
    prefix: str

    def require_unchanged(self, reason: str) -> None:
        """The path still names the pinned, single-link file, unchanged since hashing.

        An immutable connection ignores a WAL, so a sidecar that appeared
        while the file was read is refused too: its frames were never seen.
        """

        try:
            current_path = _path_identity(self.path)
        except OSError as exc:
            raise SegmentAuthenticationError(reason) from exc
        if current_path != self.identity or FileIdentity.of(os.fstat(self.descriptor)) != self.identity:
            raise SegmentAuthenticationError(reason)
        _require_no_sidecars(self.path, reason=f"{self.prefix}_sidecar_state")

    def sqlite_uri(self) -> str:
        return f"file:/proc/self/fd/{self.descriptor}?mode=ro&immutable=1"


@contextmanager
def pinned_file(
    path: Path,
    *,
    prefix: str,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> Iterator[PinnedFile]:
    """Open, pin and hash one file; reasons are ``<prefix>_<code>``.

    With an expected size and SHA-256 this is the catalog authentication the
    segment reader applies: one regular file with one link, the path naming the
    pinned descriptor before and after hashing, the catalog's exact size and
    digest, and no SQLite sidecar files.
    """

    path = Path(path).absolute()
    try:
        descriptor = _open_no_follow(path)
    except FileNotFoundError as exc:
        raise SegmentAuthenticationError(f"{prefix}_missing") from exc
    except OSError as exc:
        raise SegmentAuthenticationError(f"{prefix}_not_regular") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SegmentAuthenticationError(f"{prefix}_not_regular")
        identity = FileIdentity.of(metadata)
        try:
            if _path_identity(path) != identity:
                raise SegmentAuthenticationError(f"{prefix}_identity_changed")
        except OSError as exc:
            raise SegmentAuthenticationError(f"{prefix}_identity_changed") from exc
        _require_no_sidecars(path, reason=f"{prefix}_sidecar_state")
        if expected_size_bytes is not None and identity.size_bytes != expected_size_bytes:
            raise SegmentAuthenticationError(f"{prefix}_size_mismatch")
        sha256 = _hash_descriptor(descriptor)
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise SegmentAuthenticationError(f"{prefix}_sha256_mismatch")
        pinned = PinnedFile(
            path=path, descriptor=descriptor, identity=identity, sha256=sha256, prefix=prefix,
        )
        pinned.require_unchanged(f"{prefix}_identity_changed")
        yield pinned
    finally:
        os.close(descriptor)


@contextmanager
def _immutable_connection(pinned: PinnedFile, *, reason: str) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(pinned.sqlite_uri(), uri=True)
    except sqlite3.Error as exc:
        raise SegmentAuthenticationError(reason) from exc
    with closing(connection):
        connection.execute("PRAGMA query_only = ON")
        yield connection


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _index_sql(connection: sqlite3.Connection) -> dict[str, str | None]:
    return {
        str(name): sql
        for name, sql in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
    }


@dataclass(frozen=True)
class IndexInventory:
    allowed: tuple[str, ...]
    present: tuple[str, ...]
    missing: tuple[str, ...]
    nonstandard: tuple[str, ...]


def chronological_index_inventory(connection: sqlite3.Connection) -> IndexInventory:
    """Which released chronological indexes the columns allow, have and lack."""

    columns: dict[str, frozenset[str]] = {}
    indexes = _index_sql(connection)
    allowed: list[str] = []
    present: list[str] = []
    missing: list[str] = []
    nonstandard: list[str] = []
    for name, _ in CHRONOLOGICAL_INDEXES:
        table, required = CHRONOLOGICAL_INDEX_COLUMNS[name]
        if table not in columns:
            columns[table] = _table_columns(connection, table)
        if name in indexes and indexes[name] != released_index_sql(name):
            nonstandard.append(name)
            continue
        if not required <= columns[table]:
            if name in indexes:
                nonstandard.append(name)
            continue
        allowed.append(name)
        (present if name in indexes else missing).append(name)
    return IndexInventory(tuple(allowed), tuple(present), tuple(missing), tuple(nonstandard))


@dataclass(frozen=True)
class SegmentCensusEntry:
    segment_id: str
    size_bytes: int
    coverage_start: str | None
    coverage_end: str | None
    authenticated: bool
    indexed: bool
    missing_indexes: tuple[str, ...]
    candidate: bool
    error: str | None = None


@dataclass(frozen=True)
class SegmentCensusReport:
    completed_at: str
    catalog_error: str | None
    segments: tuple[SegmentCensusEntry, ...] = ()
    catalog_identity: FileIdentity | None = None
    # Per segment: (file name, identity, sidecar presence) as inspected.
    segment_identities: tuple[
        tuple[str, FileIdentity | None, tuple[bool, ...]], ...
    ] = field(default=(), repr=False)

    def summary(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        if self.catalog_error is not None:
            errors.append({"segment_id": None, "reason": self.catalog_error})
        errors.extend(
            {"segment_id": entry.segment_id, "reason": entry.error}
            for entry in self.segments
            if entry.error is not None
        )
        authenticated = [entry for entry in self.segments if entry.authenticated]
        candidates = [entry for entry in authenticated if entry.candidate]
        return {
            "state": CENSUS_STATE,
            "indexed": sum(1 for entry in authenticated if entry.indexed),
            "unindexed": sum(1 for entry in authenticated if not entry.indexed),
            "candidates": len(candidates),
            "candidate_bytes": sum(entry.size_bytes for entry in candidates),
            "last_census_at": self.completed_at,
            "errors": errors[:MAX_REPORTED_ERRORS],
            "errors_omitted": max(0, len(errors) - MAX_REPORTED_ERRORS),
        }


_SegmentFingerprint = tuple[str, "FileIdentity | None", tuple[bool, ...]]


def _census_entry(directory: Path, entry: CatalogEntry) -> tuple[SegmentCensusEntry, _SegmentFingerprint]:
    """One segment's census result and the file state that result describes.

    The fingerprint of an authenticated segment is the pinned identity it was
    hashed under, not a later stat, so a file replaced after its inspection
    makes the cached pass stale instead of looking fresh.
    """

    path = directory / entry.file_name
    base = {
        "segment_id": entry.segment_id,
        "size_bytes": entry.size_bytes,
        "coverage_start": entry.coverage_start,
        "coverage_end": entry.coverage_end,
    }
    try:
        with pinned_file(
            path,
            prefix="segment",
            expected_size_bytes=entry.size_bytes,
            expected_sha256=entry.sha256,
        ) as pinned:
            with _immutable_connection(pinned, reason="segment_unreadable") as connection:
                try:
                    inventory = chronological_index_inventory(connection)
                except sqlite3.Error as exc:
                    raise SegmentAuthenticationError("segment_unreadable") from exc
            pinned.require_unchanged("segment_identity_changed")
            fingerprint: _SegmentFingerprint = (path.name, pinned.identity, (False, False, False))
    except (SegmentAuthenticationError, OSError) as exc:
        reason = exc.reason if isinstance(exc, SegmentAuthenticationError) else "segment_unreadable"
        failed = SegmentCensusEntry(
            **base, authenticated=False, indexed=False, missing_indexes=(), candidate=False,
            error=reason,
        )
        return failed, _segment_fingerprint(path)
    indexed = not inventory.missing and not inventory.nonstandard
    candidate = (
        bool(inventory.missing)
        and not inventory.nonstandard
        and entry.size_bytes <= MAX_HISTORY_SEGMENT_BYTES
    )
    return SegmentCensusEntry(
        **base,
        authenticated=True,
        indexed=indexed,
        missing_indexes=inventory.missing,
        candidate=candidate,
        error="segment_nonstandard_index" if inventory.nonstandard else None,
    ), fingerprint


def _optional_identity(path: Path) -> FileIdentity | None:
    try:
        return _path_identity(path)
    except OSError:
        return None


def _segment_fingerprint(path: Path) -> tuple[str, FileIdentity | None, tuple[bool, ...]]:
    """What a cached census pass depends on for one segment, without reading it."""

    return (path.name, _optional_identity(path), _sidecar_presence(path))


def run_segment_census(
    catalog_path: Path,
    *,
    hot_path: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SegmentCensusReport:
    """One full, read-only census pass over the catalogued segments."""

    catalog_path = Path(catalog_path).absolute()
    try:
        catalog = load_catalog(catalog_path, hot_path=hot_path)
    except SegmentAuthenticationError as exc:
        return SegmentCensusReport(
            completed_at=now().isoformat(),
            catalog_error=exc.reason,
            catalog_identity=_optional_identity(catalog_path),
        )
    directory = catalog_path.parent
    inspected = [_census_entry(directory, entry) for entry in catalog.entries]
    segments = tuple(result for result, _ in inspected)
    catalog_error = None
    if _optional_identity(catalog_path) != catalog.identity:
        # A rotation or restore replaced the catalog mid-pass; the next pass
        # reads the new one. Counts from this pass are still reported.
        catalog_error = "catalog_changed"
    return SegmentCensusReport(
        completed_at=now().isoformat(),
        catalog_error=catalog_error,
        segments=segments,
        catalog_identity=catalog.identity,
        segment_identities=tuple(fingerprint for _, fingerprint in inspected),
    )


class SegmentCensus:
    """Cache one census and refresh it in the background only when files change.

    ``summary()`` never hashes: it stats the catalog and the segments the last
    pass named, and starts one background pass when those identities changed
    and ``min_interval_seconds`` has passed since the previous pass started.
    """

    def __init__(
        self,
        *,
        catalog_path: Path,
        hot_path: Path | None = None,
        min_interval_seconds: float = DEFAULT_MIN_CENSUS_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        start_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path).absolute()
        self.hot_path = Path(hot_path).absolute() if hot_path is not None else None
        self.min_interval_seconds = float(min_interval_seconds)
        self._monotonic = monotonic
        self._start_background = start_background or self._start_thread
        self._lock = threading.Lock()
        self._report: SegmentCensusReport | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        self._running = False
        self._last_started: float | None = None

    @staticmethod
    def _start_thread(target: Callable[[], None]) -> None:
        threading.Thread(target=target, name="history-segment-census", daemon=True).start()

    def _current_fingerprint(self, report: SegmentCensusReport | None) -> tuple[Any, ...]:
        markers = (
            path_entry_exists(self.catalog_path.parent / MIGRATION_PENDING_MARKER),
            self.hot_path is not None and path_entry_exists(activation_pending_path(self.hot_path)),
        )
        segments = tuple(
            _segment_fingerprint(self.catalog_path.parent / name)
            for name, _, _ in (report.segment_identities if report is not None else ())
        )
        return (markers, _optional_identity(self.catalog_path), segments)

    def _report_fingerprint(self, report: SegmentCensusReport) -> tuple[Any, ...]:
        markers = (
            report.catalog_error == "recovery_pending",
            report.catalog_error == "activation_pending",
        )
        return (markers, report.catalog_identity, report.segment_identities)

    def refresh(self) -> SegmentCensusReport:
        """Run one census pass now, in this thread, and cache it."""

        with self._lock:
            self._last_started = self._monotonic()
        report = run_segment_census(self.catalog_path, hot_path=self.hot_path)
        with self._lock:
            self._report = report
            self._fingerprint = (
                None if report.catalog_error == "catalog_changed" else self._report_fingerprint(report)
            )
        return report

    def _background_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - an informational census must never break health.
            logger.warning("History segment census failed.", exc_info=True)
            with self._lock:
                self._report = SegmentCensusReport(
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    catalog_error="census_failed",
                )
                self._fingerprint = None
        finally:
            with self._lock:
                self._running = False

    def summary(self) -> dict[str, Any]:
        with self._lock:
            report = self._report
            stale = self._fingerprint is None or self._current_fingerprint(report) != self._fingerprint
            due = self._last_started is None or (
                self._monotonic() - self._last_started >= self.min_interval_seconds
            )
            start = stale and due and not self._running
            if start:
                self._running = True
                self._last_started = self._monotonic()
        if start:
            try:
                self._start_background(self._background_refresh)
            except Exception:  # noqa: BLE001 - see _background_refresh.
                logger.warning("History segment census could not start.", exc_info=True)
                with self._lock:
                    self._running = False
        if report is None:
            return {
                "state": CENSUS_STATE,
                "indexed": None,
                "unindexed": None,
                "candidates": None,
                "candidate_bytes": None,
                "last_census_at": None,
                "errors": [],
                "errors_omitted": 0,
            }
        return report.summary()


# --- candidate validation --------------------------------------------------


@dataclass(frozen=True)
class CandidateValidation:
    segment_id: str
    failures: tuple[str, ...]
    original_sha256: str | None = None
    original_size_bytes: int | None = None
    candidate_sha256: str | None = None
    candidate_size_bytes: int | None = None
    candidate_identity: FileIdentity | None = None
    added_indexes: tuple[str, ...] = ()
    table_counts: dict[str, int] = field(default_factory=dict)
    content_digest: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def _encode_value(kind: bytes, value: Any) -> bytes:
    if kind == b"null":
        return b"N"
    if kind == b"integer":
        return b"I" + int(value).to_bytes(8, "big", signed=True)
    if kind == b"real":
        return b"R" + struct.pack(">d", float(value))
    payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return (b"T" if kind == b"text" else b"B") + len(payload).to_bytes(8, "big") + payload


def table_content_digests(connection: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """``{table: (row_count, sha256)}`` over typed values in rowid or primary-key order."""

    previous_factory = connection.text_factory
    connection.text_factory = bytes
    try:
        tables = [
            (str(name.decode() if isinstance(name, bytes) else name),
             (sql.decode() if isinstance(sql, bytes) else sql) or "")
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        digests: dict[str, tuple[int, str]] = {}
        for table, sql in tables:
            info = list(connection.execute(f'PRAGMA table_info("{table}")'))
            columns = [row[1].decode() if isinstance(row[1], bytes) else str(row[1]) for row in info]
            if "WITHOUT ROWID" in " ".join(sql.upper().split()):
                order = ", ".join(
                    f'"{column}"'
                    for _, column in sorted(
                        (int(row[5]), columns[position]) for position, row in enumerate(info) if int(row[5]) > 0
                    )
                )
                selected = [f'"{column}"' for column in columns]
            else:
                order = "rowid"
                selected = ["rowid", *(f'"{column}"' for column in columns)]
            projection = ", ".join(f"typeof({item}), {item}" for item in selected)
            digest = hashlib.sha256()
            digest.update(len(columns).to_bytes(4, "big"))
            for column in columns:
                digest.update(len(column).to_bytes(4, "big") + column.encode("utf-8"))
            count = 0
            for row in connection.execute(f'SELECT {projection} FROM "{table}" ORDER BY {order}'):
                count += 1
                digest.update(b"\x1e")
                for position in range(0, len(row), 2):
                    digest.update(_encode_value(row[position], row[position + 1]))
            digests[table] = (count, digest.hexdigest())
        return digests
    finally:
        connection.text_factory = previous_factory


def _schema_rows(connection: sqlite3.Connection) -> tuple[set[tuple[Any, ...]], dict[str, tuple[Any, ...]]]:
    others: set[tuple[Any, ...]] = set()
    indexes: dict[str, tuple[Any, ...]] = {}
    for kind, name, table, sql in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ):
        if kind == "index":
            indexes[str(name)] = (str(table), sql)
        else:
            others.add((kind, name, table, sql))
    return others, indexes


def _coverage(connection: sqlite3.Connection) -> tuple[datetime | None, datetime | None]:
    start: datetime | None = None
    end: datetime | None = None
    for table, column in HISTORY_TABLE_TIMESTAMPS.items():
        for (value,) in connection.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"):
            timestamp = _parse_timestamp(value)
            start = timestamp if start is None or timestamp < start else start
            end = timestamp if end is None or timestamp > end else end
    return start, end


def limited_read_plan_failures(connection: sqlite3.Connection) -> list[str]:
    """Event/raw LIMIT shapes that would not use a chronological index without a sort."""

    failures: list[str] = []
    columns = {table: _table_columns(connection, table) for table in ("slot_events", "metric_samples")}
    for shape, (table, filters) in LIMITED_READ_SHAPES.items():
        if not set(filters) | {"observed_at", "id"} <= columns[table]:
            continue
        where = " AND ".join(f"{column} = ?" for column in filters)
        for since in ("", _SINCE_CLAUSE):
            sql = f"SELECT * FROM {table} WHERE {where}{since}{_LIMITED_READ_ORDER}"
            details = [
                str(row[3])
                for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", ["x"] * sql.count("?"))
            ]
            uses_index = any(
                "USING INDEX idx_" in detail and detail.split("USING INDEX ", 1)[1].split()[0]
                in CHRONOLOGICAL_INDEX_COLUMNS
                for detail in details
            )
            if not uses_index or any("TEMP B-TREE" in detail for detail in details):
                failures.append(f"query_plan:{shape}{':since' if since else ''}")
    return failures


def _compare_candidate(
    original: sqlite3.Connection,
    candidate: sqlite3.Connection,
    entry: CatalogEntry,
    expected_added: tuple[str, ...],
) -> tuple[list[str], dict[str, int], str]:
    failures: list[str] = []
    try:
        check = [tuple(row) for row in candidate.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError:
        # Not a database, or damage SQLite reports as an error rather than rows.
        check = []
    if check != [("ok",)]:
        return ["candidate_integrity_check"], {}, ""
    for pragma in ("user_version", "application_id"):
        if (
            original.execute(f"PRAGMA {pragma}").fetchone()[0]
            != candidate.execute(f"PRAGMA {pragma}").fetchone()[0]
        ):
            failures.append(f"{pragma}_mismatch")
    original_others, original_indexes = _schema_rows(original)
    candidate_others, candidate_indexes = _schema_rows(candidate)
    if original_others != candidate_others:
        failures.append("schema_mismatch")
    if any(candidate_indexes.get(name) != value for name, value in original_indexes.items()):
        failures.append("original_index_changed")
    added = {name: value for name, value in candidate_indexes.items() if name not in original_indexes}
    expected = {
        name: (CHRONOLOGICAL_INDEX_COLUMNS[name][0], released_index_sql(name)) for name in expected_added
    }
    if added != expected:
        failures.append("unexpected_index_set")
    original_digests = table_content_digests(original)
    candidate_digests = table_content_digests(candidate)
    if set(original_digests) != set(candidate_digests):
        failures.append("table_set_mismatch")
    for table in sorted(set(original_digests) & set(candidate_digests)):
        if original_digests[table][0] != candidate_digests[table][0]:
            failures.append(f"row_count_mismatch:{table}")
        elif original_digests[table][1] != candidate_digests[table][1]:
            failures.append(f"content_digest_mismatch:{table}")
    counts = {table: count for table, (count, _) in candidate_digests.items()}
    if entry.row_counts is not None and any(
        counts.get(table) != count for table, count in entry.row_counts.items()
    ):
        failures.append("catalog_row_counts_mismatch")
    try:
        start, end = _coverage(candidate)
    except ValueError:
        failures.append("catalog_coverage_mismatch")
    else:
        catalog_start = _parse_timestamp(entry.coverage_start) if entry.coverage_start is not None else None
        catalog_end = _parse_timestamp(entry.coverage_end) if entry.coverage_end is not None else None
        if (start, end) != (catalog_start, catalog_end):
            failures.append("catalog_coverage_mismatch")
    failures.extend(limited_read_plan_failures(candidate))
    combined = hashlib.sha256()
    for table in sorted(candidate_digests):
        combined.update(table.encode("utf-8") + b"\x00" + candidate_digests[table][1].encode("ascii"))
    return failures, counts, combined.hexdigest()


def validate_conversion_candidate(
    *,
    catalog_path: Path,
    segment_id: str,
    candidate_path: Path,
    hot_path: Path | None = None,
) -> CandidateValidation:
    """Check a candidate indexed copy against its catalog-authenticated original.

    Read-only. The original stays pinned and authenticated by its existing
    catalog entry for the whole comparison, so a corrupted original cannot be
    legitimized by a copy of itself. The result names every failed check; a
    later publisher may act only on a result whose ``ok`` is true, and must
    recheck the returned candidate identity and digest at commit.
    """

    catalog_path = Path(catalog_path).absolute()
    try:
        catalog = load_catalog(catalog_path, hot_path=hot_path)
    except SegmentAuthenticationError as exc:
        return CandidateValidation(segment_id=segment_id, failures=(exc.reason,))
    entry = next((item for item in catalog.entries if item.segment_id == segment_id), None)
    if entry is None:
        return CandidateValidation(segment_id=segment_id, failures=("catalog_entry_missing",))
    try:
        with pinned_file(
            catalog_path.parent / entry.file_name,
            prefix="original",
            expected_size_bytes=entry.size_bytes,
            expected_sha256=entry.sha256,
        ) as original_file, pinned_file(Path(candidate_path), prefix="candidate") as candidate_file:
            if (original_file.identity.device, original_file.identity.inode) == (
                candidate_file.identity.device, candidate_file.identity.inode,
            ):
                raise SegmentAuthenticationError("candidate_same_file")
            if candidate_file.identity.size_bytes > MAX_HISTORY_SEGMENT_BYTES:
                raise SegmentAuthenticationError("candidate_oversize")
            with (
                _immutable_connection(original_file, reason="original_unreadable") as original,
                _immutable_connection(candidate_file, reason="candidate_unreadable") as candidate,
            ):
                try:
                    inventory = chronological_index_inventory(original)
                    if inventory.nonstandard:
                        raise SegmentAuthenticationError("original_nonstandard_index")
                    if not inventory.missing:
                        raise SegmentAuthenticationError("original_already_indexed")
                    try:
                        failures, counts, digest = _compare_candidate(
                            original, candidate, entry, inventory.missing,
                        )
                    except sqlite3.DatabaseError:
                        failures, counts, digest = ["candidate_unreadable"], {}, None
                except sqlite3.DatabaseError as exc:
                    raise SegmentAuthenticationError("original_unreadable") from exc
            original_file.require_unchanged("original_changed")
            candidate_file.require_unchanged("candidate_changed")
            try:
                final_catalog = load_catalog(catalog_path, hot_path=hot_path)
            except SegmentAuthenticationError:
                final_catalog = None
            final_entry = (
                next((item for item in final_catalog.entries if item.segment_id == segment_id), None)
                if final_catalog is not None
                else None
            )
            if (
                final_catalog is None
                or final_catalog.identity != catalog.identity
                or final_entry is None
                or final_entry.raw != entry.raw
            ):
                failures.append("catalog_changed")
            return CandidateValidation(
                segment_id=segment_id,
                failures=tuple(failures),
                original_sha256=original_file.sha256,
                original_size_bytes=original_file.identity.size_bytes,
                candidate_sha256=candidate_file.sha256,
                candidate_size_bytes=candidate_file.identity.size_bytes,
                candidate_identity=candidate_file.identity,
                added_indexes=inventory.missing,
                table_counts=counts,
                content_digest=digest or None,
            )
    except SegmentAuthenticationError as exc:
        return CandidateValidation(segment_id=segment_id, failures=(exc.reason,))
    except OSError:
        return CandidateValidation(segment_id=segment_id, failures=("io_error",))
