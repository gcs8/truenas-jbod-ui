from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from history_service.segment_catalog import (
    hash_descriptor,
    open_regular_file,
    path_entry_exists,
    printed_names,
    read_descriptor,
    require_unchanged,
    scan_folder,
)
from history_service.segment_reader import SegmentedHistoryReader

ACTIVATION_JOURNAL_VERSION = 1
# A segmented restore that also activates configuration files records every
# such target and the whole-import decision. Older builds refuse this version
# instead of recovering history alone (#1196).
WHOLE_IMPORT_JOURNAL_VERSION = 2
WHOLE_IMPORT_DECISIONS = frozenset({"pending", "commit", "rollback"})
IMPORT_SEAL_PURPOSE = "restore-import-seal"
# Recovery returns this state, changing nothing, while a configuration folder
# the application knows about holds a restore record it did not settle.
CONFIGURATION_RECOVERY_INCOMPLETE = "configuration-recovery-incomplete"
# Recovery returns this state, changing nothing, when a folder it lists under
# the history lock holds more than its scan bounds.
RECOVERY_SCAN_LIMIT_REACHED = "recovery-scan-limit-reached"
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
_IMPORT_SEAL_NAME = re.compile(r"\.truenas-jbod-ui-import-seal-[A-Za-z0-9_-]{1,64}\Z")


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


def record_file(path: Path, *, expected_size: int | None = None) -> dict[str, Any]:
    changed = "Segmented restore artifact changed while it was being recorded."
    with open_regular_file(
        path,
        invalid="Segmented restore artifact must be a single-link regular file.",
    ) as (descriptor, before):
        if expected_size is not None and before.st_size != expected_size:
            # A file of another size cannot match; it is not read at all.
            raise ValueError("Segmented restore artifact does not match its record.")
        sha256 = hash_descriptor(descriptor, before, changed=changed)
        require_unchanged(path, descriptor, before, changed=changed)
    return {
        "kind": "file",
        "sha256": sha256,
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


def _tree_paths(root: Path, max_entries: int | None) -> list[Path]:
    """Every entry below ``root``, sorted; more than ``max_entries`` is refused."""
    paths: list[Path] = []
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                paths.append(path)
                if max_entries is not None and len(paths) > max_entries:
                    raise ValueError("Segmented restore tree holds more entries than its record.")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    return sorted(paths)


def record_tree(root: Path, *, max_entries: int | None = None) -> dict[str, Any]:
    root_metadata = os.stat(root, follow_symlinks=False)
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Segmented restore tree must be a directory.")
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for path in _tree_paths(root, max_entries):
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
        return record_file(path, expected_size=record["size_bytes"]) == record
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
        # A tree with more entries than its record cannot match, so the walk
        # stops there instead of listing whatever else the folder holds.
        entries = len(record["directories"]) + len(record["files"])
        return record_tree(path, max_entries=entries) == record
    except (OSError, ValueError):
        return False


def _read_private_json(path: Path, *, invalid: str, changed: str) -> tuple[Any, dict[str, Any]]:
    """Read a bounded private single-link JSON file and its exact record.

    A FIFO, device or folder put in its place is refused without waiting: it
    goes through the one bounded, non-blocking reader recovery uses (#1196).
    """
    with open_regular_file(path, invalid=invalid, private=True) as (descriptor, before):
        content = read_descriptor(
            descriptor,
            before,
            limit=MAX_ACTIVATION_JOURNAL_BYTES,
            invalid=invalid,
        )
        require_unchanged(path, descriptor, before, changed=changed)
    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(invalid) from exc
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


def read_activation_journal(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, record = _read_private_json(
        path,
        invalid="History activation journal is invalid.",
        changed="History activation journal changed while it was being read.",
    )
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
    return payload, record


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
    """Record a restore keeps inside each configuration folder it changes.

    The journal lives in the history folder, which lower-trust services can
    write. The restore rewrites this record durably before each target in the
    folder changes, and recovery acts only on targets it lists, so only a
    writer of that folder can make recovery act on it (#1196).
    """
    return folder / f".truenas-jbod-ui-import-seal-{transaction_id}"


def find_import_seals(folders: Iterable[Path]) -> list[dict[str, Any]]:
    """Name the restore folder records present in ``folders``, opening none.

    ``folders`` must come from the application's own settings, never from the
    history volume. Only those folders are listed, one level deep and within
    the scan bounds (``RecoveryScanLimitReached`` past them). A record is
    matched by its exact name; a folder that exists but cannot be listed is
    reported as unreadable, because its records are unknown (#1196).
    """
    found: list[dict[str, Any]] = []
    for folder in folders:
        try:
            names = scan_folder(
                folder,
                role="configuration folder",
                matches=lambda name: _IMPORT_SEAL_NAME.fullmatch(name) is not None,
            )
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            found.append({"folder": str(folder), "device": None, "inode": None, "unreadable": True})
            continue
        for name in names:
            try:
                metadata = os.stat(Path(folder) / name, follow_symlinks=False)
            except FileNotFoundError:
                continue
            found.append(
                {
                    "folder": str(folder),
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "unreadable": False,
                }
            )
    return found


def configuration_recovery_incomplete(
    unsettled: Sequence[dict[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    """The receipt for records recovery found but did not settle; nothing changed.

    A folder that could not be listed is reported apart from the folders
    where records were seen: its contents are unknown, not evidence.
    """
    held = sorted({item["folder"] for item in unsettled if not item["unreadable"]})
    unreadable = sorted({item["folder"] for item in unsettled if item["unreadable"]})
    sentences: list[str] = []
    if held:
        sentences.append(
            "Configuration recovery is incomplete: the configuration folders listed "
            "under folders hold restore records that this recovery did not settle, so "
            "an interrupted restore may have left configuration and history from "
            "different generations. Review those folders before removing the records."
        )
    if unreadable:
        sentences.append(
            "The configuration folders listed under unreadable_folders could not be "
            "read, so it is not known whether they contain restore records. Make each one "
            "readable and searchable by the user that runs recovery (for example "
            "chmod u+rx on the folder, as its owner) and run recovery again."
        )
    sentences.append("Recovery changed nothing.")
    return {
        "apply": apply,
        "recovery_state": CONFIGURATION_RECOVERY_INCOMPLETE,
        "folders": printed_names(held),
        "folder_count": len(held),
        "unreadable_folders": printed_names(unreadable),
        "unreadable_folder_count": len(unreadable),
        "message": " ".join(sentences),
    }


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


def _valid_folder_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("device")) is int
        and value["device"] >= 0
        and type(value.get("inode")) is int
        and value["inode"] > 0
    )


def _normalized_absolute(value: Any) -> bool:
    return (
        isinstance(value, str)
        and os.path.isabs(value)
        and os.path.normpath(value) == value
        and Path(value).name not in {"", ".", ".."}
    )


def _parse_configuration_target(item: Any, *, transaction_id: str) -> dict[str, Any]:
    """Validate one recorded configuration target; ``ValueError`` names the fault."""
    if not isinstance(item, dict):
        raise ValueError("a target entry is not an object.")
    if not _normalized_absolute(item.get("path")):
        raise ValueError("a target path is not a normalized absolute path.")
    path = Path(item["path"])
    kind = item.get("kind")
    if kind not in {"file", "directory"}:
        raise ValueError("a target kind is not recognised.")
    validate = _valid_file_record if kind == "file" else _valid_tree_record
    prior = item.get("prior")
    candidate = item.get("candidate")
    if not validate(candidate) or not isinstance(prior, dict):
        raise ValueError("a target record is malformed.")
    if prior.get("kind") != "missing" and not validate(prior):
        raise ValueError("a prior target record is malformed.")
    if not _valid_folder_identity(item.get("parent")):
        raise ValueError("a target parent record is malformed.")
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
        raise ValueError("a target artifact name is unexpected.") from exc
    return {
        "entry": item,
        "path": path,
        "kind": kind,
        "parent": item["parent"],
        "staged": path.parent / item["staged_name"],
        "previous": path.parent / item["previous_name"],
        "prior": prior,
        "candidate": candidate,
        "matcher": file_matches if kind == "file" else tree_matches,
    }


def _require_disjoint(
    targets: list[dict[str, Any]],
    protected: list[Path],
    transaction_id: str,
) -> None:
    claimed = list(protected)
    for target in targets:
        record_path = import_seal_path(target["path"].parent, transaction_id)
        claimed.extend((record_path, _journal_update_path(record_path, transaction_id)))
    for target in targets:
        paths = (target["path"], target["staged"], target["previous"])
        for path in paths:
            if any(_paths_overlap(path, other) for other in claimed):
                raise ValueError("a target overlaps history or another target.")
        claimed.extend(paths)


def read_import_seal(
    path: Path,
    *,
    transaction_id: str,
    folder: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read one configuration folder's restore record and check its shape.

    The record must be a private single-link file owned by this user, written
    for ``transaction_id`` and naming ``folder`` as its own folder.
    """
    payload, record = _read_private_json(
        path,
        invalid="Restore folder record is invalid.",
        changed="Restore folder record changed while it was being read.",
    )
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or record["uid"] != geteuid():
        raise ValueError("Restore folder record is invalid.")
    anchor = payload.get("anchor") if isinstance(payload, dict) else None
    expected_keys = {"purpose", "transaction_id", "folder", "anchor", "targets"}
    if anchor == folder:
        expected_keys |= {"folders", "decision", "history"}
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload["purpose"] != IMPORT_SEAL_PURPOSE
        or payload["transaction_id"] != transaction_id
        or payload["folder"] != folder
        or not _normalized_absolute(anchor)
        or not isinstance(payload["targets"], list)
    ):
        raise ValueError("Restore folder record is invalid.")
    if anchor == folder:
        folders = payload["folders"]
        if (
            payload["decision"] not in WHOLE_IMPORT_DECISIONS
            or not isinstance(payload["history"], list)
            or not isinstance(folders, list)
            or not folders
            or not all(
                isinstance(item, dict)
                and set(item) == {"path", "device", "inode"}
                and _normalized_absolute(item["path"])
                and _valid_folder_identity(item)
                for item in folders
            )
            or folders[0]["path"] != folder
            or len({item["path"] for item in folders}) != len(folders)
        ):
            raise ValueError("Restore folder record is invalid.")
    return payload, record


def _unsealed() -> SegmentedRestoreRecoveryRefused:
    return SegmentedRestoreRecoveryRefused(
        "configuration-target-unsealed",
        "The restore journal names a configuration file whose folder holds no "
        "matching restore seal, so it was not written by the restore that "
        "changed that folder. Recovery changed nothing.",
    )


def _load_folder_record(folder: Path, transaction_id: str) -> dict[str, Any]:
    path = import_seal_path(folder, transaction_id)
    try:
        payload, record = read_import_seal(
            path,
            transaction_id=transaction_id,
            folder=str(folder),
        )
    except (OSError, ValueError) as exc:
        raise _unsealed() from exc
    return {"folder": str(folder), "path": path, "payload": payload, "record": record}


def _require_configuration_folder(folder: Path, identity: dict[str, Any] | None) -> None:
    cursor = folder
    try:
        while True:
            metadata = os.stat(cursor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("not a directory")
            if cursor == cursor.parent:
                break
            cursor = cursor.parent
        folder_metadata = os.stat(folder, follow_symlinks=False)
    except OSError as exc:
        raise SegmentedRestoreRecoveryRefused(
            "configuration-target-unavailable",
            "This interrupted restore also changed configuration files that are "
            "not reachable here. Run recovery where the configuration volume is "
            "mounted (the admin service).",
        ) from exc
    if identity is not None and (folder_metadata.st_dev, folder_metadata.st_ino) != (
        identity["device"],
        identity["inode"],
    ):
        raise SegmentedRestoreRecoveryRefused(
            "configuration-target-unavailable",
            "This interrupted restore also changed configuration files, but their "
            "folder here is not the one the restore used. Run recovery where the "
            "same configuration volume is mounted (the admin service).",
        )


def _configuration_targets(
    *,
    journal: dict[str, Any],
    journal_record: dict[str, Any],
    journal_path: Path,
    source: Path,
    segments_directory: Path,
) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]]]:
    """Resolve the configuration targets, decision and folder records of a journal.

    The journal lives in the history folder, which lower-trust services can
    write, so it is only an index: it names the transaction and the first
    configuration folder. Each configuration folder holds the restore's own
    record of every target in it; the first folder's record also lists every
    folder and holds the decision. Recovery acts only on targets those records
    list, and refuses a journal target they do not list exactly (#1196).
    """
    if journal.get("journal_version") != WHOLE_IMPORT_JOURNAL_VERSION:
        return [], None, []
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
    anchor_reference = journal.get("anchor")
    if not (
        isinstance(anchor_reference, dict)
        and set(anchor_reference) == {"path", "device", "inode"}
        and _normalized_absolute(anchor_reference["path"])
        and _valid_folder_identity(anchor_reference)
    ):
        raise _journal_invalid("the first configuration folder is not recorded.")
    indexed: list[dict[str, Any]] = []
    for item in recorded:
        try:
            indexed.append(_parse_configuration_target(item, transaction_id=transaction_id))
        except ValueError as exc:
            raise _journal_invalid(str(exc)) from exc
    try:
        _require_disjoint(indexed, protected, transaction_id)
    except ValueError as exc:
        raise _journal_invalid(str(exc)) from exc
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
    anchor_folder = anchor_reference["path"]
    _require_configuration_folder(Path(anchor_folder), anchor_reference)
    records = {anchor_folder: _load_folder_record(Path(anchor_folder), transaction_id)}
    anchor = records[anchor_folder]["payload"]
    if anchor["anchor"] != anchor_folder:
        raise _unsealed()
    listed = [item["path"] for item in anchor["folders"]]
    for target in indexed:
        folder = target["path"].parent
        _require_configuration_folder(folder, target["parent"])
        if str(folder) not in records:
            records[str(folder)] = _load_folder_record(folder, transaction_id)
        if str(folder) not in listed:
            raise _unsealed()
        if target["entry"] not in records[str(folder)]["payload"]["targets"]:
            raise SegmentedRestoreRecoveryRefused(
                "configuration-target-unrecorded",
                "The restore journal names a configuration file that the restore's "
                "own record in that folder does not list, or lists differently. "
                "Recovery changed nothing.",
            )
    # The first folder's list, not the journal, decides which folders recovery
    # converges, so a journal that omits a folder cannot leave it behind.
    targets: list[dict[str, Any]] = []
    for identity in anchor["folders"]:
        folder = Path(identity["path"])
        _require_configuration_folder(folder, identity)
        if identity["path"] not in records:
            if not path_entry_exists(import_seal_path(folder, transaction_id)):
                # The import lists a folder before writing that folder's
                # record, and changes nothing in it until the record exists.
                continue
            records[identity["path"]] = _load_folder_record(folder, transaction_id)
        # Every folder record must name this first folder; a record planted in
        # a folder its writer controls cannot claim the others.
        if records[identity["path"]]["payload"]["anchor"] != anchor_folder:
            raise _unsealed()
        for item in records[identity["path"]]["payload"]["targets"]:
            try:
                target = _parse_configuration_target(item, transaction_id=transaction_id)
            except ValueError as exc:
                raise _unsealed() from exc
            if target["path"].parent != folder or (
                target["parent"]["device"],
                target["parent"]["inode"],
            ) != (identity["device"], identity["inode"]):
                raise _unsealed()
            targets.append(target)
    try:
        _require_disjoint(targets, protected, transaction_id)
    except ValueError as exc:
        raise _unsealed() from exc
    for target in targets:
        matcher = target["matcher"]
        for location, allowed in (
            (target["path"], (target["prior"], target["candidate"])),
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
    ordered = [records[path] for path in listed if path in records]
    return targets, anchor["decision"], ordered


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


def _remove_import_seals(folder_records: list[dict[str, Any]], transaction_id: str) -> None:
    # The first folder's record names the others, so it goes last.
    for folder_record in reversed(folder_records):
        update_path = _journal_update_path(folder_record["path"], transaction_id)
        if path_entry_exists(update_path):
            try:
                _, update_record = read_import_seal(
                    update_path,
                    transaction_id=transaction_id,
                    folder=folder_record["folder"],
                )
            except (OSError, ValueError):
                # A torn update is not the restore's record; leave it for review.
                update_record = None
            if update_record is not None:
                remove_recorded_file(update_path, update_record, label="restore import seal")
        remove_recorded_file(
            folder_record["path"],
            folder_record["record"],
            label="restore import seal",
        )


def recover_segmented_restore(
    *,
    source: Path,
    segments_directory: Path,
    journal_path: Path,
    journal: dict[str, Any],
    journal_record: dict[str, Any],
    apply: bool,
    configuration_records: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Recover one segmented restore journal.

    ``configuration_records`` are the restore records found, by name only,
    in the configuration folders the application's own settings name (see
    ``find_import_seals``). If any of them is not one this recovery settles,
    recovery changes nothing and returns ``configuration-recovery-incomplete``:
    the journal may have been edited, downgraded or replaced, and a whole
    generation must not be reported while such a record remains (#1196).
    """
    if journal.get("operation") != "segmented-restore":
        raise ValueError("Segmented restore journal is invalid.")
    source = source.absolute()
    segments_directory = segments_directory.absolute()
    for suffix in ("-wal", "-shm", "-journal"):
        if path_entry_exists(Path(f"{source}{suffix}")):
            raise ValueError("Segmented restore recovery refuses SQLite sidecar artifacts.")
    paths = _restore_paths(source, segments_directory, journal)
    configuration_targets, decision, folder_records = _configuration_targets(
        journal=journal,
        journal_record=journal_record,
        journal_path=journal_path,
        source=source,
        segments_directory=segments_directory,
    )
    settled = {
        (folder_record["record"]["device"], folder_record["record"]["inode"])
        for folder_record in folder_records
    }
    unsettled = [
        item
        for item in configuration_records
        if item["unreadable"] or (item["device"], item["inode"]) not in settled
    ]
    if unsettled:
        # The find authorizes nothing: it only stops a success report.
        return configuration_recovery_incomplete(unsettled, apply=apply)
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
    # A whole import converges every target to the decision recorded in its
    # first configuration folder, never to anything the journal says; an
    # undecided whole import returns to the prior generation. A history-only
    # journal finishes forward only when the candidate is fully live.
    if decision is None:
        finish_candidate = candidate_live
    else:
        finish_candidate = decision == "commit"
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
        _remove_import_seals(folder_records, journal["transaction_id"])
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
    _remove_import_seals(folder_records, journal["transaction_id"])
    return {
        "apply": True,
        "phase": journal["phase"],
        "recovery_state": "prior-generation-restored",
    }
