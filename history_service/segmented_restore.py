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
# A segmented restore that also activates configuration files records every
# such target and the whole-import decision. Older builds refuse this version
# instead of recovering history alone (#1196).
WHOLE_IMPORT_JOURNAL_VERSION = 2
WHOLE_IMPORT_DECISIONS = frozenset({"pending", "commit", "rollback"})
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


class SegmentedRestoreRecoveryRefused(ValueError):
    """Recovery stopped before changing anything; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
        or payload.get("operation") not in {"rotate", "segmented-restore"}
        or type(payload.get("journal_version")) is not int
        or payload.get("journal_version")
        not in (
            {ACTIVATION_JOURNAL_VERSION, WHOLE_IMPORT_JOURNAL_VERSION}
            if payload.get("operation") == "segmented-restore"
            else {ACTIVATION_JOURNAL_VERSION}
        )
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


def _journal_update_path(path: Path, transaction_id: str) -> Path:
    return path.with_name(f"{path.name}.next-{transaction_id}")


def import_seal_path(folder: Path, transaction_id: str) -> Path:
    """Seal a restore writes into each configuration folder it journals.

    The journal lives in the history folder, which lower-trust services can
    write. Requiring a recorded seal inside every target folder means only a
    writer of that folder can make recovery act on it (#1196).
    """
    return folder / f".truenas-jbod-ui-import-seal-{transaction_id}"


def replace_restore_journal(
    path: Path,
    payload: dict[str, Any],
    *,
    current_record: dict[str, Any],
) -> dict[str, Any]:
    """Durably replace an authenticated restore journal with ``payload``.

    The journal path never disappears: the new content is written and fsynced
    beside it, then renamed over it, then the directory is fsynced.
    """
    if not file_matches(path, current_record):
        raise ValueError("History activation journal changed before it could be updated.")
    transaction_id = payload.get("transaction_id")
    if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise ValueError("Segmented restore transaction ID is invalid.")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_ACTIVATION_JOURNAL_BYTES:
        # Recovery cannot read a larger journal, so refuse before activation.
        raise ValueError("The restore touches too many files to record for crash recovery.")
    update_path = _journal_update_path(path, transaction_id)
    write_restore_journal(update_path, payload)
    os.replace(update_path, path)
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


def _restore_prior_target(
    target: Path,
    previous: Path,
    prior: dict[str, Any],
    candidate: dict[str, Any],
    *,
    matcher: Callable[[Path, Any], bool],
) -> None:
    if prior.get("kind") == "missing":
        _remove_if_present(target, matcher=matcher, record=candidate, label="candidate configuration")
        return
    if matcher(target, prior):
        _remove_if_present(
            previous,
            matcher=matcher,
            record=prior,
            label="duplicate prior configuration",
        )
        return
    if not matcher(previous, prior):
        raise SegmentedRestoreRecoveryRefused(
            "prior-generation-unavailable",
            "Segmented restore prior configuration is unavailable.",
        )
    _remove_if_present(target, matcher=matcher, record=candidate, label="candidate configuration")
    os.replace(previous, target)
    _fsync_directory(target.parent)


def _journal_invalid(detail: str) -> SegmentedRestoreRecoveryRefused:
    return SegmentedRestoreRecoveryRefused(
        "journal-invalid",
        f"Segmented restore journal is invalid: {detail}",
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        common = os.path.commonpath((str(first), str(second)))
    except ValueError:
        return False
    return common in {str(first), str(second)}


def _configuration_targets(
    *,
    journal: dict[str, Any],
    journal_record: dict[str, Any],
    journal_path: Path,
    source: Path,
    segments_directory: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    """Validate the non-history targets a whole-import journal recorded."""
    if journal.get("journal_version") != WHOLE_IMPORT_JOURNAL_VERSION:
        return [], None
    decision = journal.get("decision")
    if decision not in WHOLE_IMPORT_DECISIONS:
        raise _journal_invalid("the import decision is not recognised.")
    recorded = journal.get("targets")
    if not isinstance(recorded, list):
        raise _journal_invalid("the target list is missing.")
    transaction_id = journal["transaction_id"]
    protected = [
        source,
        Path(f"{source}-wal"),
        Path(f"{source}-shm"),
        Path(f"{source}-journal"),
        segments_directory,
        journal_path,
        _journal_update_path(journal_path, transaction_id),
        source.parent / f".{source.name}.restore-{transaction_id}",
        source.parent / f".{source.name}.previous-{transaction_id}",
        segments_directory.parent / f".{segments_directory.name}.restore-{transaction_id}",
        segments_directory.parent / f".{segments_directory.name}.previous-{transaction_id}",
    ]
    targets: list[dict[str, Any]] = []
    for item in recorded:
        if not isinstance(item, dict):
            raise _journal_invalid("a target entry is not an object.")
        raw_path = item.get("path")
        if (
            not isinstance(raw_path, str)
            or not os.path.isabs(raw_path)
            or os.path.normpath(raw_path) != raw_path
            or Path(raw_path).name in {"", ".", ".."}
        ):
            raise _journal_invalid("a target path is not a normalized absolute path.")
        path = Path(raw_path)
        kind = item.get("kind")
        if kind not in {"file", "directory"}:
            raise _journal_invalid("a target kind is not recognised.")
        validate = _valid_file_record if kind == "file" else _valid_tree_record
        prior = item.get("prior")
        candidate = item.get("candidate")
        if not validate(candidate) or not isinstance(prior, dict):
            raise _journal_invalid("a target record is malformed.")
        if prior.get("kind") != "missing" and not validate(prior):
            raise _journal_invalid("a prior target record is malformed.")
        parent = item.get("parent")
        if not (
            isinstance(parent, dict)
            and type(parent.get("device")) is int
            and parent["device"] >= 0
            and type(parent.get("inode")) is int
            and parent["inode"] > 0
        ):
            raise _journal_invalid("a target parent record is malformed.")
        if not _valid_file_record(item.get("seal")):
            raise _journal_invalid("a target seal record is malformed.")
        try:
            _artifact_name(
                item.get("staged_name"),
                expected=f".{path.name}.restore-{transaction_id}",
                label="staged configuration path",
            )
            _artifact_name(
                item.get("previous_name"),
                expected=f".{path.name}.previous-{transaction_id}",
                label="prior configuration path",
            )
        except ValueError as exc:
            raise _journal_invalid("a target artifact name is unexpected.") from exc
        staged = path.parent / item["staged_name"]
        previous = path.parent / item["previous_name"]
        seal = import_seal_path(path.parent, transaction_id)
        for claimed in (path, staged, previous):
            if any(_paths_overlap(claimed, other) for other in protected) or _paths_overlap(
                claimed, seal
            ):
                raise _journal_invalid("a target overlaps history or another target.")
        protected.extend((path, staged, previous))
        targets.append(
            {
                "path": path,
                "kind": kind,
                "parent": parent,
                "staged": staged,
                "previous": previous,
                "seal_path": seal,
                "seal": item["seal"],
                "prior": prior,
                "candidate": candidate,
                "matcher": file_matches if kind == "file" else tree_matches,
            }
        )
    if not targets:
        return targets, decision
    # Only the identity that wrote the journal may follow its configuration
    # paths; anyone else could otherwise point recovery at files it cannot
    # write itself.
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or journal_record.get("uid") != geteuid():
        raise SegmentedRestoreRecoveryRefused(
            "journal-owner-mismatch",
            "This interrupted restore also changed configuration files and its "
            "journal was written by a different user. Run recovery as the user "
            "that ran the restore (the admin service), with the configuration "
            "volume mounted.",
        )
    for target in targets:
        path = target["path"]
        cursor = path.parent
        try:
            while True:
                metadata = os.stat(cursor, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("not a directory")
                if cursor == cursor.parent:
                    break
                cursor = cursor.parent
            parent_metadata = os.stat(path.parent, follow_symlinks=False)
        except OSError as exc:
            raise SegmentedRestoreRecoveryRefused(
                "configuration-target-unavailable",
                "This interrupted restore also changed configuration files that are "
                "not reachable here. Run recovery where the configuration volume is "
                "mounted (the admin service).",
            ) from exc
        if (parent_metadata.st_dev, parent_metadata.st_ino) != (
            target["parent"]["device"],
            target["parent"]["inode"],
        ):
            raise SegmentedRestoreRecoveryRefused(
                "configuration-target-unavailable",
                "This interrupted restore also changed configuration files, but their "
                "folder here is not the one the restore used. Run recovery where the "
                "same configuration volume is mounted (the admin service).",
            )
        if not file_matches(target["seal_path"], target["seal"]):
            raise SegmentedRestoreRecoveryRefused(
                "configuration-target-unsealed",
                "The restore journal names a configuration file whose folder holds no "
                "matching restore seal, so it was not written by the restore that "
                "changed that folder. Recovery changed nothing.",
            )
        matcher = target["matcher"]
        for location, allowed in (
            (path, (target["prior"], target["candidate"])),
            (target["staged"], (target["candidate"],)),
            (target["previous"], (target["prior"],)),
        ):
            if path_entry_exists(location) and not any(
                matcher(location, record) for record in allowed
            ):
                raise SegmentedRestoreRecoveryRefused(
                    "configuration-target-divergent",
                    "A configuration file changed after the interrupted restore; "
                    "recovery kept every file for review.",
                )
    return targets, decision


def _remove_journal_update(journal_path: Path, transaction_id: str) -> None:
    update_path = _journal_update_path(journal_path, transaction_id)
    if not path_entry_exists(update_path):
        return
    try:
        payload, record = read_activation_journal(update_path)
    except (OSError, ValueError):
        # A torn update is not authenticated evidence; leave it for review.
        return
    if payload.get("transaction_id") == transaction_id:
        remove_recorded_file(update_path, record, label="activation journal update")


def _remove_import_seals(configuration_targets: list[dict[str, Any]]) -> None:
    removed: set[Path] = set()
    for target in configuration_targets:
        if target["seal_path"] in removed:
            continue
        removed.add(target["seal_path"])
        remove_recorded_file(target["seal_path"], target["seal"], label="restore import seal")


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
    configuration_targets, decision = _configuration_targets(
        journal=journal,
        journal_record=journal_record,
        journal_path=journal_path,
        source=source,
        segments_directory=segments_directory,
    )
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
    configuration_candidate_live = all(
        target["matcher"](target["path"], target["candidate"])
        for target in configuration_targets
    )
    # A whole-import journal converges every target to its recorded decision.
    # Without one, only a fully activated candidate is finished forward.
    if decision == "commit":
        finish_candidate = True
    elif decision == "rollback":
        finish_candidate = False
    else:
        finish_candidate = candidate_live and configuration_candidate_live
    if finish_candidate and not (candidate_live and configuration_candidate_live):
        raise SegmentedRestoreRecoveryRefused(
            "committed-generation-incomplete",
            "The interrupted restore was committed, but not every restored file "
            "still holds the committed generation; recovery kept every file for review.",
        )
    if finish_candidate:
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
        for target in configuration_targets:
            _remove_if_present(
                target["previous"],
                matcher=target["matcher"],
                record=target["prior"],
                label="prior configuration",
            )
            _remove_if_present(
                target["staged"],
                matcher=target["matcher"],
                record=target["candidate"],
                label="staged configuration",
            )
        _remove_journal_update(journal_path, journal["transaction_id"])
        _remove_if_present(
            journal_path,
            matcher=file_matches,
            record=journal_record,
            label="activation journal",
        )
        _remove_import_seals(configuration_targets)
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
    for target in configuration_targets:
        matcher = target["matcher"]
        if not (
            target["prior"].get("kind") == "missing"
            or matcher(target["path"], target["prior"])
            or matcher(target["previous"], target["prior"])
        ):
            raise SegmentedRestoreRecoveryRefused(
                "prior-generation-unavailable",
                "The configuration from before the interrupted restore is no longer "
                "available, so recovery cannot return to it; every file was kept.",
            )
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
    for target in configuration_targets:
        _restore_prior_target(
            target["path"],
            target["previous"],
            target["prior"],
            target["candidate"],
            matcher=target["matcher"],
        )
        _remove_if_present(
            target["staged"],
            matcher=target["matcher"],
            record=target["candidate"],
            label="staged configuration",
        )
    _remove_journal_update(journal_path, journal["transaction_id"])
    _remove_if_present(
        journal_path,
        matcher=file_matches,
        record=journal_record,
        label="activation journal",
    )
    _remove_import_seals(configuration_targets)
    return {
        "apply": True,
        "phase": journal["phase"],
        "recovery_state": "prior-generation-restored",
    }
