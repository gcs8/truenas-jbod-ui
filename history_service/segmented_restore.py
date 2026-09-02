from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from history_service.segment_catalog import path_entry_exists
from history_service.segment_reader import SegmentedHistoryReader

ACTIVATION_JOURNAL_VERSION = 1
MAX_ACTIVATION_JOURNAL_BYTES = 1024 * 1024
RESTORE_PHASES = frozenset(
    {
        "prepared",
        "hot-parked",
        "hot-replaced",
        "segments-parked",
        "segments-replaced",
        "cleanup",
    }
)
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("History activation journal contains a duplicate JSON key.")
        result[key] = value
    return result


def record_file(path: Path) -> dict[str, Any]:
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
            raise ValueError("Segmented restore artifact must be a single-link regular file.")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != identity or (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_mode,
            path_metadata.st_nlink,
            path_metadata.st_uid,
            path_metadata.st_gid,
            path_metadata.st_size,
            path_metadata.st_mtime_ns,
            path_metadata.st_ctime_ns,
        ) != identity:
            raise ValueError("Segmented restore artifact changed while it was being recorded.")
    finally:
        os.close(descriptor)
    return {
        "kind": "file",
        "sha256": digest.hexdigest(),
        "size_bytes": before.st_size,
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
        "gid": before.st_gid,
    }


def record_optional_file(path: Path) -> dict[str, Any]:
    if not path_entry_exists(path):
        return {"kind": "missing"}
    return record_file(path)


def _relative_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Segmented restore {label} is invalid.")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Segmented restore {label} is invalid.")
    return path


def record_tree(root: Path) -> dict[str, Any]:
    root_metadata = os.stat(root, follow_symlinks=False)
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Segmented restore tree must be a directory.")
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Segmented restore tree must not contain symlinks.")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(
                {
                    "path": relative,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "uid": metadata.st_uid,
                    "gid": metadata.st_gid,
                }
            )
        elif stat.S_ISREG(metadata.st_mode):
            record = record_file(path)
            record["path"] = relative
            files.append(record)
        else:
            raise ValueError("Segmented restore tree contains an unsupported entry.")
    after = os.stat(root, follow_symlinks=False)
    if (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != (
        root_metadata.st_dev,
        root_metadata.st_ino,
        root_metadata.st_mode,
        root_metadata.st_uid,
        root_metadata.st_gid,
        root_metadata.st_mtime_ns,
        root_metadata.st_ctime_ns,
    ):
        raise ValueError("Segmented restore tree changed while it was being recorded.")
    return {
        "kind": "directory",
        "device": root_metadata.st_dev,
        "inode": root_metadata.st_ino,
        "mode": stat.S_IMODE(root_metadata.st_mode),
        "uid": root_metadata.st_uid,
        "gid": root_metadata.st_gid,
        "directories": directories,
        "files": files,
    }


def record_optional_tree(path: Path) -> dict[str, Any]:
    if not path_entry_exists(path):
        return {"kind": "missing"}
    return record_tree(path)


def _valid_owner_mode_record(record: Any, *, kind: str) -> bool:
    return (
        isinstance(record, dict)
        and record.get("kind") == kind
        and type(record.get("device")) is int
        and record["device"] >= 0
        and type(record.get("inode")) is int
        and record["inode"] > 0
        and type(record.get("mode")) is int
        and 0 <= record["mode"] <= 0o7777
        and type(record.get("uid")) is int
        and record["uid"] >= 0
        and type(record.get("gid")) is int
        and record["gid"] >= 0
    )


def _valid_file_record(record: Any) -> bool:
    return (
        _valid_owner_mode_record(record, kind="file")
        and type(record.get("size_bytes")) is int
        and record["size_bytes"] >= 0
        and isinstance(record.get("sha256"), str)
        and _SHA256.fullmatch(record["sha256"]) is not None
    )


def file_matches(path: Path, record: Any) -> bool:
    if not _valid_file_record(record):
        return False
    try:
        return record_file(path) == record
    except (OSError, ValueError):
        return False


def _valid_tree_record(record: Any) -> bool:
    if not _valid_owner_mode_record(record, kind="directory"):
        return False
    directories = record.get("directories")
    files = record.get("files")
    if not isinstance(directories, list) or not isinstance(files, list):
        return False
    seen: set[str] = set()
    for item in directories:
        if not (
            isinstance(item, dict)
            and type(item.get("device")) is int
            and item["device"] >= 0
            and type(item.get("inode")) is int
            and item["inode"] > 0
            and type(item.get("mode")) is int
            and 0 <= item["mode"] <= 0o7777
            and type(item.get("uid")) is int
            and item["uid"] >= 0
            and type(item.get("gid")) is int
            and item["gid"] >= 0
        ):
            return False
        try:
            relative = _relative_path(item.get("path"), label="directory path").as_posix()
        except ValueError:
            return False
        if relative in seen:
            return False
        seen.add(relative)
    for item in files:
        if not _valid_file_record(item):
            return False
        try:
            relative = _relative_path(item.get("path"), label="file path").as_posix()
        except ValueError:
            return False
        if relative in seen:
            return False
        seen.add(relative)
    return True


def tree_matches(path: Path, record: Any) -> bool:
    if not _valid_tree_record(record):
        return False
    try:
        return record_tree(path) == record
    except (OSError, ValueError):
        return False


def read_activation_journal(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MAX_ACTIVATION_JOURNAL_BYTES
        ):
            raise ValueError("History activation journal is invalid.")
        content = os.read(descriptor, MAX_ACTIVATION_JOURNAL_BYTES + 1)
        if len(content) > MAX_ACTIVATION_JOURNAL_BYTES or os.read(descriptor, 1):
            raise ValueError("History activation journal is invalid.")
        after = os.fstat(descriptor)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) or (path_metadata.st_dev, path_metadata.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("History activation journal changed while it was being read.")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("History activation journal is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("journal_version") != ACTIVATION_JOURNAL_VERSION
        or payload.get("operation") not in {"rotate", "segmented-restore"}
    ):
        raise ValueError("History activation journal is invalid.")
    return payload, {
        "kind": "file",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": stat.S_IMODE(before.st_mode),
        "uid": before.st_uid,
        "gid": before.st_gid,
    }


def write_restore_journal(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    created_metadata = os.fstat(descriptor)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Segmented restore journal write was incomplete.")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        descriptor = -1
        try:
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (
                created_metadata.st_dev,
                created_metadata.st_ino,
            ):
                path.unlink()
                _fsync_directory(path.parent)
        finally:
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)
    return record_file(path)


def _artifact_name(value: Any, *, expected: str, label: str) -> str:
    if not isinstance(value, str) or Path(value).name != value or value != expected:
        raise ValueError(f"Segmented restore {label} is invalid.")
    return value


def _restore_paths(
    source: Path,
    segments_directory: Path,
    journal: dict[str, Any],
) -> dict[str, Any]:
    transaction_id = journal.get("transaction_id")
    if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise ValueError("Segmented restore transaction ID is invalid.")
    if journal.get("phase") not in RESTORE_PHASES:
        raise ValueError("Segmented restore phase is invalid.")
    generation_id = journal.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise ValueError("Segmented restore generation ID is invalid.")
    hot = journal.get("hot")
    segments = journal.get("segments")
    if not isinstance(hot, dict) or not isinstance(segments, dict):
        raise ValueError("Segmented restore journal is invalid.")
    _artifact_name(hot.get("target_name"), expected=source.name, label="hot target")
    _artifact_name(
        hot.get("staged_name"),
        expected=f".{source.name}.restore-{transaction_id}",
        label="staged hot path",
    )
    _artifact_name(
        hot.get("previous_name"),
        expected=f".{source.name}.previous-{transaction_id}",
        label="prior hot path",
    )
    _artifact_name(
        segments.get("target_name"),
        expected=segments_directory.name,
        label="segments target",
    )
    _artifact_name(
        segments.get("staged_name"),
        expected=f".{segments_directory.name}.restore-{transaction_id}",
        label="staged segments path",
    )
    _artifact_name(
        segments.get("previous_name"),
        expected=f".{segments_directory.name}.previous-{transaction_id}",
        label="prior segments path",
    )
    prior_hot = hot.get("prior")
    candidate_hot = hot.get("candidate")
    prior_segments = segments.get("prior")
    candidate_segments = segments.get("candidate")
    if not (
        isinstance(prior_hot, dict)
        and prior_hot.get("kind") in {"file", "missing"}
        and _valid_file_record(candidate_hot)
        and isinstance(prior_segments, dict)
        and prior_segments.get("kind") in {"directory", "missing"}
        and _valid_tree_record(candidate_segments)
    ):
        raise ValueError("Segmented restore journal artifact record is invalid.")
    if prior_hot.get("kind") == "file" and not _valid_file_record(prior_hot):
        raise ValueError("Segmented restore prior hot record is invalid.")
    if prior_segments.get("kind") == "directory" and not _valid_tree_record(prior_segments):
        raise ValueError("Segmented restore prior segments record is invalid.")
    return {
        "hot": hot,
        "segments": segments,
        "prior_hot": prior_hot,
        "candidate_hot": candidate_hot,
        "prior_segments": prior_segments,
        "candidate_segments": candidate_segments,
        "staged_hot_path": source.parent / hot["staged_name"],
        "previous_hot_path": source.parent / hot["previous_name"],
        "staged_segments_path": segments_directory.parent / segments["staged_name"],
        "previous_segments_path": segments_directory.parent / segments["previous_name"],
    }


def _existing_matches(path: Path, matcher: Callable[[Path, Any], bool], record: Any) -> bool:
    return path_entry_exists(path) and matcher(path, record)


def _require_known_location(
    path: Path,
    *,
    allowed: tuple[tuple[Callable[[Path, Any], bool], Any], ...],
    label: str,
) -> None:
    if not path_entry_exists(path):
        return
    if not any(matcher(path, record) for matcher, record in allowed):
        raise ValueError(f"Segmented restore {label} is divergent.")


def _retire_authenticated(
    path: Path,
    *,
    authenticate: Callable[[Path], bool],
    label: str,
) -> None:
    quarantine = Path(tempfile.mkdtemp(prefix=".segmented-restore-retired-", dir=path.parent))
    os.chmod(quarantine, 0o700)
    moved = quarantine / path.name
    try:
        os.replace(path, moved)
        _fsync_directory(path.parent)
        if not authenticate(moved):
            if not path_entry_exists(path):
                os.replace(moved, path)
                _fsync_directory(path.parent)
            raise ValueError(f"Segmented restore {label} integrity check failed.")
        if moved.is_dir() and not moved.is_symlink():
            shutil.rmtree(moved)
        else:
            moved.unlink()
        _fsync_directory(quarantine)
    finally:
        try:
            quarantine.rmdir()
        except OSError:
            pass
        _fsync_directory(path.parent)


def _remove_if_present(
    path: Path,
    *,
    matcher: Callable[[Path, Any], bool],
    record: Any,
    label: str,
) -> None:
    if path_entry_exists(path):
        _retire_authenticated(
            path,
            authenticate=lambda candidate: matcher(candidate, record),
            label=label,
        )


def remove_recorded_file(path: Path, record: dict[str, Any], *, label: str) -> None:
    _remove_if_present(
        path,
        matcher=file_matches,
        record=record,
        label=label,
    )


def remove_recorded_tree(path: Path, record: dict[str, Any], *, label: str) -> None:
    _remove_if_present(
        path,
        matcher=tree_matches,
        record=record,
        label=label,
    )


def _restore_prior_file(
    target: Path,
    previous: Path,
    prior: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    if prior.get("kind") == "missing":
        _remove_if_present(target, matcher=file_matches, record=candidate, label="candidate hot")
        return
    if file_matches(target, prior):
        _remove_if_present(previous, matcher=file_matches, record=prior, label="duplicate prior hot")
        return
    if not file_matches(previous, prior):
        raise ValueError("Segmented restore prior hot artifact is unavailable.")
    _remove_if_present(target, matcher=file_matches, record=candidate, label="candidate hot")
    os.replace(previous, target)
    _fsync_directory(target.parent)


def _restore_prior_tree(
    target: Path,
    previous: Path,
    prior: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    if prior.get("kind") == "missing":
        _remove_if_present(target, matcher=tree_matches, record=candidate, label="candidate segments")
        return
    if tree_matches(target, prior):
        _remove_if_present(
            previous,
            matcher=tree_matches,
            record=prior,
            label="duplicate prior segments",
        )
        return
    if not tree_matches(previous, prior):
        raise ValueError("Segmented restore prior segment tree is unavailable.")
    _remove_if_present(target, matcher=tree_matches, record=candidate, label="candidate segments")
    os.replace(previous, target)
    _fsync_directory(target.parent)


def recover_segmented_restore(
    *,
    source: Path,
    segments_directory: Path,
    journal_path: Path,
    journal: dict[str, Any],
    journal_record: dict[str, Any],
    apply: bool,
) -> dict[str, Any]:
    if journal.get("operation") != "segmented-restore":
        raise ValueError("Segmented restore journal is invalid.")
    source = source.absolute()
    segments_directory = segments_directory.absolute()
    for suffix in ("-wal", "-shm", "-journal"):
        if path_entry_exists(Path(f"{source}{suffix}")):
            raise ValueError("Segmented restore recovery refuses SQLite sidecar artifacts.")
    paths = _restore_paths(source, segments_directory, journal)
    prior_hot = paths["prior_hot"]
    candidate_hot = paths["candidate_hot"]
    prior_segments = paths["prior_segments"]
    candidate_segments = paths["candidate_segments"]
    staged_hot = paths["staged_hot_path"]
    previous_hot = paths["previous_hot_path"]
    staged_segments = paths["staged_segments_path"]
    previous_segments = paths["previous_segments_path"]

    _require_known_location(
        source,
        allowed=((file_matches, prior_hot), (file_matches, candidate_hot)),
        label="live hot database",
    )
    _require_known_location(
        segments_directory,
        allowed=((tree_matches, prior_segments), (tree_matches, candidate_segments)),
        label="live segment tree",
    )
    _require_known_location(
        staged_hot,
        allowed=((file_matches, candidate_hot),),
        label="staged hot database",
    )
    _require_known_location(
        previous_hot,
        allowed=((file_matches, prior_hot),),
        label="prior hot database",
    )
    _require_known_location(
        staged_segments,
        allowed=((tree_matches, candidate_segments),),
        label="staged segment tree",
    )
    _require_known_location(
        previous_segments,
        allowed=((tree_matches, prior_segments),),
        label="prior segment tree",
    )

    candidate_live = file_matches(source, candidate_hot) and tree_matches(
        segments_directory,
        candidate_segments,
    )
    if candidate_live:
        try:
            reader = SegmentedHistoryReader.from_catalog(
                hot_path=source,
                catalog_path=segments_directory / "catalog.json",
                allow_pending_activation=True,
            )
            if reader.catalog_payload().get("generation_id") != journal["generation_id"]:
                raise ValueError("Segmented restore candidate generation ID does not match.")
            reader.verify_catalog_segments()
        except ValueError as exc:
            raise ValueError("Segmented restore candidate generation integrity check failed.") from exc
        if not apply:
            return {
                "apply": False,
                "phase": journal["phase"],
                "recovery_state": "candidate-ready-to-finalize",
            }
        _remove_if_present(
            previous_hot,
            matcher=file_matches,
            record=prior_hot,
            label="prior hot database",
        )
        _remove_if_present(
            previous_segments,
            matcher=tree_matches,
            record=prior_segments,
            label="prior segment tree",
        )
        _remove_if_present(
            staged_hot,
            matcher=file_matches,
            record=candidate_hot,
            label="staged hot database",
        )
        _remove_if_present(
            staged_segments,
            matcher=tree_matches,
            record=candidate_segments,
            label="staged segment tree",
        )
        _remove_if_present(
            journal_path,
            matcher=file_matches,
            record=journal_record,
            label="activation journal",
        )
        return {
            "apply": True,
            "phase": journal["phase"],
            "recovery_state": "candidate-finalized",
        }

    prior_hot_available = prior_hot.get("kind") == "missing" or file_matches(
        source, prior_hot
    ) or file_matches(previous_hot, prior_hot)
    prior_segments_available = prior_segments.get("kind") == "missing" or tree_matches(
        segments_directory, prior_segments
    ) or tree_matches(previous_segments, prior_segments)
    if not prior_hot_available or not prior_segments_available:
        raise ValueError("Segmented restore prior generation is unavailable.")
    if not apply:
        return {
            "apply": False,
            "phase": journal["phase"],
            "recovery_state": "prior-generation-ready-to-restore",
        }
    _restore_prior_file(source, previous_hot, prior_hot, candidate_hot)
    _restore_prior_tree(
        segments_directory,
        previous_segments,
        prior_segments,
        candidate_segments,
    )
    _remove_if_present(
        staged_hot,
        matcher=file_matches,
        record=candidate_hot,
        label="staged hot database",
    )
    _remove_if_present(
        staged_segments,
        matcher=tree_matches,
        record=candidate_segments,
        label="staged segment tree",
    )
    _remove_if_present(
        journal_path,
        matcher=file_matches,
        record=journal_record,
        label="activation journal",
    )
    return {
        "apply": True,
        "phase": journal["phase"],
        "recovery_state": "prior-generation-restored",
    }
