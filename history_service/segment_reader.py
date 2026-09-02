from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    SEGMENT_ID_PATTERN,
    activation_pending_path,
    path_entry_exists,
)
from history_service.segment_sealer import HISTORY_TABLE_TIMESTAMPS

MAX_SEGMENTS_PER_QUERY = 32
MAX_HISTORY_QUERY_LIMIT = 5_000
HASH_CHUNK_BYTES = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_catalog_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("Segmented history catalog contains a duplicate JSON key.")
        payload[key] = value
    return payload


def _parse_catalog_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Segmented history catalog coverage is invalid.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Segmented history catalog coverage is invalid.") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Segmented history catalog coverage is invalid.")
    return timestamp


def _utc_day_start(value: str) -> str:
    return (
        _parse_catalog_timestamp(value)
        .astimezone(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )


@dataclass(frozen=True)
class _CatalogSegment:
    path: Path
    size_bytes: int
    sha256: str
    coverage_start: datetime | None
    coverage_end: datetime | None


@dataclass(frozen=True)
class _AuthenticatedFileIdentity:
    device: int
    inode: int
    mode: int
    link_count: int
    owner: int
    group: int
    size_bytes: int
    modified_at_ns: int
    changed_at_ns: int


@dataclass(frozen=True)
class _VerifiedSegmentDigest:
    generation_id: str
    sha256: str
    identity: _AuthenticatedFileIdentity


class SegmentedHistoryReader:
    def __init__(
        self,
        *,
        hot_path: Path,
        segment_paths: Iterable[Path] = (),
        max_segments_per_query: int = MAX_SEGMENTS_PER_QUERY,
        activation_marker_path: Path | None = None,
    ) -> None:
        if type(max_segments_per_query) is not int or max_segments_per_query < 1:
            raise ValueError("Segmented history query segment limit is invalid.")
        self.hot_path = self._require_regular_file(Path(hot_path), label="hot history")
        self.segment_paths = tuple(
            self._require_regular_file(Path(path), label="history segment") for path in segment_paths
        )
        if len(self.segment_paths) > max_segments_per_query:
            raise ValueError("Segmented history query exceeds its segment limit.")
        self.max_segments_per_query = max_segments_per_query
        self.activation_marker_path = activation_marker_path
        self._catalog_segments: tuple[_CatalogSegment, ...] | None = None
        self._catalog_payload: dict[str, Any] | None = None
        self._catalog_generation_id: str | None = None
        self._verified_segment_digests: dict[tuple[str, Path], _VerifiedSegmentDigest] = {}
        self._verified_segment_digests_lock = threading.Lock()

    @classmethod
    def from_catalog(
        cls,
        *,
        hot_path: Path,
        catalog_path: Path,
        max_segments_per_query: int = MAX_SEGMENTS_PER_QUERY,
        allow_pending_recovery: bool = False,
        allow_pending_activation: bool = False,
    ) -> "SegmentedHistoryReader":
        marker_path = activation_pending_path(hot_path)
        if not allow_pending_activation and path_entry_exists(marker_path):
            raise ValueError("Segmented history activation is pending.")
        catalog_path = cls._require_regular_file(Path(catalog_path), label="segment catalog")
        if not allow_pending_recovery and path_entry_exists(catalog_path.parent / MIGRATION_PENDING_MARKER):
            raise ValueError("Segmented history migration recovery is pending.")
        try:
            catalog = json.loads(
                catalog_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_catalog_keys,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Segmented history catalog is invalid.") from exc
        if (
            not isinstance(catalog, dict)
            or catalog.get("catalog_version") != 1
            or catalog.get("complete") is not True
            or not isinstance(catalog.get("segments"), list)
        ):
            raise ValueError("Segmented history catalog is invalid.")
        catalog_segments: list[_CatalogSegment] = []
        segment_ids: set[str] = set()
        for entry in catalog["segments"]:
            if not isinstance(entry, dict):
                raise ValueError("Segmented history catalog is invalid.")
            segment_id = entry.get("segment_id")
            file_name = entry.get("file_name")
            if (
                not isinstance(segment_id, str)
                or not SEGMENT_ID_PATTERN.fullmatch(segment_id)
                or segment_id in segment_ids
                or not isinstance(file_name, str)
                or file_name != f"{segment_id}.sqlite3"
            ):
                raise ValueError("Segmented history catalog is invalid.")
            segment_ids.add(segment_id)
            segment_path = cls._require_regular_file(catalog_path.parent / file_name, label="history segment")
            cls._require_no_sqlite_sidecars(segment_path)
            size_bytes = entry.get("size_bytes")
            sha256 = entry.get("sha256")
            if (
                type(size_bytes) is not int
                or size_bytes != segment_path.stat().st_size
                or not isinstance(sha256, str)
            ):
                raise ValueError("Segmented history catalog segment integrity check failed.")
            coverage_start_value = entry.get("coverage_start")
            coverage_end_value = entry.get("coverage_end")
            if (coverage_start_value is None) != (coverage_end_value is None):
                raise ValueError("Segmented history catalog coverage is invalid.")
            coverage_start = coverage_end = None
            if coverage_start_value is not None:
                coverage_start = _parse_catalog_timestamp(coverage_start_value)
                coverage_end = _parse_catalog_timestamp(coverage_end_value)
                if coverage_end < coverage_start:
                    raise ValueError("Segmented history catalog coverage is invalid.")
            catalog_segments.append(
                _CatalogSegment(
                    path=segment_path,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                )
            )
        if (
            len(catalog_segments) > max_segments_per_query
            and any(segment.coverage_start is None for segment in catalog_segments)
        ):
            raise ValueError("Segmented history catalog lacks bounded query coverage.")
        reader = cls(
            hot_path=hot_path,
            segment_paths=(),
            max_segments_per_query=max_segments_per_query,
            activation_marker_path=None if allow_pending_activation else marker_path,
        )
        reader.segment_paths = tuple(segment.path for segment in catalog_segments)
        reader._catalog_segments = tuple(catalog_segments)
        reader._catalog_payload = catalog
        generation_id = catalog.get("generation_id")
        reader._catalog_generation_id = (
            generation_id if isinstance(generation_id, str) else "legacy-v1"
        )
        reader._verify_catalog_coverage()
        return reader

    @staticmethod
    def _require_regular_file(path: Path, *, label: str) -> Path:
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Segmented history {label} must be a regular file.")
        return path.absolute()

    @contextmanager
    def _read_only_connection(
        self,
        path: Path,
        *,
        expected_size_bytes: int | None = None,
        expected_sha256: str | None = None,
        immutable: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        self._require_activation_ready()
        if not immutable:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                yield connection
            finally:
                connection.close()
            return

        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        connection: sqlite3.Connection | None = None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Segmented history segment must be a regular file.")
            try:
                connection = sqlite3.connect(
                    f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
                    uri=True,
                )
            except sqlite3.Error as exc:
                raise ValueError("Segmented history catalog segment integrity check failed.") from exc
            self._authenticated_segment_identity(path, descriptor)
            if expected_size_bytes is not None and metadata.st_size != expected_size_bytes:
                raise ValueError("Segmented history catalog segment integrity check failed.")
            if expected_sha256 is not None:
                cache_key = self._verified_digest_cache_key(path, expected_sha256)
                cache_lock = (
                    self._verified_segment_digests_lock
                    if cache_key is not None
                    else nullcontext()
                )
                with cache_lock:
                    current_identity = self._authenticated_segment_identity(path, descriptor)
                    if expected_size_bytes is not None and current_identity.size_bytes != expected_size_bytes:
                        raise ValueError("Segmented history catalog segment integrity check failed.")
                    verified = (
                        self._verified_segment_digests.get(cache_key)
                        if cache_key is not None
                        else None
                    )
                    expected_verification = _VerifiedSegmentDigest(
                        generation_id=cache_key[0] if cache_key is not None else "",
                        sha256=expected_sha256,
                        identity=current_identity,
                    )
                    if verified != expected_verification:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        digest = hashlib.sha256()
                        while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
                            digest.update(chunk)
                        if digest.hexdigest() != expected_sha256:
                            raise ValueError("Segmented history catalog segment integrity check failed.")
                        if self._authenticated_segment_identity(path, descriptor) != current_identity:
                            raise ValueError("Segmented history catalog segment integrity check failed.")
                        if cache_key is not None:
                            self._verified_segment_digests[cache_key] = expected_verification
                    os.lseek(descriptor, 0, os.SEEK_SET)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            if connection is not None:
                connection.close()
            os.close(descriptor)

    @staticmethod
    def _file_identity(metadata: os.stat_result) -> _AuthenticatedFileIdentity:
        return _AuthenticatedFileIdentity(
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

    @classmethod
    def _authenticated_segment_identity(
        cls,
        path: Path,
        descriptor: int,
    ) -> _AuthenticatedFileIdentity:
        path_metadata = os.stat(path, follow_symlinks=False)
        pinned_metadata = os.fstat(descriptor)
        path_identity = cls._file_identity(path_metadata)
        pinned_identity = cls._file_identity(pinned_metadata)
        if (
            stat.S_ISLNK(path_metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
            or pinned_metadata.st_nlink != 1
            or path_identity != pinned_identity
        ):
            raise ValueError("Segmented history catalog segment integrity check failed.")
        return pinned_identity

    def _verified_digest_cache_key(
        self,
        path: Path,
        expected_sha256: str,
    ) -> tuple[str, Path] | None:
        if self._catalog_generation_id is None:
            return None
        if any(
            segment.path == path and segment.sha256 == expected_sha256
            for segment in self._catalog_segments or ()
        ):
            return (self._catalog_generation_id, path)
        return None

    @staticmethod
    def _require_limit(limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_QUERY_LIMIT:
            raise ValueError("Segmented history query limit is invalid.")
        return limit

    @staticmethod
    def _rollup_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(row["rollup_seconds"]),
            str(row["_bucket_start"]),
            str(row["system_id"]),
            str(row["enclosure_key"]),
            int(row["slot"]),
            str(row["metric_name"]),
            str(row.get("disk_identity_key") or ""),
        )

    @classmethod
    def _merge_rollup_rows(cls, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = cls._rollup_key(row)
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                continue
            existing["sample_count"] = int(existing["sample_count"]) + int(row["sample_count"])
            existing["_value_sum"] = float(existing["_value_sum"]) + float(row["_value_sum"])
            existing["value_min"] = min(float(existing["value_min"]), float(row["value_min"]))
            existing["value_max"] = max(float(existing["value_max"]), float(row["value_max"]))
            if _parse_catalog_timestamp(row["_last_observed_at"]) > _parse_catalog_timestamp(
                existing["_last_observed_at"]
            ):
                for field in (
                    "observed_at",
                    "system_label",
                    "enclosure_id",
                    "enclosure_label",
                    "slot_label",
                    "device_name",
                    "serial",
                    "model",
                    "state",
                    "gptid",
                    "persistent_id_label",
                    "disk_identity_key",
                    "logical_unit_id",
                    "sas_address",
                    "_last_value",
                    "_last_observed_at",
                ):
                    existing[field] = row[field]
        for row in merged.values():
            if row["metric_name"] in {"bytes_read", "bytes_written", "power_on_hours"}:
                value = float(row["_last_value"])
                row["observed_at"] = row["_last_observed_at"]
            else:
                value = float(row["_value_sum"]) / int(row["sample_count"])
                row["observed_at"] = row["_bucket_start"]
            row["value_real"] = value
            row["value"] = value
            row.pop("_value_sum", None)
            row.pop("_last_value", None)
            row.pop("_last_observed_at", None)
        return list(merged.values())

    @staticmethod
    def _require_no_sqlite_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            if path_entry_exists(Path(f"{path}{suffix}")):
                raise ValueError("Segmented history immutable segment has SQLite sidecar state.")

    def _require_activation_ready(self) -> None:
        if self.activation_marker_path is not None and path_entry_exists(self.activation_marker_path):
            raise ValueError("Segmented history activation is pending.")

    def _selected_segment_paths(self, *, since: str | None) -> tuple[Path, ...]:
        self._require_activation_ready()
        if self._catalog_segments is None:
            return self.segment_paths
        since_timestamp = _parse_catalog_timestamp(since) if since is not None else None
        candidates = tuple(
            segment
            for segment in self._catalog_segments
            if since_timestamp is None
            or segment.coverage_end is None
            or segment.coverage_end >= since_timestamp
        )
        if len(candidates) > self.max_segments_per_query:
            raise ValueError("Segmented history query exceeds its segment limit.")
        return tuple(segment.path for segment in candidates)

    @contextmanager
    def _query_connection(self, path: Path) -> Iterator[sqlite3.Connection]:
        self._require_activation_ready()
        if path == self.hot_path:
            with self._read_only_connection(path) as connection:
                yield connection
            return
        self._require_no_sqlite_sidecars(path)
        catalog_segment = next(
            (
                segment
                for segment in (self._catalog_segments or ())
                if segment.path == path
            ),
            None,
        )
        with self._read_only_connection(
            path,
            expected_size_bytes=catalog_segment.size_bytes if catalog_segment is not None else None,
            expected_sha256=catalog_segment.sha256 if catalog_segment is not None else None,
            immutable=True,
        ) as connection:
            yield connection

    def verify_catalog_segments(self) -> tuple[Path, ...]:
        paths = self._selected_segment_paths(since=None)
        for path in paths:
            with self._query_connection(path):
                pass
        return paths

    def catalog_payload(self) -> dict[str, Any]:
        if self._catalog_payload is None:
            raise ValueError("Segmented history reader was not created from a catalog.")
        return self._catalog_payload

    def _verify_catalog_coverage(self) -> None:
        for segment in self._catalog_segments or ():
            actual_start: datetime | None = None
            actual_end: datetime | None = None
            with self._read_only_connection(
                segment.path,
                expected_size_bytes=segment.size_bytes,
                expected_sha256=segment.sha256,
                immutable=True,
            ) as connection:
                for table_name, timestamp_column in HISTORY_TABLE_TIMESTAMPS.items():
                    rows = connection.execute(
                        f"SELECT {timestamp_column} FROM {table_name} WHERE {timestamp_column} IS NOT NULL"
                    )
                    for row in rows:
                        timestamp = _parse_catalog_timestamp(row[0])
                        actual_start = timestamp if actual_start is None else min(actual_start, timestamp)
                        actual_end = timestamp if actual_end is None else max(actual_end, timestamp)
            if actual_start != segment.coverage_start or actual_end != segment.coverage_end:
                raise ValueError("Segmented history catalog coverage does not match segment contents.")

    def counts(self) -> dict[str, int]:
        totals = {
            "tracked_slots": 0,
            "event_count": 0,
            "metric_sample_count": 0,
            "metric_rollup_count": 0,
        }
        for path in (self.hot_path, *self._selected_segment_paths(since=None)):
            with self._query_connection(path) as connection:
                if path == self.hot_path:
                    totals["tracked_slots"] = int(
                        connection.execute("SELECT COUNT(*) FROM slot_state_current").fetchone()[0]
                    )
                totals["event_count"] += int(
                    connection.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0]
                )
                totals["metric_sample_count"] += int(
                    connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
                )
                totals["metric_rollup_count"] += int(
                    connection.execute("SELECT COUNT(*) FROM metric_rollups").fetchone()[0]
                )
        return totals

    def database_size_bytes(self) -> int:
        total = 0
        for path in (
            self.hot_path,
            Path(f"{self.hot_path}-wal"),
            Path(f"{self.hot_path}-shm"),
        ):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        for path in self._selected_segment_paths(since=None):
            segment_path = self._require_regular_file(path, label="history segment")
            total += segment_path.stat().st_size
        return total

    def list_scopes(self, *, include_activity_counts: bool = True) -> list[dict[str, Any]]:
        with self._read_only_connection(self.hot_path) as connection:
            rows = connection.execute(
                """
                SELECT
                    system_id, system_label, enclosure_id, enclosure_label,
                    enclosure_key, COUNT(*) AS tracked_slots,
                    MAX(last_seen_at) AS last_seen_at
                FROM slot_state_current
                GROUP BY
                    system_id, system_label, enclosure_id,
                    enclosure_label, enclosure_key
                ORDER BY system_label, enclosure_label
                """
            ).fetchall()
        scopes = [dict(row) for row in rows]
        if not include_activity_counts:
            for scope in scopes:
                scope.update(
                    {
                        "event_count": None,
                        "metric_sample_count": None,
                        "metric_rollup_count": None,
                        "activity_counts_deferred": 1,
                    }
                )
            return scopes
        activity: dict[tuple[str, str], dict[str, int]] = {}
        for path in (self.hot_path, *self._selected_segment_paths(since=None)):
            with self._query_connection(path) as connection:
                for table_name, key in (
                    ("slot_events", "event_count"),
                    ("metric_samples", "metric_sample_count"),
                    ("metric_rollups", "metric_rollup_count"),
                ):
                    for row in connection.execute(
                        f"""
                        SELECT system_id, enclosure_key, COUNT(*) AS row_count
                        FROM {table_name}
                        GROUP BY system_id, enclosure_key
                        """
                    ).fetchall():
                        values = activity.setdefault(
                            (str(row["system_id"]), str(row["enclosure_key"])),
                            {
                                "event_count": 0,
                                "metric_sample_count": 0,
                                "metric_rollup_count": 0,
                            },
                        )
                        values[key] += int(row["row_count"])
        for scope in scopes:
            values = activity.get(
                (str(scope["system_id"]), str(scope["enclosure_key"])),
                {
                    "event_count": 0,
                    "metric_sample_count": 0,
                    "metric_rollup_count": 0,
                },
            )
            scope.update(values)
        return scopes

    def list_history_system_summaries(
        self,
        exclude_system_ids: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        excludes = {
            system_id.strip()
            for system_id in exclude_system_ids
            if system_id and system_id.strip()
        }
        summaries: dict[str, dict[str, Any]] = {}
        for path in (self.hot_path, *self._selected_segment_paths(since=None)):
            with self._query_connection(path) as connection:
                for table_name, key in (
                    ("slot_state_current", "tracked_slots"),
                    ("slot_events", "event_count"),
                    ("metric_samples", "metric_sample_count"),
                    ("metric_rollups", "metric_rollup_count"),
                ):
                    for row in connection.execute(
                        f"""
                        SELECT system_id, MAX(system_label) AS system_label,
                               COUNT(*) AS row_count
                        FROM {table_name}
                        GROUP BY system_id
                        """
                    ).fetchall():
                        system_id = str(row["system_id"])
                        if system_id in excludes:
                            continue
                        summary = summaries.setdefault(
                            system_id,
                            {
                                "system_id": system_id,
                                "system_label": row["system_label"],
                                "tracked_slots": 0,
                                "event_count": 0,
                                "metric_sample_count": 0,
                                "metric_rollup_count": 0,
                            },
                        )
                        if row["system_label"]:
                            summary["system_label"] = row["system_label"]
                        summary[key] += int(row["row_count"])
        return sorted(
            summaries.values(),
            key=lambda summary: (
                str(summary.get("system_label") or "").casefold(),
                str(summary["system_id"]),
            ),
        )

    def list_slot_events(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        *,
        limit: int = 100,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._require_limit(limit)
        enclosure_key = enclosure_id or ""
        where_clauses = ["system_id = ?", "enclosure_key = ?", "slot = ?"]
        parameters: list[Any] = [system_id, enclosure_key, slot]
        if since is not None:
            where_clauses.append("julianday(observed_at) >= julianday(?)")
            parameters.append(since)
        parameters.append(limit)
        rows: list[dict[str, Any]] = []
        for path in (self.hot_path, *self._selected_segment_paths(since=since)):
            with self._query_connection(path) as connection:
                rows.extend(
                    dict(row)
                    for row in connection.execute(
                        f"""
                        SELECT *
                        FROM slot_events
                        WHERE {' AND '.join(where_clauses)}
                        ORDER BY julianday(observed_at) DESC, id DESC
                        LIMIT ?
                        """,
                        parameters,
                    ).fetchall()
                )
        return sorted(
            rows,
            key=lambda row: (_parse_catalog_timestamp(row["observed_at"]), int(row["id"])),
            reverse=True,
        )[:limit]

    def list_raw_metric_samples(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._require_limit(limit)
        enclosure_key = enclosure_id or ""
        base_where_clauses = ["system_id = ?", "enclosure_key = ?", "slot = ?"]
        base_parameters: list[Any] = [system_id, enclosure_key, slot]
        if metric_name:
            base_where_clauses.append("metric_name = ?")
            base_parameters.append(metric_name)
        return self._list_raw_metric_samples_by_filter(
            where_clauses=base_where_clauses,
            parameters=base_parameters,
            limit=limit,
            since=since,
        )

    def _list_raw_metric_samples_by_filter(
        self,
        *,
        where_clauses: list[str],
        parameters: list[Any],
        limit: int,
        since: str | None,
    ) -> list[dict[str, Any]]:
        query_where_clauses = list(where_clauses)
        query_parameters = list(parameters)
        if since:
            query_where_clauses.append("julianday(observed_at) >= julianday(?)")
            query_parameters.append(since)
        query_parameters.append(limit)
        query = f"""
            SELECT *
            FROM metric_samples
            WHERE {' AND '.join(query_where_clauses)}
            ORDER BY julianday(observed_at) DESC, id DESC
            LIMIT ?
        """
        rows: list[dict[str, Any]] = []
        for path in (self.hot_path, *self._selected_segment_paths(since=since)):
            with self._query_connection(path) as connection:
                rows.extend(dict(row) for row in connection.execute(query, query_parameters).fetchall())
        for row in rows:
            row["value"] = row["value_integer"] if row["value_integer"] is not None else row["value_real"]
        return sorted(
            rows,
            key=lambda row: (_parse_catalog_timestamp(row["observed_at"]), int(row["id"])),
            reverse=True,
        )[:limit]

    def list_metric_samples(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._require_limit(limit)
        enclosure_key = enclosure_id or ""
        base_where_clauses = ["system_id = ?", "enclosure_key = ?", "slot = ?"]
        base_parameters: list[Any] = [system_id, enclosure_key, slot]
        if metric_name:
            base_where_clauses.append("metric_name = ?")
            base_parameters.append(metric_name)
        return self._list_metric_samples_by_filter(
            where_clauses=base_where_clauses,
            parameters=base_parameters,
            limit=limit,
            since=since,
        )

    def list_disk_metric_samples(
        self,
        disk_identity_key: str,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []
        limit = self._require_limit(limit)
        where_clauses = ["disk_identity_key = ?"]
        parameters: list[Any] = [normalized_identity_key]
        if metric_name:
            where_clauses.append("metric_name = ?")
            parameters.append(metric_name)
        return self._list_metric_samples_by_filter(
            where_clauses=where_clauses,
            parameters=parameters,
            limit=limit,
            since=since,
        )

    def list_disk_metric_homes(
        self,
        disk_identity_key: str,
        *,
        since: str | None = None,
        limit: int = MAX_HISTORY_QUERY_LIMIT,
    ) -> list[dict[str, Any]]:
        limit = self._require_limit(limit)
        normalized_identity_key = disk_identity_key.strip()
        if not normalized_identity_key:
            return []
        raw_since_clause = "AND julianday(observed_at) >= julianday(?)" if since else ""
        rollup_since_clause = "AND julianday(bucket_start) >= julianday(?)" if since else ""
        parameters: list[Any] = [normalized_identity_key]
        if since:
            parameters.append(since)
        parameters.append(normalized_identity_key)
        if since:
            parameters.append(since)
        parameters.append(normalized_identity_key)
        if since:
            parameters.append(since)
        query = f"""
            SELECT
                system_id,
                MAX(system_label) AS system_label,
                enclosure_key,
                MAX(enclosure_id) AS enclosure_id,
                MAX(enclosure_label) AS enclosure_label,
                slot,
                MAX(slot_label) AS slot_label,
                strftime('%Y-%m-%dT%H:%M:%f+00:00', MIN(julianday(first_seen_at))) AS first_seen_at,
                strftime('%Y-%m-%dT%H:%M:%f+00:00', MAX(julianday(last_seen_at))) AS last_seen_at,
                SUM(sample_count) AS sample_count
            FROM (
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    observed_at AS first_seen_at,
                    observed_at AS last_seen_at,
                    1 AS sample_count
                FROM metric_samples
                WHERE disk_identity_key = ? {raw_since_clause}
                UNION ALL
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    bucket_start AS first_seen_at,
                    bucket_start AS last_seen_at,
                    sample_count
                FROM metric_rollups hourly
                WHERE disk_identity_key = ?
                  AND bucket_seconds = 3600
                  {rollup_since_clause}
                UNION ALL
                SELECT
                    system_id, system_label, enclosure_key, enclosure_id,
                    enclosure_label, slot, slot_label,
                    bucket_start AS first_seen_at,
                    bucket_start AS last_seen_at,
                    sample_count
                FROM metric_rollups daily
                WHERE disk_identity_key = ?
                  AND bucket_seconds = 86400
                  {rollup_since_clause}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM metric_rollups hourly
                      WHERE hourly.disk_identity_key = daily.disk_identity_key
                        AND hourly.system_id = daily.system_id
                        AND hourly.enclosure_key = daily.enclosure_key
                        AND hourly.slot = daily.slot
                        AND hourly.bucket_seconds = 3600
                        AND substr(hourly.bucket_start, 1, 10) = substr(daily.bucket_start, 1, 10)
                  )
            ) history
            GROUP BY system_id, enclosure_key, slot
            ORDER BY MIN(julianday(first_seen_at)), MAX(julianday(last_seen_at))
            LIMIT ?
        """
        merged: dict[tuple[str, str, int], dict[str, Any]] = {}
        for path in (self.hot_path, *self._selected_segment_paths(since=since)):
            with self._query_connection(path) as connection:
                for row in connection.execute(query, [*parameters, limit]).fetchall():
                    item = dict(row)
                    key = (str(item["system_id"]), str(item["enclosure_key"]), int(item["slot"]))
                    existing = merged.get(key)
                    if existing is None:
                        merged[key] = item
                        continue
                    item_first = _parse_catalog_timestamp(item["first_seen_at"])
                    existing_first = _parse_catalog_timestamp(existing["first_seen_at"])
                    if item_first < existing_first:
                        existing["first_seen_at"] = item["first_seen_at"]
                    item_last = _parse_catalog_timestamp(item["last_seen_at"])
                    existing_last = _parse_catalog_timestamp(existing["last_seen_at"])
                    if item_last > existing_last:
                        for label in (
                            "system_label",
                            "enclosure_id",
                            "enclosure_label",
                            "slot_label",
                            "last_seen_at",
                        ):
                            existing[label] = item[label]
                    existing["sample_count"] = int(existing["sample_count"]) + int(item["sample_count"])
        return sorted(
            merged.values(),
            key=lambda item: (
                _parse_catalog_timestamp(item["first_seen_at"]),
                _parse_catalog_timestamp(item["last_seen_at"]),
                str(item["system_id"]),
                str(item["enclosure_key"]),
                int(item["slot"]),
            ),
        )[:limit]

    def list_followed_metric_samples(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        disk_identity_key: str,
        *,
        metric_name: str | None = None,
        limit: int = 500,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._require_limit(limit)
        disk_samples = self.list_disk_metric_samples(
            disk_identity_key,
            metric_name=metric_name,
            limit=limit,
            since=since,
        )
        local_samples = self.list_metric_samples(
            system_id,
            enclosure_id,
            slot,
            metric_name=metric_name,
            limit=limit,
            since=since,
        )
        merged_by_key: dict[Any, dict[str, Any]] = {}
        for item in [*disk_samples, *local_samples]:
            item_id = item.get("id")
            if item_id is not None:
                key: Any = ("id", item_id)
            else:
                key = (
                    item.get("observed_at"),
                    item.get("metric_name"),
                    item.get("system_id"),
                    item.get("enclosure_key"),
                    item.get("slot"),
                    item.get("value"),
                )
            merged_by_key[key] = item
        return sorted(
            merged_by_key.values(),
            key=lambda item: (
                _parse_catalog_timestamp(item.get("observed_at")),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )[:limit]

    def get_slot_history_bundle(
        self,
        system_id: str,
        enclosure_id: str | None,
        slot: int,
        *,
        event_limit: int = 12,
        metric_limits: dict[str, int] | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        enclosure_key = enclosure_id or ""
        with self._read_only_connection(self.hot_path) as connection:
            current_row = connection.execute(
                """
                SELECT *
                FROM slot_state_current
                WHERE system_id = ? AND enclosure_key = ? AND slot = ?
                """,
                (system_id, enclosure_key, slot),
            ).fetchone()
        current = dict(current_row) if current_row is not None else None
        events = self.list_slot_events(
            system_id,
            enclosure_id,
            slot,
            limit=event_limit,
        )
        metric_limits = metric_limits or {}
        metrics: dict[str, list[dict[str, Any]]] = {}
        latest_values: dict[str, Any] = {}
        sample_counts: dict[str, int] = {}
        disk_history: dict[str, Any] = {
            "identity_available": False,
            "followed": False,
            "serial": current.get("serial") if current else None,
            "persistent_id_label": current.get("persistent_id_label") if current else None,
            "persistent_id": current.get("gptid") if current else None,
            "current_home": None,
            "homes": [],
            "prior_home_count": 0,
            "window_limited": bool(since),
        }
        disk_identity_key = current.get("disk_identity_key") if current else None
        for metric_name, limit in metric_limits.items():
            samples = (
                self.list_followed_metric_samples(
                    system_id,
                    enclosure_id,
                    slot,
                    str(disk_identity_key),
                    metric_name=metric_name,
                    limit=limit,
                    since=since,
                )
                if disk_identity_key
                else self.list_metric_samples(
                    system_id,
                    enclosure_id,
                    slot,
                    metric_name=metric_name,
                    limit=limit,
                    since=since,
                )
            )
            metrics[metric_name] = samples
            latest_values[metric_name] = samples[0].get("value") if samples else None
            sample_counts[metric_name] = len(samples)
        if disk_identity_key:
            homes = self.list_disk_metric_homes(str(disk_identity_key), since=since)
            current_home_key = (system_id, enclosure_key, slot)

            def home_scope_key(home: dict[str, Any]) -> tuple[str, str, int]:
                slot_value = home.get("slot")
                normalized_slot = int(slot_value) if slot_value is not None else -1
                return (
                    str(home.get("system_id") or ""),
                    str(home.get("enclosure_key") or ""),
                    normalized_slot,
                )

            disk_history["identity_available"] = True
            disk_history["homes"] = homes
            disk_history["current_home"] = next(
                (home for home in homes if home_scope_key(home) == current_home_key),
                None,
            )
            disk_history["prior_home_count"] = sum(
                1 for home in homes if home_scope_key(home) != current_home_key
            )
            disk_history["followed"] = bool(disk_history["prior_home_count"])
        return {
            "events": events,
            "metrics": metrics,
            "sample_counts": sample_counts,
            "latest_values": latest_values,
            "disk_history": disk_history,
        }

    def list_scope_history(
        self,
        system_id: str,
        enclosure_id: str | None,
        *,
        slots: list[int] | None = None,
        event_limit: int = 12,
        metric_limits: dict[str, int] | None = None,
        since: str | None = None,
    ) -> dict[int, dict[str, Any]]:
        if type(event_limit) is not int or not 0 <= event_limit <= MAX_HISTORY_QUERY_LIMIT:
            raise ValueError("Segmented history query limit is invalid.")
        enclosure_key = enclosure_id or ""
        slot_numbers = sorted({int(slot) for slot in (slots or [])})
        metric_limits = metric_limits or {}
        for limit in metric_limits.values():
            self._require_limit(limit)
        requested_slots = bool(slot_numbers)
        discovered_slots = set(slot_numbers)
        where_clauses = ["system_id = ?", "enclosure_key = ?"]
        parameters: list[Any] = [system_id, enclosure_key]
        if requested_slots:
            placeholders = ", ".join("?" for _ in slot_numbers)
            where_clauses.append(f"slot IN ({placeholders})")
            parameters.extend(slot_numbers)
        scope_where = " AND ".join(where_clauses)
        events_by_slot: dict[int, list[dict[str, Any]]] = {}
        raw_by_metric_slot: dict[str, dict[int, list[dict[str, Any]]]] = {
            metric_name: {} for metric_name in metric_limits
        }
        rollups_by_metric_interval_slot: dict[str, dict[int, dict[int, list[dict[str, Any]]]]] = {
            metric_name: {3600: {}, 86400: {}} for metric_name in metric_limits
        }
        for path in (self.hot_path, *self._selected_segment_paths(since=since)):
            with self._query_connection(path) as connection:
                if not requested_slots:
                    rows = connection.execute(
                        """
                        SELECT slot FROM slot_state_current
                        WHERE system_id = ? AND enclosure_key = ?
                        UNION SELECT slot FROM slot_events
                        WHERE system_id = ? AND enclosure_key = ?
                        UNION SELECT slot FROM metric_samples
                        WHERE system_id = ? AND enclosure_key = ?
                        UNION SELECT slot FROM metric_rollups
                        WHERE system_id = ? AND enclosure_key = ?
                        """,
                        (system_id, enclosure_key) * 4,
                    ).fetchall()
                    discovered_slots.update(int(row[0]) for row in rows)
                if event_limit > 0:
                    rows = connection.execute(
                        f"""
                        SELECT * FROM (
                            SELECT *, ROW_NUMBER() OVER (
                                PARTITION BY slot
                                ORDER BY julianday(observed_at) DESC, id DESC
                            ) AS row_number
                            FROM slot_events WHERE {scope_where}
                        ) WHERE row_number <= ?
                        """,
                        [*parameters, event_limit],
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        item.pop("row_number", None)
                        events_by_slot.setdefault(int(item["slot"]), []).append(item)
                for metric_name, limit in metric_limits.items():
                    metric_where = [*where_clauses, "metric_name = ?"]
                    metric_parameters = [*parameters, metric_name]
                    if since:
                        metric_where.append("julianday(observed_at) >= julianday(?)")
                        metric_parameters.append(since)
                    rows = connection.execute(
                        f"""
                        SELECT * FROM (
                            SELECT *, ROW_NUMBER() OVER (
                                PARTITION BY slot, metric_name
                                ORDER BY julianday(observed_at) DESC, id DESC
                            ) AS row_number
                            FROM metric_samples
                            WHERE {' AND '.join(metric_where)}
                        ) WHERE row_number <= ?
                        """,
                        [*metric_parameters, limit],
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        item.pop("row_number", None)
                        item["value"] = (
                            item["value_integer"]
                            if item["value_integer"] is not None
                            else item["value_real"]
                        )
                        raw_by_metric_slot[metric_name].setdefault(int(item["slot"]), []).append(item)
                    for bucket_seconds in (3600, 86400):
                        rollup_where = [*where_clauses, "metric_name = ?", "bucket_seconds = ?"]
                        rollup_parameters: list[Any] = [*parameters, metric_name, bucket_seconds]
                        if since:
                            rollup_where.append("julianday(bucket_start) >= julianday(?)")
                            rollup_parameters.append(since)
                        rows = connection.execute(
                            f"""
                            SELECT * FROM (
                                SELECT
                                    NULL AS id,
                                    CASE WHEN metric_name IN (
                                        'bytes_read', 'bytes_written', 'power_on_hours'
                                    ) THEN last_observed_at ELSE bucket_start END AS observed_at,
                                    bucket_start AS _bucket_start,
                                    system_id, system_label, enclosure_key, enclosure_id,
                                    enclosure_label, slot, slot_label, metric_name,
                                    NULL AS value_integer,
                                    CASE WHEN metric_name IN (
                                        'bytes_read', 'bytes_written', 'power_on_hours'
                                    ) THEN last_value ELSE value_sum / sample_count END AS value_real,
                                    device_name, serial, model, state, gptid,
                                    persistent_id_label, NULLIF(disk_identity_key, '') AS disk_identity_key,
                                    logical_unit_id, sas_address,
                                    bucket_seconds AS rollup_seconds,
                                    sample_count, value_min, value_max,
                                    value_sum AS _value_sum,
                                    last_value AS _last_value,
                                    last_observed_at AS _last_observed_at,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY slot, metric_name
                                        ORDER BY julianday(bucket_start) DESC
                                    ) AS row_number
                                FROM metric_rollups
                                WHERE {' AND '.join(rollup_where)}
                            ) WHERE row_number <= ?
                            """,
                            [*rollup_parameters, limit],
                        ).fetchall()
                        for row in rows:
                            item = dict(row)
                            item.pop("row_number", None)
                            item["value"] = item["value_real"]
                            rollups_by_metric_interval_slot[metric_name][bucket_seconds].setdefault(
                                int(item["slot"]), []
                            ).append(item)
        payload_by_slot: dict[int, dict[str, Any]] = {}
        for slot in sorted(discovered_slots):
            events = sorted(
                events_by_slot.get(slot, []),
                key=lambda item: (
                    _parse_catalog_timestamp(item["observed_at"]),
                    int(item["id"]),
                ),
                reverse=True,
            )[:event_limit]
            metrics: dict[str, list[dict[str, Any]]] = {}
            sample_counts: dict[str, int] = {}
            latest_values: dict[str, Any] = {}
            for metric_name, limit in metric_limits.items():
                samples = sorted(
                    raw_by_metric_slot[metric_name].get(slot, []),
                    key=lambda item: (
                        _parse_catalog_timestamp(item["observed_at"]),
                        int(item["id"]),
                    ),
                    reverse=True,
                )[:limit]
                before = str(samples[-1]["observed_at"]) if samples else None
                hourly_added = False
                for bucket_seconds in (3600, 86400):
                    if len(samples) >= limit:
                        break
                    boundary = None
                    if before:
                        boundary = (
                            _utc_day_start(before)
                            if bucket_seconds == 86400 and hourly_added
                            else before
                        )
                    candidates = sorted(
                        self._merge_rollup_rows(
                            rollups_by_metric_interval_slot[metric_name][bucket_seconds].get(slot, [])
                        ),
                        key=lambda item: _parse_catalog_timestamp(item["_bucket_start"]),
                        reverse=True,
                    )
                    additions = [
                        item
                        for item in candidates
                        if boundary is None
                        or _parse_catalog_timestamp(item["_bucket_start"])
                        < _parse_catalog_timestamp(boundary)
                    ][: max(0, limit - len(samples))]
                    for item in additions:
                        item.pop("_bucket_start", None)
                    samples.extend(additions)
                    if additions:
                        hourly_added = hourly_added or bucket_seconds == 3600
                        before = str(additions[-1]["observed_at"])
                metrics[metric_name] = samples
                sample_counts[metric_name] = len(samples)
                latest_values[metric_name] = samples[0].get("value") if samples else None
            payload_by_slot[slot] = {
                "events": events,
                "metrics": metrics,
                "sample_counts": sample_counts,
                "latest_values": latest_values,
            }
        return payload_by_slot

    def _list_metric_samples_by_filter(
        self,
        *,
        where_clauses: list[str],
        parameters: list[Any],
        limit: int,
        since: str | None,
    ) -> list[dict[str, Any]]:
        samples = self._list_raw_metric_samples_by_filter(
            where_clauses=where_clauses,
            parameters=parameters,
            limit=limit,
            since=since,
        )
        if len(samples) >= limit:
            return samples[:limit]

        before = str(samples[-1]["observed_at"]) if samples else None
        hourly_rollup_added = False
        for bucket_seconds in (3600, 86400):
            remaining = limit - len(samples)
            if remaining <= 0:
                break
            rollup_where = [*where_clauses, "bucket_seconds = ?"]
            rollup_parameters: list[Any] = [*parameters, bucket_seconds]
            if since is not None:
                rollup_where.append("julianday(bucket_start) >= julianday(?)")
                rollup_parameters.append(since)
            if before:
                boundary = (
                    _utc_day_start(before)
                    if bucket_seconds == 86400 and hourly_rollup_added
                    else before
                )
                rollup_where.append("julianday(bucket_start) < julianday(?)")
                rollup_parameters.append(boundary)
            rollup_parameters.append(remaining)
            query = f"""
                SELECT
                    NULL AS id,
                    CASE
                        WHEN metric_name IN ('bytes_read', 'bytes_written', 'power_on_hours')
                        THEN last_observed_at
                        ELSE bucket_start
                    END AS observed_at,
                    bucket_start AS _bucket_start,
                    system_id,
                    system_label,
                    enclosure_key,
                    enclosure_id,
                    enclosure_label,
                    slot,
                    slot_label,
                    metric_name,
                    NULL AS value_integer,
                    CASE
                        WHEN metric_name IN ('bytes_read', 'bytes_written', 'power_on_hours')
                        THEN last_value
                        ELSE value_sum / sample_count
                    END AS value_real,
                    device_name,
                    serial,
                    model,
                    state,
                    gptid,
                    persistent_id_label,
                    NULLIF(disk_identity_key, '') AS disk_identity_key,
                    logical_unit_id,
                    sas_address,
                    bucket_seconds AS rollup_seconds,
                    sample_count,
                    value_min,
                    value_max,
                    value_sum AS _value_sum,
                    last_value AS _last_value,
                    last_observed_at AS _last_observed_at
                FROM metric_rollups
                WHERE {' AND '.join(rollup_where)}
                ORDER BY julianday(bucket_start) DESC
                LIMIT ?
            """
            rollups: list[dict[str, Any]] = []
            for path in (self.hot_path, *self._selected_segment_paths(since=since)):
                with self._query_connection(path) as connection:
                    rollups.extend(dict(row) for row in connection.execute(query, rollup_parameters).fetchall())
            rollups = self._merge_rollup_rows(rollups)
            rollups.sort(
                key=lambda row: _parse_catalog_timestamp(row["_bucket_start"]),
                reverse=True,
            )
            rollups = rollups[:remaining]
            for rollup in rollups:
                rollup["value"] = rollup["value_real"]
                rollup.pop("_bucket_start", None)
            samples.extend(rollups)
            if rollups:
                if bucket_seconds == 3600:
                    hourly_rollup_added = True
                before = str(rollups[-1]["observed_at"])
        return samples[:limit]
