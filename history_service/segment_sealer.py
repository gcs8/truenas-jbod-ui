from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from history_service.segment_catalog import MAX_HISTORY_SEGMENT_BYTES, SEGMENT_ID_PATTERN

HISTORY_TABLE_TIMESTAMPS = {
    "slot_events": "observed_at",
    "metric_samples": "observed_at",
    "metric_rollups": "bucket_start",
}
SEGMENT_DIRECTORY_MODE = 0o750
SEGMENT_FILE_MODE = 0o640


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp with an offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp with an offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be an ISO-8601 timestamp with an offset")
    return parsed


def normalize_history_cutoff(value: str) -> str:
    return (
        _parse_timestamp(value, label="History segment cutoff")
        .astimezone(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )


def _history_coverage(connection: sqlite3.Connection) -> tuple[str, str] | None:
    earliest: tuple[datetime, str] | None = None
    latest: tuple[datetime, str] | None = None
    for table_name, timestamp_column in HISTORY_TABLE_TIMESTAMPS.items():
        rows = connection.execute(
            f"SELECT {timestamp_column}, julianday({timestamp_column}) "
            f"FROM {table_name} WHERE {timestamp_column} IS NOT NULL"
        )
        for raw_value, sqlite_value in rows:
            parsed = _parse_timestamp(raw_value, label="History segment row timestamp")
            if sqlite_value is None:
                raise ValueError("History segment row timestamp is unsupported by SQLite.")
            candidate = (parsed, str(raw_value))
            if earliest is None or candidate[0] < earliest[0]:
                earliest = candidate
            if latest is None or candidate[0] > latest[0]:
                latest = candidate
    if earliest is None or latest is None:
        return None
    return earliest[1], latest[1]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_regular_source(source: Path) -> os.stat_result:
    metadata = os.stat(source, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("History segment source must be a regular file.")
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.stat(f"{source}{suffix}", follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise ValueError("History segment source has SQLite sidecar state and must be quiesced first.")
    return metadata


def _require_source_owner(source_metadata: os.stat_result) -> None:
    if os.geteuid() != source_metadata.st_uid:
        raise ValueError("History segment publisher must own the hot history database.")


def _require_output_directory(
    output_directory: Path,
    *,
    source_metadata: os.stat_result | None = None,
    repair: bool = False,
) -> Path:
    try:
        descriptor = os.open(
            output_directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("History segment output directory must be a directory.") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("History segment output directory must be a directory.")
        if source_metadata is not None:
            if metadata.st_uid != source_metadata.st_uid:
                raise ValueError(
                    "History segment output directory must be owned by the hot history database owner."
                )
            if repair:
                os.fchown(descriptor, source_metadata.st_uid, source_metadata.st_gid)
                os.fchmod(descriptor, SEGMENT_DIRECTORY_MODE)
                metadata = os.fstat(descriptor)
            if (
                metadata.st_uid,
                metadata.st_gid,
                stat.S_IMODE(metadata.st_mode),
            ) != (
                source_metadata.st_uid,
                source_metadata.st_gid,
                SEGMENT_DIRECTORY_MODE,
            ):
                raise ValueError("History segment output directory permission policy is invalid.")
    finally:
        os.close(descriptor)
    return output_directory


def _prepare_output_directory(
    output_directory: Path,
    source_metadata: os.stat_result,
) -> Path:
    output_directory.mkdir(mode=SEGMENT_DIRECTORY_MODE, parents=True, exist_ok=True)
    return _require_output_directory(
        output_directory,
        source_metadata=source_metadata,
        repair=True,
    )


def _copy_and_prune(source: Path, destination: Path, cutoff: str) -> dict[str, int]:
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        source_connection.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA journal_mode = DELETE")
            _history_coverage(destination_connection)
            with destination_connection:
                for table_name, timestamp_column in HISTORY_TABLE_TIMESTAMPS.items():
                    destination_connection.execute(
                        f"DELETE FROM {table_name} WHERE julianday({timestamp_column}) >= julianday(?)",
                        (cutoff,),
                    )
                destination_connection.execute("DELETE FROM slot_state_current")
            destination_connection.execute("VACUUM")
            quick_check = destination_connection.execute("PRAGMA quick_check").fetchone()
            if quick_check != ("ok",):
                raise ValueError("History segment SQLite integrity check failed.")
            return {
                table_name: int(
                    destination_connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                )
                for table_name in HISTORY_TABLE_TIMESTAMPS
            }


def _segment_coverage(path: Path) -> tuple[str, str]:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        coverage = _history_coverage(connection)
    if coverage is None:
        raise ValueError("History segment contains no historical rows before the cutoff.")
    return coverage


def seal_history_segment(
    *,
    source: Path,
    output_directory: Path,
    segment_id: str,
    cutoff: str,
    key_id: str,
    sequence: int = 1,
    before_publish: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
        raise ValueError("History segment ID is invalid.")
    if not key_id or len(key_id) > 128:
        raise ValueError("History segment key ID is invalid.")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("History segment sequence is invalid.")
    cutoff = normalize_history_cutoff(cutoff)
    source = source.absolute()
    source_metadata = _require_regular_source(source)
    _require_source_owner(source_metadata)
    output_directory = _prepare_output_directory(output_directory.absolute(), source_metadata)
    destination = output_directory / f"{segment_id}.sqlite3"
    try:
        destination_metadata = os.stat(destination, follow_symlinks=False)
    except FileNotFoundError:
        destination_metadata = None
    if destination_metadata is not None:
        raise ValueError("History segment destination already exists.")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".segment-", suffix=".sqlite3", dir=output_directory)
    temporary_path = Path(temporary_name)
    try:
        os.fchown(descriptor, source_metadata.st_uid, source_metadata.st_gid)
        os.fchmod(descriptor, SEGMENT_FILE_MODE)
        temporary_metadata = os.fstat(descriptor)
        if (
            temporary_metadata.st_uid,
            temporary_metadata.st_gid,
            stat.S_IMODE(temporary_metadata.st_mode),
        ) != (
            source_metadata.st_uid,
            source_metadata.st_gid,
            SEGMENT_FILE_MODE,
        ):
            raise ValueError("History segment publication permission policy could not be applied.")
        os.close(descriptor)
        descriptor = -1
        row_counts = _copy_and_prune(source, temporary_path, cutoff)
        if os.stat(source, follow_symlinks=False) != source_metadata:
            raise ValueError("History segment source changed while it was being sealed.")
        coverage_start, coverage_end = _segment_coverage(temporary_path)
        size_bytes = temporary_path.stat().st_size
        if size_bytes > MAX_HISTORY_SEGMENT_BYTES:
            raise ValueError("History segment exceeds its byte limit.")
        sha256 = _sha256_file(temporary_path)
        with temporary_path.open("rb", buffering=0) as stream:
            os.fsync(stream.fileno())
        if before_publish is not None:
            before_publish(
                {
                    "file_name": destination.name,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                }
            )
        os.link(temporary_path, destination)
        temporary_path.unlink()
        _fsync_directory(output_directory)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return {
        "segment_id": segment_id,
        "path": str(destination),
        "sequence": sequence,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "sealed_at": cutoff,
        "key_id": key_id,
        "row_counts": row_counts,
        "size_bytes": size_bytes,
        "sha256": sha256,
    }
