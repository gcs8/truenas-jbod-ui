from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    SEGMENT_ID_PATTERN,
    path_entry_exists,
)
from history_service.segment_reader import SegmentedHistoryReader
from history_service.segment_sealer import (
    HISTORY_TABLE_TIMESTAMPS,
    _prepare_output_directory,
    _require_output_directory,
    _require_regular_source,
    seal_history_segment,
)
from history_service.migration_lock import history_write_lock


class SegmentedHistoryMigrationError(RuntimeError):
    pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _create_rollback_snapshot(
    source: Path,
    segments_directory: Path,
    source_metadata: os.stat_result,
) -> tuple[Path, str]:
    rollback_name = ".v1-rollback.sqlite3"
    rollback_path = segments_directory / rollback_name
    directory_descriptor = os.open(
        segments_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    source_descriptor = -1
    rollback_descriptor = -1
    created = False
    try:
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            raise ValueError("Segmented history output directory is invalid.")
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened_source_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened_source_metadata.st_mode)
            or (opened_source_metadata.st_dev, opened_source_metadata.st_ino)
            != (source_metadata.st_dev, source_metadata.st_ino)
        ):
            raise ValueError("Segmented history source changed during migration preflight.")
        try:
            rollback_descriptor = os.open(
                rollback_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ELOOP}:
                raise ValueError("Segmented history rollback snapshot already exists.") from exc
            raise
        created = True
        os.fchmod(rollback_descriptor, 0o600)
        digest = hashlib.sha256()
        with os.fdopen(source_descriptor, "rb", buffering=0, closefd=True) as source_stream, os.fdopen(
            rollback_descriptor,
            "wb",
            buffering=0,
            closefd=True,
        ) as rollback_stream:
            source_descriptor = -1
            rollback_descriptor = -1
            while chunk := source_stream.read(1024 * 1024):
                digest.update(chunk)
                rollback_stream.write(chunk)
            os.fsync(rollback_stream.fileno())
        os.fsync(directory_descriptor)
        return rollback_path, digest.hexdigest()
    except BaseException:
        if created:
            try:
                os.unlink(rollback_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except FileNotFoundError:
                pass
        raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if rollback_descriptor >= 0:
            os.close(rollback_descriptor)
        os.close(directory_descriptor)


def _stage_hot_replacement(source: Path, cutoff: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.segmented-",
        suffix=".sqlite3",
        dir=source.parent,
    )
    temporary_path = Path(temporary_name)
    source_mode = stat.S_IMODE(os.stat(source, follow_symlinks=False).st_mode)
    os.fchmod(descriptor, source_mode)
    os.close(descriptor)
    try:
        with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with sqlite3.connect(temporary_path) as replacement_connection:
                source_connection.backup(replacement_connection)
                replacement_connection.execute("PRAGMA journal_mode = DELETE")
                with replacement_connection:
                    for table_name, timestamp_column in HISTORY_TABLE_TIMESTAMPS.items():
                        replacement_connection.execute(
                            f"DELETE FROM {table_name} WHERE julianday({timestamp_column}) < julianday(?)",
                            (cutoff,),
                        )
                replacement_connection.execute("VACUUM")
                if replacement_connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise SegmentedHistoryMigrationError("Staged hot history integrity check failed.")
        with temporary_path.open("rb", buffering=0) as stream:
            os.fsync(stream.fileno())
        return temporary_path
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_private_json_no_replace(
    path: Path,
    payload: dict[str, Any],
    *,
    temporary_prefix: str,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=temporary_prefix, suffix=".json", dir=path.parent)
    temporary_path = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    try:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        os.link(temporary_path, path)
        temporary_path.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_private_json_replace(
    path: Path,
    payload: dict[str, Any],
    *,
    temporary_prefix: str,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=temporary_prefix, suffix=".json", dir=path.parent)
    temporary_path = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    try:
        content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _write_catalog(path: Path, payload: dict[str, Any]) -> None:
    _write_private_json_no_replace(path, payload, temporary_prefix=".catalog-")


def _write_pending_marker(
    path: Path,
    *,
    rollback_sha256: str,
    operation: str = "forward",
    source_sha256: str | None = None,
    phase: str = "prepared",
    replacement_sha256: str | None = None,
    segment_publication: dict[str, Any] | None = None,
) -> None:
    if operation not in {"forward", "rollback"}:
        raise ValueError("Segmented history migration operation is invalid.")
    if operation == "rollback" and source_sha256 is None:
        raise ValueError("Segmented history rollback source digest is required.")
    if phase not in {"prepared", "segment-ready", "hot-ready"}:
        raise ValueError("Segmented history migration phase is invalid.")
    if operation != "forward" and phase != "prepared":
        raise ValueError("Segmented history migration phase is invalid.")
    if phase == "hot-ready" and replacement_sha256 is None:
        raise ValueError("Segmented history replacement digest is required.")
    if source_sha256 is None:
        source_sha256 = rollback_sha256
    payload: dict[str, Any] = {
        "marker_version": 1,
        "operation": operation,
        "phase": phase,
        "rollback_sha256": rollback_sha256,
        "source_sha256": source_sha256,
        "status": "migration-pending",
    }
    if replacement_sha256 is not None:
        payload["replacement_sha256"] = replacement_sha256
    if segment_publication is not None:
        payload["segment_publication"] = segment_publication
    _write_private_json_no_replace(
        path,
        payload,
        temporary_prefix=".migration-pending-",
    )


def _replace_pending_marker(
    path: Path,
    *,
    rollback_sha256: str,
    source_sha256: str,
    phase: str,
    segment_publication: dict[str, Any],
    replacement_sha256: str | None = None,
) -> None:
    if phase not in {"segment-ready", "hot-ready"}:
        raise ValueError("Segmented history migration phase is invalid.")
    if phase == "hot-ready" and replacement_sha256 is None:
        raise ValueError("Segmented history replacement digest is required.")
    payload: dict[str, Any] = {
        "marker_version": 1,
        "operation": "forward",
        "phase": phase,
        "rollback_sha256": rollback_sha256,
        "segment_publication": segment_publication,
        "source_sha256": source_sha256,
        "status": "migration-pending",
    }
    if replacement_sha256 is not None:
        payload["replacement_sha256"] = replacement_sha256
    _write_private_json_replace(
        path,
        payload,
        temporary_prefix=".migration-pending-",
    )


def _clear_pending_marker(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _restore_v1_source(source: Path, rollback_path: Path, source_mode: int, expected_sha256: str) -> None:
    descriptor, restore_name = tempfile.mkstemp(
        prefix=f".{source.name}.rollback-",
        suffix=".sqlite3",
        dir=source.parent,
    )
    restore_path = Path(restore_name)
    os.fchmod(descriptor, source_mode)
    try:
        destination = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        snapshot_descriptor = os.open(
            rollback_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(snapshot_descriptor).st_mode):
                raise ValueError("Segmented history rollback snapshot integrity check failed.")
            snapshot = os.fdopen(snapshot_descriptor, "rb", buffering=0, closefd=True)
            snapshot_descriptor = -1
            with destination, snapshot:
                digest = hashlib.sha256()
                while chunk := snapshot.read(1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != expected_sha256:
                    raise ValueError("Segmented history rollback snapshot integrity check failed.")
                snapshot.seek(0)
                shutil.copyfileobj(snapshot, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            if snapshot_descriptor >= 0:
                os.close(snapshot_descriptor)
        os.replace(restore_path, source)
        os.chmod(source, source_mode)
        _fsync_directory(source.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        restore_path.unlink(missing_ok=True)


def _remove_authenticated_orphan_segment(
    segments_directory: Path,
    publication: Any,
) -> Path | None:
    if not isinstance(publication, dict):
        raise ValueError("Segmented history orphan publication record is invalid.")
    file_name = publication.get("file_name")
    size_bytes = publication.get("size_bytes")
    sha256 = publication.get("sha256")
    if not isinstance(file_name, str) or not file_name.endswith(".sqlite3"):
        raise ValueError("Segmented history orphan publication record is invalid.")
    segment_id = file_name.removesuffix(".sqlite3")
    if (
        not SEGMENT_ID_PATTERN.fullmatch(segment_id)
        or file_name != f"{segment_id}.sqlite3"
        or type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ValueError("Segmented history orphan publication record is invalid.")

    directory_descriptor = os.open(
        segments_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                file_name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return None
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink not in {1, 2}
            or metadata.st_size != size_bytes
        ):
            raise ValueError("Segmented history orphan segment integrity check failed.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError("Segmented history orphan segment integrity check failed.")
        path_metadata = os.stat(file_name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != metadata.st_nlink
            or (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError("Segmented history orphan segment changed during recovery.")
        temporary_links: list[str] = []
        for candidate_name in os.listdir(directory_descriptor):
            if not candidate_name.startswith(".segment-") or not candidate_name.endswith(".sqlite3"):
                continue
            candidate_metadata = os.stat(
                candidate_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (candidate_metadata.st_dev, candidate_metadata.st_ino) == (metadata.st_dev, metadata.st_ino):
                temporary_links.append(candidate_name)
        if len(temporary_links) != metadata.st_nlink - 1:
            raise ValueError("Segmented history orphan segment link set is invalid.")
        for temporary_link in temporary_links:
            os.unlink(temporary_link, dir_fd=directory_descriptor)
        os.unlink(file_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        return segments_directory / file_name
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_descriptor)


def _load_rollback_catalog(
    catalog_path: Path,
    source: Path,
    *,
    allow_pending_recovery: bool = False,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    reader = SegmentedHistoryReader.from_catalog(
        hot_path=source,
        catalog_path=catalog_path,
        allow_pending_recovery=allow_pending_recovery,
    )
    verified_segment_paths = reader.verify_catalog_segments()
    catalog = reader.catalog_payload()
    if not isinstance(catalog, dict) or not isinstance(catalog.get("segments"), list):
        raise ValueError("Segmented history catalog is invalid.")
    if len(catalog["segments"]) != len(verified_segment_paths):
        raise ValueError("Segmented history catalog is invalid.")
    for entry, segment_path in zip(catalog["segments"], verified_segment_paths, strict=True):
        if not isinstance(entry, dict):
            raise ValueError("Segmented history catalog is invalid.")
        segment_id = entry.get("segment_id")
        if (
            not isinstance(segment_id, str)
            or not SEGMENT_ID_PATTERN.fullmatch(segment_id)
            or entry.get("file_name") != f"{segment_id}.sqlite3"
            or segment_path.name != entry["file_name"]
        ):
            raise ValueError("Segmented history catalog is invalid.")
    return catalog, verified_segment_paths


def _migrate_segmented_history_locked(
    *,
    source: Path,
    segments_directory: Path,
    cutoff: str,
    key_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    source = source.absolute()
    segments_directory = segments_directory.absolute()
    catalog_path = segments_directory / "catalog.json"
    if path_entry_exists(catalog_path):
        raise ValueError("Segmented history catalog already exists.")
    if not apply:
        return {
            "apply": False,
            "catalog_path": str(catalog_path),
            "detail": "Dry run only. Pass --apply after quiescing the history service.",
        }

    source_metadata = _require_regular_source(source)
    segments_directory = _prepare_output_directory(segments_directory)
    rollback_path = segments_directory / ".v1-rollback.sqlite3"
    pending_path = segments_directory / MIGRATION_PENDING_MARKER
    if path_entry_exists(rollback_path):
        raise ValueError("Segmented history rollback snapshot already exists.")
    if path_entry_exists(pending_path):
        raise ValueError("Segmented history migration recovery is already pending.")
    rollback_path, rollback_sha256 = _create_rollback_snapshot(
        source,
        segments_directory,
        source_metadata,
    )
    _write_pending_marker(pending_path, rollback_sha256=rollback_sha256)

    staged_hot_path: Path | None = None
    segment_path: Path | None = None
    segment_publication: dict[str, Any] | None = None
    pending_marker_active = True
    source_mode = stat.S_IMODE(os.stat(source, follow_symlinks=False).st_mode)

    def record_segment_publication(publication: dict[str, Any]) -> None:
        nonlocal segment_publication
        segment_publication = dict(publication)
        _replace_pending_marker(
            pending_path,
            rollback_sha256=rollback_sha256,
            source_sha256=rollback_sha256,
            phase="segment-ready",
            segment_publication=segment_publication,
        )

    try:
        segment_receipt = seal_history_segment(
            source=source,
            output_directory=segments_directory,
            segment_id="segment-0001",
            cutoff=cutoff,
            key_id=key_id,
            before_publish=record_segment_publication,
        )
        segment_path = Path(str(segment_receipt["path"]))
        if segment_publication is None:
            raise SegmentedHistoryMigrationError("History segment publication record is missing.")
        staged_hot_path = _stage_hot_replacement(source, cutoff)
        _replace_pending_marker(
            pending_path,
            rollback_sha256=rollback_sha256,
            source_sha256=rollback_sha256,
            phase="hot-ready",
            segment_publication=segment_publication,
            replacement_sha256=_sha256_file(staged_hot_path),
        )
        os.replace(staged_hot_path, source)
        staged_hot_path = None
        os.chmod(source, source_mode)
        _fsync_directory(source.parent)
        catalog = {
            "catalog_version": 1,
            "generation_id": "generation-0001",
            "complete": True,
            "segments": [
                {
                    "segment_id": segment_receipt["segment_id"],
                    "file_name": segment_path.name,
                    "sha256": segment_receipt["sha256"],
                    "size_bytes": segment_receipt["size_bytes"],
                    "coverage_start": segment_receipt["coverage_start"],
                    "coverage_end": segment_receipt["coverage_end"],
                    "sealed_at": segment_receipt["sealed_at"],
                    "key_id": segment_receipt["key_id"],
                    "row_counts": segment_receipt["row_counts"],
                }
            ],
            "rollback_sha256": rollback_sha256,
        }
        _write_catalog(catalog_path, catalog)
        _clear_pending_marker(pending_path)
        pending_marker_active = False
    except BaseException:
        if staged_hot_path is not None:
            staged_hot_path.unlink(missing_ok=True)
        if path_entry_exists(rollback_path) and not path_entry_exists(catalog_path):
            _restore_v1_source(source, rollback_path, source_mode, rollback_sha256)
        if segment_publication is not None and not path_entry_exists(catalog_path):
            _remove_authenticated_orphan_segment(segments_directory, segment_publication)
        if pending_marker_active and not path_entry_exists(catalog_path):
            _clear_pending_marker(pending_path)
        raise

    return {
        "apply": True,
        "catalog_path": str(catalog_path),
        "rollback_path": str(rollback_path),
        "segment": segment_receipt,
    }


def migrate_segmented_history(
    *,
    source: Path,
    segments_directory: Path,
    cutoff: str,
    key_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    with history_write_lock(source, blocking=False):
        return _migrate_segmented_history_locked(
            source=source,
            segments_directory=segments_directory,
            cutoff=cutoff,
            key_id=key_id,
            apply=apply,
        )


def _rollback_segmented_history_locked(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool = False,
) -> dict[str, Any]:
    source = source.absolute()
    segments_directory = segments_directory.absolute()
    segments_directory = _require_output_directory(segments_directory)
    catalog_path = segments_directory / "catalog.json"
    rollback_path = segments_directory / ".v1-rollback.sqlite3"
    pending_path = segments_directory / MIGRATION_PENDING_MARKER
    _require_regular_source(source)
    if path_entry_exists(pending_path):
        raise ValueError("Segmented history migration recovery is already pending.")
    catalog, segment_paths = _load_rollback_catalog(catalog_path, source)
    rollback_sha256 = catalog.get("rollback_sha256")
    if not isinstance(rollback_sha256, str) or _sha256_file(rollback_path) != rollback_sha256:
        raise ValueError("Segmented history rollback snapshot integrity check failed.")
    if not apply:
        return {
            "apply": False,
            "catalog_path": str(catalog_path),
            "detail": "Dry run only. Pass --apply after quiescing the history service.",
        }

    source_mode = stat.S_IMODE(os.stat(source, follow_symlinks=False).st_mode)
    _write_pending_marker(
        pending_path,
        rollback_sha256=rollback_sha256,
        operation="rollback",
        source_sha256=_sha256_file(source),
    )
    _restore_v1_source(source, rollback_path, source_mode, rollback_sha256)
    catalog_path.unlink()
    for segment_path in segment_paths:
        segment_path.unlink()
    _fsync_directory(segments_directory)
    _clear_pending_marker(pending_path)
    return {
        "apply": True,
        "catalog_path": str(catalog_path),
        "rollback_path": str(rollback_path),
        "removed_segment_count": len(segment_paths),
    }


def rollback_segmented_history(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool = False,
) -> dict[str, Any]:
    with history_write_lock(source, blocking=False):
        return _rollback_segmented_history_locked(
            source=source,
            segments_directory=segments_directory,
            apply=apply,
        )


def _recover_pending_migration_locked(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool = False,
) -> dict[str, Any]:
    source = source.absolute()
    segments_directory = segments_directory.absolute()
    segments_directory = _require_output_directory(segments_directory)
    catalog_path = segments_directory / "catalog.json"
    rollback_path = segments_directory / ".v1-rollback.sqlite3"
    pending_path = segments_directory / MIGRATION_PENDING_MARKER
    _require_regular_source(source)
    if not path_entry_exists(pending_path):
        if path_entry_exists(catalog_path) or not path_entry_exists(rollback_path):
            raise ValueError("Segmented history migration recovery is not pending.")
        rollback_path = SegmentedHistoryReader._require_regular_file(
            rollback_path,
            label="rollback snapshot",
        )
        if _sha256_file(source) != _sha256_file(rollback_path):
            raise ValueError("Segmented history orphan rollback snapshot does not match the live source.")
        if not apply:
            return {
                "apply": False,
                "recovery_state": "orphan-snapshot-matches-source",
                "rollback_path": str(rollback_path),
                "detail": "Dry run only. Pass --apply to remove the redundant rollback snapshot.",
            }
        rollback_path.unlink()
        _fsync_directory(segments_directory)
        return {
            "apply": True,
            "recovery_state": "orphan-snapshot-removed",
            "rollback_path": str(rollback_path),
            "orphaned_segment_paths": [],
        }
    pending_path = SegmentedHistoryReader._require_regular_file(pending_path, label="migration recovery marker")
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Segmented history migration recovery marker is invalid.") from exc
    if not isinstance(pending, dict):
        raise ValueError("Segmented history migration recovery marker is invalid.")
    operation = pending.get("operation")
    phase = pending.get("phase")
    rollback_sha256 = pending.get("rollback_sha256")
    source_sha256 = pending.get("source_sha256")
    replacement_sha256 = pending.get("replacement_sha256")
    if (
        pending.get("marker_version") != 1
        or pending.get("status") != "migration-pending"
        or operation not in {"forward", "rollback"}
        or phase not in {"prepared", "segment-ready", "hot-ready"}
        or not isinstance(rollback_sha256, str)
        or not isinstance(source_sha256, str)
        or (operation == "rollback" and phase != "prepared")
        or (phase == "hot-ready" and not isinstance(replacement_sha256, str))
        or _sha256_file(rollback_path) != rollback_sha256
    ):
        raise ValueError("Segmented history migration recovery integrity check failed.")
    if path_entry_exists(catalog_path):
        catalog, segment_paths = _load_rollback_catalog(
            catalog_path,
            source,
            allow_pending_recovery=True,
        )
        if catalog.get("rollback_sha256") != rollback_sha256:
            raise ValueError("Segmented history migration recovery integrity check failed.")
        if operation == "rollback":
            current_source_sha256 = _sha256_file(source)
            if current_source_sha256 == source_sha256:
                if not apply:
                    return {
                        "apply": False,
                        "recovery_state": "rollback-not-started",
                        "catalog_path": str(catalog_path),
                        "detail": "Dry run only. Pass --apply to cancel the unstarted rollback.",
                    }
                _clear_pending_marker(pending_path)
                return {
                    "apply": True,
                    "recovery_state": "rollback-cancelled",
                    "catalog_path": str(catalog_path),
                    "orphaned_segment_paths": [],
                }
            if current_source_sha256 != rollback_sha256:
                raise ValueError("Segmented history migration recovery integrity check failed.")
            if not apply:
                return {
                    "apply": False,
                    "recovery_state": "rollback-ready-to-finalize",
                    "catalog_path": str(catalog_path),
                    "detail": "Dry run only. Pass --apply to finalize the restored v1 source.",
                }
            catalog_path.unlink()
            for segment_path in segment_paths:
                segment_path.unlink()
            _fsync_directory(segments_directory)
            _clear_pending_marker(pending_path)
            return {
                "apply": True,
                "recovery_state": "rollback-finalized",
                "rollback_path": str(rollback_path),
                "orphaned_segment_paths": [],
            }
        if phase != "hot-ready" or _sha256_file(source) != replacement_sha256:
            raise ValueError("Segmented history migration recovery integrity check failed.")
        if not apply:
            return {
                "apply": False,
                "recovery_state": "published-catalog-ready-to-finalize",
                "catalog_path": str(catalog_path),
                "detail": "Dry run only. Pass --apply to finalize the published catalog.",
            }
        _clear_pending_marker(pending_path)
        return {
            "apply": True,
            "recovery_state": "published-catalog-finalized",
            "catalog_path": str(catalog_path),
            "orphaned_segment_paths": [],
        }
    candidate_segment_paths = sorted(segments_directory.glob("segment-*.sqlite3"))
    if any(
        not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
        for path in candidate_segment_paths
    ):
        raise ValueError("Segmented history recovery found an unauthenticated orphan segment.")
    publication = pending.get("segment_publication")
    expected_segment_path: Path | None = None
    if publication is not None:
        if not isinstance(publication, dict) or not isinstance(publication.get("file_name"), str):
            raise ValueError("Segmented history orphan publication record is invalid.")
        expected_segment_path = segments_directory / publication["file_name"]
    if any(path != expected_segment_path for path in candidate_segment_paths):
        raise ValueError("Segmented history recovery found an unauthenticated orphan segment.")
    orphaned_segment_paths = [str(path) for path in candidate_segment_paths]
    current_source_sha256 = _sha256_file(source)
    if operation == "forward":
        allowed_source_digests = {source_sha256}
        if phase == "hot-ready" and isinstance(replacement_sha256, str):
            allowed_source_digests.add(replacement_sha256)
        if current_source_sha256 not in allowed_source_digests:
            raise ValueError("Segmented history recovery refuses a divergent live source.")
    elif current_source_sha256 != rollback_sha256:
        raise ValueError("Segmented history recovery refuses a divergent live source.")
    if not apply:
        return {
            "apply": False,
            "rollback_path": str(rollback_path),
            "orphaned_segment_paths": orphaned_segment_paths,
            "detail": "Dry run only. Pass --apply after quiescing the history service.",
        }

    if operation == "forward":
        source_mode = stat.S_IMODE(os.stat(source, follow_symlinks=False).st_mode)
        _restore_v1_source(source, rollback_path, source_mode, rollback_sha256)
    removed_orphaned_segment_paths: list[str] = []
    if publication is not None:
        removed_path = _remove_authenticated_orphan_segment(segments_directory, publication)
        if removed_path is not None:
            removed_orphaned_segment_paths.append(str(removed_path))
    _clear_pending_marker(pending_path)
    return {
        "apply": True,
        "recovery_state": "forward-rolled-back" if operation == "forward" else "rollback-finalized",
        "rollback_path": str(rollback_path),
        "orphaned_segment_paths": orphaned_segment_paths,
        "removed_orphaned_segment_paths": removed_orphaned_segment_paths,
    }


def recover_pending_migration(
    *,
    source: Path,
    segments_directory: Path,
    apply: bool = False,
) -> dict[str, Any]:
    with history_write_lock(source, blocking=False):
        return _recover_pending_migration_locked(
            source=source,
            segments_directory=segments_directory,
            apply=apply,
        )
