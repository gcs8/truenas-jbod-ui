from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from history_service.migration_lock import history_write_lock
from history_service.scheduled_backup import read_scheduled_backup_status
from history_service.segment_catalog import activation_pending_path, path_entry_exists
from history_service.segment_migration import (
    _fsync_directory,
    _remove_authenticated_orphan_segment,
    _sha256_file,
    _stage_hot_replacement,
    _write_private_json_no_replace,
    _write_private_json_replace,
)
from history_service.segment_reader import MAX_SEGMENTS_PER_QUERY, SegmentedHistoryReader
from history_service.segment_sealer import (
    HISTORY_TABLE_TIMESTAMPS,
    _require_regular_source,
    seal_history_segment,
)

ROTATION_JOURNAL_PHASES = (
    "prepared",
    "segment-published",
    "hot-staged",
    "hot-replaced",
    "catalog-replaced",
    "cleanup",
)

_GENERATION_ID = re.compile(r"generation-(?P<sequence>[0-9]{4})\Z")
_SEGMENT_ID = re.compile(r"segment-(?P<sequence>[0-9]{4})\Z")
_MAX_BACKUP_AGE = timedelta(hours=36)
_HEADROOM_SAFETY_BYTES = 1024 * 1024
_JOURNAL_VERSION = 1
_MAX_JOURNAL_BYTES = 1024 * 1024


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("Segment rotation journal contains a duplicate JSON key.")
        payload[key] = value
    return payload


def _record_file(path: Path, *, final_name: str | None = None) -> dict[str, Any]:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Segment rotation artifact must be a single-link regular file.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != before_identity or (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_mode,
            path_metadata.st_nlink,
            path_metadata.st_size,
            path_metadata.st_mtime_ns,
            path_metadata.st_ctime_ns,
        ) != before_identity:
            raise ValueError("Segment rotation artifact changed while it was being hashed.")
    finally:
        os.close(descriptor)
    record: dict[str, Any] = {
        "file_name": path.name,
        "sha256": digest.hexdigest(),
        "size_bytes": before.st_size,
    }
    if final_name is not None:
        record["final_name"] = final_name
    return record


def _record_path(root: Path, record: Any, *, label: str, final: bool = False) -> Path:
    if not isinstance(record, dict):
        raise ValueError(f"Segment rotation {label} record is invalid.")
    field = "final_name" if final else "file_name"
    file_name = record.get(field)
    if (
        not isinstance(file_name, str)
        or not file_name
        or file_name in {".", ".."}
        or Path(file_name).name != file_name
    ):
        raise ValueError(f"Segment rotation {label} record is invalid.")
    return root / file_name


def _file_matches(path: Path, record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    size_bytes = record.get("size_bytes")
    expected_sha256 = record.get("sha256")
    if (
        type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        return False
    try:
        actual = _record_file(path)
    except (OSError, ValueError):
        return False
    return actual["size_bytes"] == size_bytes and actual["sha256"] == expected_sha256


def _require_file_matches(path: Path, record: Any, *, label: str) -> None:
    if not _file_matches(path, record):
        raise ValueError(f"Segment rotation {label} integrity check failed.")


def _retire_authenticated_path(
    path: Path,
    *,
    authenticate: Callable[[Path], None],
) -> None:
    root = path.parent
    quarantine_directory = Path(tempfile.mkdtemp(prefix=".rotation-retired-", dir=root))
    os.chmod(quarantine_directory, 0o700)
    quarantine_path = quarantine_directory / path.name
    try:
        os.replace(path, quarantine_path)
        _fsync_directory(root)
        try:
            authenticate(quarantine_path)
        except BaseException:
            if not path_entry_exists(path):
                os.replace(quarantine_path, path)
                _fsync_directory(root)
            raise
        quarantine_path.unlink()
        _fsync_directory(quarantine_directory)
    finally:
        try:
            quarantine_directory.rmdir()
        except OSError:
            pass
        _fsync_directory(root)


def _remove_created_file(path: Path, expected: os.stat_result, *, label: str) -> None:
    def authenticate(candidate: Path) -> None:
        metadata = os.stat(candidate, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValueError(f"Segment rotation {label} changed before cleanup.")

    _retire_authenticated_path(path, authenticate=authenticate)


def _copy_file_no_replace(source: Path, destination: Path) -> dict[str, Any]:
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor = -1
    created = False
    created_metadata: os.stat_result | None = None
    try:
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode) or source_metadata.st_nlink != 1:
            raise ValueError("Segment rotation source artifact is invalid.")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.fchmod(destination_descriptor, 0o600)
        created_metadata = os.fstat(destination_descriptor)
        while chunk := os.read(source_descriptor, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("Segment rotation copy was incomplete.")
                view = view[written:]
        os.fsync(destination_descriptor)
    except BaseException:
        if created and created_metadata is not None and path_entry_exists(destination):
            _remove_created_file(destination, created_metadata, label="partial copy")
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    try:
        _fsync_directory(destination.parent)
        source_record = _record_file(source)
        destination_record = _record_file(destination)
        if (
            source_record["sha256"] != destination_record["sha256"]
            or source_record["size_bytes"] != destination_record["size_bytes"]
        ):
            raise ValueError("Segment rotation copy integrity check failed.")
    except BaseException:
        if created_metadata is not None and path_entry_exists(destination):
            _remove_created_file(destination, created_metadata, label="unverified copy")
        try:
            _fsync_directory(destination.parent)
        except OSError:
            pass
        raise
    return destination_record


def _stage_catalog(segments_directory: Path, payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".rotation-catalog-",
        suffix=".json",
        dir=segments_directory,
    )
    path = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    created_metadata = os.fstat(descriptor)
    try:
        content = _canonical_json_bytes(payload)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if path_entry_exists(path):
            _remove_created_file(path, created_metadata, label="partial catalog stage")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path, _record_file(path, final_name="catalog.json")


def _replace_hot(staged_hot_path: Path, source: Path, *, mode: int) -> None:
    os.replace(staged_hot_path, source)
    os.chmod(source, mode)
    _fsync_directory(source.parent)


def _replace_catalog(staged_catalog_path: Path, catalog_path: Path) -> None:
    os.replace(staged_catalog_path, catalog_path)
    _fsync_directory(catalog_path.parent)


def _write_rotation_journal(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path_entry_exists(path):
        _write_private_json_replace(path, payload, temporary_prefix=".rotation-journal-")
    else:
        _write_private_json_no_replace(path, payload, temporary_prefix=".rotation-journal-")
    return _record_file(path)


def _require_output_directory(path: Path) -> Path:
    metadata = os.stat(path, follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("History segment output directory must be a directory.")
    return path


def _read_rotation_journal(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_JOURNAL_BYTES
        ):
            raise ValueError("Segment rotation journal is invalid.")
        content = os.read(descriptor, _MAX_JOURNAL_BYTES + 1)
        if len(content) > _MAX_JOURNAL_BYTES or os.read(descriptor, 1):
            raise ValueError("Segment rotation journal is invalid.")
        after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns)
            != (metadata.st_dev, metadata.st_ino, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError("Segment rotation journal changed while it was being read.")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Segment rotation journal is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("journal_version") != _JOURNAL_VERSION
        or payload.get("operation") != "rotate"
        or payload.get("phase") not in ROTATION_JOURNAL_PHASES
    ):
        raise ValueError("Segment rotation journal is invalid.")
    return payload, {
        "file_name": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _remove_journal(path: Path, record: dict[str, Any] | None = None) -> None:
    _remove_recorded_file(
        path.parent,
        record if record is not None else _record_file(path),
        label="journal",
    )


def _remove_recorded_file(
    root: Path,
    record: Any,
    *,
    label: str,
    allow_missing: bool = False,
) -> None:
    path = _record_path(root, record, label=label)
    if not path_entry_exists(path):
        if allow_missing:
            return
        raise ValueError(f"Segment rotation {label} integrity check failed.")
    _retire_authenticated_path(
        path,
        authenticate=lambda candidate: _require_file_matches(candidate, record, label=label),
    )


def _restore_hot(source: Path, rollback_record: dict[str, Any], *, mode: int) -> None:
    rollback_path = _record_path(source.parent, rollback_record, label="prior hot rollback")
    _require_file_matches(rollback_path, rollback_record, label="prior hot rollback")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.rotation-restore-",
        suffix=".sqlite3",
        dir=source.parent,
    )
    temporary_path = Path(temporary_name)
    os.fchmod(descriptor, mode)
    reservation_metadata = os.fstat(descriptor)
    os.close(descriptor)
    _remove_created_file(temporary_path, reservation_metadata, label="restore reservation")
    restored_record: dict[str, Any] | None = None
    try:
        restored_record = _copy_file_no_replace(rollback_path, temporary_path)
        if (
            restored_record["sha256"] != rollback_record["sha256"]
            or restored_record["size_bytes"] != rollback_record["size_bytes"]
        ):
            raise ValueError("Segment rotation prior hot rollback integrity check failed.")
        os.replace(temporary_path, source)
        os.chmod(source, mode)
        _fsync_directory(source.parent)
    finally:
        if restored_record is not None and path_entry_exists(temporary_path):
            _remove_recorded_file(
                source.parent,
                restored_record,
                label="staged hot restore",
            )


def _validated_catalog(
    source: Path,
    segments_directory: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    catalog_path = segments_directory / "catalog.json"
    before_record = _record_file(catalog_path)
    reader = SegmentedHistoryReader.from_catalog(
        hot_path=source,
        catalog_path=catalog_path,
    )
    reader.verify_catalog_segments()
    catalog = reader.catalog_payload()
    after_record = _record_file(catalog_path)
    if before_record != after_record:
        raise ValueError("Segment rotation catalog changed during verification.")
    if not isinstance(catalog, dict):
        raise ValueError("Segment rotation catalog is invalid.")
    return catalog_path, catalog, before_record


def _next_generation(catalog: dict[str, Any]) -> tuple[str, str, int]:
    generation_match = _GENERATION_ID.fullmatch(str(catalog.get("generation_id") or ""))
    segments = catalog.get("segments")
    if generation_match is None or not isinstance(segments, list):
        raise ValueError("Segment rotation catalog generation is invalid.")
    if len(segments) >= MAX_SEGMENTS_PER_QUERY:
        raise ValueError("Segment rotation would exceed the active segment limit.")
    sequences: list[int] = []
    for entry in segments:
        if not isinstance(entry, dict):
            raise ValueError("Segment rotation catalog is invalid.")
        segment_match = _SEGMENT_ID.fullmatch(str(entry.get("segment_id") or ""))
        if segment_match is None:
            raise ValueError("Segment rotation catalog segment sequence is invalid.")
        parsed_sequence = int(segment_match.group("sequence"))
        declared_sequence = entry.get("sequence", parsed_sequence)
        if type(declared_sequence) is not int or declared_sequence != parsed_sequence:
            raise ValueError("Segment rotation catalog segment sequence is invalid.")
        sequences.append(parsed_sequence)
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        raise ValueError("Segment rotation catalog segment sequence is invalid.")
    generation_sequence = int(generation_match.group("sequence"))
    if generation_sequence != len(sequences) or generation_sequence >= 9999:
        raise ValueError("Segment rotation catalog generation sequence is invalid.")
    next_sequence = generation_sequence + 1
    return (
        f"generation-{next_sequence:04d}",
        f"segment-{next_sequence:04d}",
        next_sequence,
    )


def _validate_backup_evidence(
    backup_directory: Path,
    status_path: Path,
) -> dict[str, Any]:
    directory_metadata = os.stat(backup_directory, follow_symlinks=False)
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        raise ValueError("Segment rotation backup directory is invalid.")
    status = read_scheduled_backup_status(status_path)
    if (
        status is None
        or status.get("last_error_code") is not None
        or "history_db" not in status.get("included_groups", [])
        or "history_db" in status.get("last_absent_groups", [])
    ):
        raise ValueError("Segment rotation requires a verified scheduled FULL backup.")
    raw_success_at = status.get("last_success_at")
    if not isinstance(raw_success_at, str):
        raise ValueError("Segment rotation backup timestamp is invalid.")
    try:
        success_at = datetime.fromisoformat(raw_success_at)
    except ValueError as exc:
        raise ValueError("Segment rotation backup timestamp is invalid.") from exc
    if success_at.tzinfo is None:
        raise ValueError("Segment rotation backup timestamp is invalid.")
    age = datetime.now(timezone.utc) - success_at.astimezone(timezone.utc)
    if age < timedelta(0) or age > _MAX_BACKUP_AGE:
        raise ValueError("Segment rotation scheduled FULL backup is stale.")
    artifact_name = status.get("last_artifact_name")
    if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
        raise ValueError("Segment rotation backup artifact is invalid.")
    artifact_path = backup_directory / artifact_name
    record = _record_file(artifact_path)
    if (
        record["size_bytes"] != status.get("last_size_bytes")
        or record["sha256"] != status.get("last_sha256")
    ):
        raise ValueError("Segment rotation backup artifact integrity check failed.")
    record["successful_at"] = success_at.astimezone(timezone.utc).isoformat()
    record["included_groups"] = list(status["included_groups"])
    return record


def _require_newer_backup(
    prior_catalog: dict[str, Any],
    backup_record: dict[str, Any],
) -> None:
    prior_backup = prior_catalog.get("rotation_backup")
    if prior_backup is None:
        return
    if not isinstance(prior_backup, dict):
        raise ValueError("Segment rotation prior backup evidence is invalid.")
    prior_success_at = prior_backup.get("successful_at")
    candidate_success_at = backup_record.get("successful_at")
    if not isinstance(prior_success_at, str) or not isinstance(candidate_success_at, str):
        raise ValueError("Segment rotation backup evidence is invalid.")
    try:
        prior_timestamp = datetime.fromisoformat(prior_success_at)
        candidate_timestamp = datetime.fromisoformat(candidate_success_at)
    except ValueError as exc:
        raise ValueError("Segment rotation backup evidence is invalid.") from exc
    if (
        prior_timestamp.tzinfo is None
        or candidate_timestamp.tzinfo is None
        or candidate_timestamp <= prior_timestamp
        or backup_record.get("sha256") == prior_backup.get("sha256")
        or backup_record.get("file_name") == prior_backup.get("file_name")
    ):
        raise ValueError("Segment rotation requires a newer verified FULL backup.")


def _require_headroom(source: Path, catalog_path: Path, segments_directory: Path) -> None:
    requirements: dict[int, tuple[Path, int]] = {}
    source_size = source.stat().st_size
    catalog_size = catalog_path.stat().st_size
    for path, required_bytes in (
        (source.parent, (2 * source_size) + _HEADROOM_SAFETY_BYTES),
        (segments_directory, source_size + (2 * catalog_size) + _HEADROOM_SAFETY_BYTES),
    ):
        device = path.stat().st_dev
        if device in requirements:
            previous_path, previous_bytes = requirements[device]
            requirements[device] = (previous_path, previous_bytes + required_bytes)
        else:
            requirements[device] = (path, required_bytes)
    for path, required_bytes in requirements.values():
        if shutil.disk_usage(path).free < required_bytes:
            raise ValueError("Segment rotation has insufficient disk headroom.")


def _history_row_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        return {
            table_name: int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            for table_name in HISTORY_TABLE_TIMESTAMPS
        }


def _require_exact_partition(
    source_counts: dict[str, int],
    segment_counts: dict[str, Any],
    staged_hot_counts: dict[str, int],
) -> None:
    for table_name in HISTORY_TABLE_TIMESTAMPS:
        segment_count = segment_counts.get(table_name)
        if (
            type(segment_count) is not int
            or source_counts[table_name] != segment_count + staged_hot_counts[table_name]
        ):
            raise ValueError("Segment rotation row accounting failed.")


def _cleanup_unjournaled_artifacts(records: list[tuple[Path, dict[str, Any], str]]) -> None:
    for root, record, label in reversed(records):
        if path_entry_exists(_record_path(root, record, label=label)):
            _remove_recorded_file(root, record, label=label)


def _rotate_segmented_history_locked(
    *,
    source: Path,
    segments_directory: Path,
    cutoff: str,
    key_id: str,
    scheduled_backup_directory: Path,
    scheduled_backup_status_path: Path,
    apply: bool,
) -> dict[str, Any]:
    source = source.absolute()
    segments_directory = _require_output_directory(segments_directory.absolute())
    source_metadata = _require_regular_source(source)
    if source.parent.stat().st_dev != segments_directory.stat().st_dev:
        raise ValueError("Segment rotation artifacts must share one directory mount.")
    journal_path = activation_pending_path(source)
    if path_entry_exists(journal_path):
        raise ValueError("Segment rotation recovery is already pending.")
    catalog_path, prior_catalog, prior_catalog_record = _validated_catalog(source, segments_directory)
    candidate_generation_id, segment_id, sequence = _next_generation(prior_catalog)
    candidate_segment_path = segments_directory / f"{segment_id}.sqlite3"
    if path_entry_exists(candidate_segment_path):
        raise ValueError("Segment rotation destination already exists.")
    backup_record = _validate_backup_evidence(
        scheduled_backup_directory.absolute(),
        scheduled_backup_status_path.absolute(),
    )
    _require_newer_backup(prior_catalog, backup_record)
    _require_headroom(source, catalog_path, segments_directory)
    current_source_metadata = os.stat(source, follow_symlinks=False)
    if (
        current_source_metadata.st_dev,
        current_source_metadata.st_ino,
        current_source_metadata.st_mode,
        current_source_metadata.st_size,
        current_source_metadata.st_mtime_ns,
    ) != (
        source_metadata.st_dev,
        source_metadata.st_ino,
        source_metadata.st_mode,
        source_metadata.st_size,
        source_metadata.st_mtime_ns,
    ):
        raise ValueError("Segment rotation source changed during preflight.")
    if not _file_matches(catalog_path, prior_catalog_record):
        raise ValueError("Segment rotation catalog changed during preflight.")
    prior_generation_id = str(prior_catalog["generation_id"])
    source_record = _record_file(source)
    source_record["mode"] = stat.S_IMODE(source_metadata.st_mode)
    if not apply:
        return {
            "apply": False,
            "generation_id": candidate_generation_id,
            "segment_id": segment_id,
            "detail": "Dry run only. Pass --apply after quiescing the history service.",
        }

    os.chmod(segments_directory, 0o700)

    rollback_hot_path = source.parent / f".{source.name}.{candidate_generation_id}.rollback.sqlite3"
    rollback_catalog_path = segments_directory / f".{prior_generation_id}.catalog.rollback.json"
    for path in (rollback_hot_path, rollback_catalog_path):
        if path_entry_exists(path):
            raise ValueError("Segment rotation rollback artifact already exists.")
    rollback_records: list[tuple[Path, dict[str, Any], str]] = []
    rollback_hot_record = _copy_file_no_replace(source, rollback_hot_path)
    rollback_records.append((source.parent, rollback_hot_record, "prior hot rollback"))
    try:
        rollback_catalog_record = _copy_file_no_replace(catalog_path, rollback_catalog_path)
        rollback_records.append(
            (segments_directory, rollback_catalog_record, "prior catalog rollback")
        )
    except BaseException:
        _cleanup_unjournaled_artifacts(rollback_records)
        raise

    journal: dict[str, Any] = {
        "journal_version": _JOURNAL_VERSION,
        "operation": "rotate",
        "transaction_id": candidate_generation_id,
        "phase": "prepared",
        "prior_generation_id": prior_generation_id,
        "candidate_generation_id": candidate_generation_id,
        "cutoff": cutoff,
        "key_id": key_id,
        "source": source_record,
        "prior_catalog": prior_catalog_record,
        "prior_hot_rollback": rollback_hot_record,
        "prior_catalog_rollback": rollback_catalog_record,
        "backup": backup_record,
    }
    marker_written = False
    try:
        _write_rotation_journal(journal_path, journal)
        marker_written = True

        def record_segment_before_publish(publication: dict[str, Any]) -> None:
            journal["new_segment"] = {
                **publication,
                "segment_id": segment_id,
                "sequence": sequence,
            }
            _write_rotation_journal(journal_path, journal)

        source_counts = _history_row_counts(source)
        segment_receipt = seal_history_segment(
            source=source,
            output_directory=segments_directory,
            segment_id=segment_id,
            cutoff=cutoff,
            key_id=key_id,
            sequence=sequence,
            before_publish=record_segment_before_publish,
        )
        journal["new_segment"] = {
            key: segment_receipt[key]
            for key in (
                "segment_id",
                "sequence",
                "coverage_start",
                "coverage_end",
                "sealed_at",
                "key_id",
                "row_counts",
                "size_bytes",
                "sha256",
            )
        }
        journal["new_segment"]["file_name"] = candidate_segment_path.name
        journal["phase"] = "segment-published"
        _write_rotation_journal(journal_path, journal)

        staged_hot_path = _stage_hot_replacement(source, cutoff)
        staged_hot_record = _record_file(staged_hot_path, final_name=source.name)
        staged_hot_counts = _history_row_counts(staged_hot_path)
        _require_exact_partition(source_counts, segment_receipt["row_counts"], staged_hot_counts)
        if _sha256_file(source) != source_record["sha256"]:
            raise ValueError("Segment rotation source changed before hot replacement.")
        journal["staged_hot"] = staged_hot_record
        _write_rotation_journal(journal_path, journal)

        candidate_catalog = json.loads(json.dumps(prior_catalog))
        prior_segments = candidate_catalog.get("segments")
        if not isinstance(prior_segments, list):
            raise ValueError("Segment rotation catalog is invalid.")
        prior_segments.append(
            {
                "segment_id": segment_id,
                "file_name": candidate_segment_path.name,
                "sequence": sequence,
                "sha256": segment_receipt["sha256"],
                "size_bytes": segment_receipt["size_bytes"],
                "coverage_start": segment_receipt["coverage_start"],
                "coverage_end": segment_receipt["coverage_end"],
                "sealed_at": segment_receipt["sealed_at"],
                "key_id": segment_receipt["key_id"],
                "row_counts": segment_receipt["row_counts"],
                "supersedes": [],
            }
        )
        candidate_catalog["parent_generation_id"] = prior_generation_id
        candidate_catalog["generation_id"] = candidate_generation_id
        candidate_catalog["complete"] = True
        candidate_catalog["tombstones"] = []
        candidate_catalog["rotation_backup"] = backup_record
        staged_catalog_path, staged_catalog_record = _stage_catalog(
            segments_directory,
            candidate_catalog,
        )
        journal["candidate_catalog"] = staged_catalog_record
        journal["phase"] = "hot-staged"
        _write_rotation_journal(journal_path, journal)

        _replace_hot(staged_hot_path, source, mode=int(source_record["mode"]))
        journal["phase"] = "hot-replaced"
        _write_rotation_journal(journal_path, journal)

        if not _file_matches(catalog_path, prior_catalog_record):
            raise ValueError("Segment rotation prior catalog changed before publication.")
        _replace_catalog(staged_catalog_path, catalog_path)
        journal["phase"] = "catalog-replaced"
        _write_rotation_journal(journal_path, journal)
        journal["phase"] = "cleanup"
        journal_record = _write_rotation_journal(journal_path, journal)

        _remove_recorded_file(
            source.parent,
            rollback_hot_record,
            label="prior hot rollback",
        )
        _remove_recorded_file(
            segments_directory,
            rollback_catalog_record,
            label="prior catalog rollback",
        )
        _remove_journal(journal_path, journal_record)
    except Exception:
        if not marker_written:
            _cleanup_unjournaled_artifacts(rollback_records)
        raise

    return {
        "apply": True,
        "generation_id": candidate_generation_id,
        "segment_id": segment_id,
        "catalog_path": str(catalog_path),
        "segment": segment_receipt,
    }


def rotate_segmented_history(
    *,
    source: Path,
    segments_directory: Path,
    cutoff: str,
    key_id: str,
    scheduled_backup_directory: Path,
    scheduled_backup_status_path: Path,
    apply: bool = False,
) -> dict[str, Any]:
    with history_write_lock(source, blocking=False):
        return _rotate_segmented_history_locked(
            source=source,
            segments_directory=segments_directory,
            cutoff=cutoff,
            key_id=key_id,
            scheduled_backup_directory=scheduled_backup_directory,
            scheduled_backup_status_path=scheduled_backup_status_path,
            apply=apply,
        )


def _recover_pending_rotation_locked(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool,
) -> dict[str, Any]:
    source = source.absolute()
    segments_directory = _require_output_directory(segments_directory.absolute())
    _require_regular_source(source)
    if apply:
        os.chmod(segments_directory, 0o700)
    journal_path = activation_pending_path(source)
    if not path_entry_exists(journal_path):
        catalog_path, catalog, catalog_record = _validated_catalog(source, segments_directory)
        candidate_generation_id, _, _ = _next_generation(catalog)
        prior_generation_id = str(catalog["generation_id"])
        rollback_hot_path = source.parent / (
            f".{source.name}.{candidate_generation_id}.rollback.sqlite3"
        )
        rollback_catalog_path = segments_directory / (
            f".{prior_generation_id}.catalog.rollback.json"
        )
        orphan_records: list[tuple[Path, dict[str, Any], str]] = []
        if path_entry_exists(rollback_hot_path):
            rollback_hot_record = _record_file(rollback_hot_path)
            source_record = _record_file(source)
            if (
                rollback_hot_record["sha256"] != source_record["sha256"]
                or rollback_hot_record["size_bytes"] != source_record["size_bytes"]
            ):
                raise ValueError("Segment rotation orphan hot rollback integrity check failed.")
            orphan_records.append(
                (source.parent, rollback_hot_record, "orphan hot rollback")
            )
        if path_entry_exists(rollback_catalog_path):
            rollback_catalog_record = _record_file(rollback_catalog_path)
            if (
                rollback_catalog_record["sha256"] != catalog_record["sha256"]
                or rollback_catalog_record["size_bytes"] != catalog_record["size_bytes"]
            ):
                raise ValueError("Segment rotation orphan catalog rollback integrity check failed.")
            orphan_records.append(
                (
                    segments_directory,
                    rollback_catalog_record,
                    "orphan catalog rollback",
                )
            )
        if not orphan_records:
            raise ValueError("Segment rotation recovery is not pending.")
        if not apply:
            return {
                "apply": False,
                "recovery_state": "orphan-rollbacks-ready-to-remove",
                "orphan_paths": [
                    str(_record_path(root, record, label=label))
                    for root, record, label in orphan_records
                ],
            }
        _cleanup_unjournaled_artifacts(orphan_records)
        return {
            "apply": True,
            "recovery_state": "orphan-rollbacks-removed",
            "orphan_paths": [],
        }
    journal, journal_record = _read_rotation_journal(journal_path)
    phase = str(journal["phase"])
    source_record = journal.get("source")
    prior_catalog_record = journal.get("prior_catalog")
    rollback_hot_record = journal.get("prior_hot_rollback")
    rollback_catalog_record = journal.get("prior_catalog_rollback")
    staged_hot_record = journal.get("staged_hot")
    candidate_catalog_record = journal.get("candidate_catalog")
    new_segment_record = journal.get("new_segment")
    if (
        not isinstance(source_record, dict)
        or type(source_record.get("mode")) is not int
        or not isinstance(rollback_hot_record, dict)
        or not isinstance(rollback_catalog_record, dict)
    ):
        raise ValueError("Segment rotation journal is invalid.")
    expected_staged_hot = (
        _record_path(source.parent, staged_hot_record, label="staged hot")
        if isinstance(staged_hot_record, dict)
        else None
    )
    if any(
        path != expected_staged_hot
        for path in source.parent.glob(f".{source.name}.segmented-*.sqlite3")
    ):
        raise ValueError("Segment rotation found an unauthenticated staging artifact.")
    expected_staged_catalog = (
        _record_path(
            segments_directory,
            candidate_catalog_record,
            label="candidate catalog",
        )
        if isinstance(candidate_catalog_record, dict)
        else None
    )
    if any(
        path != expected_staged_catalog
        for path in segments_directory.glob(".rotation-catalog-*.json")
    ):
        raise ValueError("Segment rotation found an unauthenticated staging artifact.")
    rollback_hot_path = _record_path(
        source.parent,
        rollback_hot_record,
        label="prior hot rollback",
    )
    rollback_catalog_path = _record_path(
        segments_directory,
        rollback_catalog_record,
        label="prior catalog rollback",
    )
    catalog_path = segments_directory / "catalog.json"
    active_catalog_is_prior = _file_matches(catalog_path, prior_catalog_record)
    active_catalog_is_candidate = (
        isinstance(candidate_catalog_record, dict)
        and _file_matches(catalog_path, candidate_catalog_record)
    )
    if active_catalog_is_prior == active_catalog_is_candidate:
        raise ValueError("Segment rotation catalog integrity check failed.")

    if active_catalog_is_candidate and phase == "cleanup":
        if path_entry_exists(rollback_hot_path):
            _require_file_matches(
                rollback_hot_path,
                rollback_hot_record,
                label="prior hot rollback",
            )
        if path_entry_exists(rollback_catalog_path):
            _require_file_matches(
                rollback_catalog_path,
                rollback_catalog_record,
                label="prior catalog rollback",
            )
    else:
        _require_file_matches(
            rollback_hot_path,
            rollback_hot_record,
            label="prior hot rollback",
        )
        _require_file_matches(
            rollback_catalog_path,
            rollback_catalog_record,
            label="prior catalog rollback",
        )

    live_hot_is_prior = _file_matches(source, source_record)
    live_hot_is_candidate = (
        isinstance(staged_hot_record, dict)
        and _file_matches(source, staged_hot_record)
    )
    if active_catalog_is_candidate:
        if not live_hot_is_candidate:
            raise ValueError("Segment rotation live hot database is divergent.")
        if not isinstance(new_segment_record, dict):
            raise ValueError("Segment rotation new segment record is invalid.")
        new_segment_path = _record_path(
            segments_directory,
            new_segment_record,
            label="new segment",
        )
        _require_file_matches(new_segment_path, new_segment_record, label="new segment")
        try:
            SegmentedHistoryReader.from_catalog(
                hot_path=source,
                catalog_path=catalog_path,
                allow_pending_activation=True,
            ).verify_catalog_segments()
        except ValueError as exc:
            raise ValueError("Segment rotation candidate generation integrity check failed.") from exc
        if not apply:
            return {
                "apply": False,
                "recovery_state": "candidate-ready-to-finalize",
                "phase": phase,
            }
        _remove_recorded_file(
            source.parent,
            rollback_hot_record,
            label="prior hot rollback",
            allow_missing=phase == "cleanup",
        )
        _remove_recorded_file(
            segments_directory,
            rollback_catalog_record,
            label="prior catalog rollback",
            allow_missing=phase == "cleanup",
        )
        if isinstance(staged_hot_record, dict):
            _remove_recorded_file(
                source.parent,
                staged_hot_record,
                label="staged hot",
                allow_missing=True,
            )
        if isinstance(candidate_catalog_record, dict):
            _remove_recorded_file(
                segments_directory,
                candidate_catalog_record,
                label="candidate catalog",
                allow_missing=True,
            )
        _remove_journal(journal_path, journal_record)
        return {
            "apply": True,
            "recovery_state": "candidate-finalized",
            "phase": phase,
        }

    if not live_hot_is_prior and not live_hot_is_candidate:
        raise ValueError("Segment rotation live hot database is divergent.")
    if not apply:
        return {
            "apply": False,
            "recovery_state": "prior-generation-ready-to-restore",
            "phase": phase,
        }
    if live_hot_is_candidate:
        _restore_hot(source, rollback_hot_record, mode=int(source_record["mode"]))
    if isinstance(new_segment_record, dict):
        _remove_authenticated_orphan_segment(segments_directory, new_segment_record)
    else:
        candidate_segment_id = journal.get("candidate_generation_id")
        if isinstance(candidate_segment_id, str):
            sequence_match = _GENERATION_ID.fullmatch(candidate_segment_id)
            if sequence_match is not None:
                unexpected_segment = segments_directory / (
                    f"segment-{int(sequence_match.group('sequence')):04d}.sqlite3"
                )
                if path_entry_exists(unexpected_segment):
                    raise ValueError("Segment rotation found an unauthenticated segment.")
    if isinstance(staged_hot_record, dict):
        _remove_recorded_file(
            source.parent,
            staged_hot_record,
            label="staged hot",
            allow_missing=True,
        )
    if isinstance(candidate_catalog_record, dict):
        _remove_recorded_file(
            segments_directory,
            candidate_catalog_record,
            label="candidate catalog",
            allow_missing=True,
        )
    _remove_recorded_file(
        source.parent,
        rollback_hot_record,
        label="prior hot rollback",
    )
    _remove_recorded_file(
        segments_directory,
        rollback_catalog_record,
        label="prior catalog rollback",
    )
    _remove_journal(journal_path, journal_record)
    return {
        "apply": True,
        "recovery_state": "prior-generation-restored",
        "phase": phase,
    }


def recover_pending_rotation(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool = False,
) -> dict[str, Any]:
    with history_write_lock(source, blocking=False):
        return _recover_pending_rotation_locked(
            source=source,
            segments_directory=segments_directory,
            apply=apply,
        )
