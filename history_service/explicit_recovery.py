"""Offline #417 bundle admission, paused apply/replay and explicit finalization.

Read-only planning remains unchanged. Explicit apply uses recovery_apply to
publish selected history while retaining pause; explicit resume authenticates
selected v1 journals and replays only to applied-paused.
No HistoryStore construction, ambient configuration or automatic repair.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack, closing
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import zipfile

from history_service.migration_lock import _history_lifecycle_lock, _database_path_is_mount_point
from history_service.recovery_state import ARTIFACTS, MAX_RECORD_BYTES, _unique_object, recovery_path

MAX_EVIDENCE_BYTES = 6 * 1024 * 1024 * 1024
MAX_READ_SECONDS = 120


class RecoveryRefusal(ValueError):
    def __init__(self, reason: str, exit_code: int = 4):
        self.reason = reason
        self.exit_code = exit_code
        super().__init__(reason)


def _require(condition, reason="evidence-conflict"):
    if not condition:
        raise RecoveryRefusal(reason)


def _token(value, length):
    _require(
        isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None,
        "invalid-selection-token",
    )


def _database(value):
    path = Path(value)
    _require(path.is_absolute() and path.name not in ("", ".", ".."), "explicit-absolute-database-required")
    return path.parent.resolve(strict=True) / path.name


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
    )


def _names(descriptor, maximum):
    with os.scandir(descriptor) as entries:
        result = set()
        for entry in entries:
            result.add(entry.name)
            _require(len(result) <= maximum, "unknown-topology")
        return result


def _open(stack, name, parent, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    fd = os.open(name, flags, dir_fd=parent)
    stack.callback(os.close, fd)
    _require(_identity(before) == _identity(os.fstat(fd)))
    return fd, before


def _record(stack, parent, name):
    root, root_info = _open(stack, name, parent, directory=True)
    _require(stat.S_IMODE(root_info.st_mode) == 0o700 and root_info.st_uid == os.geteuid())
    fd, info = _open(stack, "intent.json", root)
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_size <= MAX_RECORD_BYTES
    )
    raw = os.read(fd, MAX_RECORD_BYTES + 1)
    _require(len(raw) == info.st_size and _identity(info) == _identity(os.fstat(fd)))
    record = json.loads(raw, object_pairs_hook=_unique_object)
    _require(isinstance(record, dict) and set(record) == {"version", "id", "policy", "phase", "artifacts"})
    _require(
        type(record["version"]) is int
        and record["version"] == 1
        and record["policy"] == "pause"
        and record["phase"] == "intent"
    )
    _token(record["id"], 32)
    artifacts = record["artifacts"]
    _require(isinstance(artifacts, dict) and "main" in artifacts and set(artifacts) <= ARTIFACTS.keys())
    for facts in artifacts.values():
        _require(isinstance(facts, dict) and set(facts) == {"dev", "ino", "size", "mtime_ns", "sha256"})
        _require(all(type(facts[k]) is int and facts[k] >= 0 for k in ("dev", "ino", "size", "mtime_ns")))
        _token(facts["sha256"], 64)
    _require(_identity(info) == _identity(os.stat("intent.json", dir_fd=root, follow_symlinks=False)))
    _require(_identity(root_info) == _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)))
    return root, root_info, fd, info, raw, record


def inspect(database: Path) -> dict:
    """Bounded local selection metadata only. Does not hash evidence payloads."""
    try:
        database = _database(database)
        with ExitStack() as stack:
            parent = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, parent)
            name = recovery_path(database).name
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                from history_service.recovery_state import inspect_recovery

                return {"state": inspect_recovery(database), "restore_available": False}
            _, _, _, _, raw, record = _record(stack, parent, name)
            return {
                "state": "required",
                "recovery_id": record["id"],
                "intent_sha256": hashlib.sha256(raw).hexdigest(),
                "restore_available": False,
            }
    except (OSError, ValueError, TypeError, RecursionError):
        return {"state": "invalid", "restore_available": False}


def _hash(fd, info, deadline):
    _require(info.st_size <= MAX_EVIDENCE_BYTES, "evidence-bounds")
    os.lseek(fd, 0, os.SEEK_SET)
    result = hashlib.sha256()
    total = 0
    while chunk := os.read(fd, 1024 * 1024):
        total += len(chunk)
        _require(total <= info.st_size and time.monotonic() <= deadline, "evidence-bounds")
        result.update(chunk)
    _require(total == info.st_size and _identity(info) == _identity(os.fstat(fd)))
    return result.hexdigest()


@contextmanager
def _admitted_evidence(database: Path, *, recovery_id: str, intent_sha256: str, resolved_topology: str):
    """Private ownership/admission shared by planner and bounded publisher.

    The planner must call the returned unchanged-inventory check on exit. The
    publisher instead verifies each explicit transition under the same lock.
    """
    _token(recovery_id, 32)
    _token(intent_sha256, 64)
    _require(resolved_topology == "unsegmented", "resolved-unsegmented-topology-required")
    try:
        database = _database(database)
        _require(not _database_path_is_mount_point(database), "database-file-mount-unsupported")
        with _history_lifecycle_lock(database, blocking=False) as parent, ExitStack() as stack:
            parent_info = os.fstat(parent)
            root_name = recovery_path(database).name
            root, root_info, record_fd, record_info, raw, record = _record(stack, parent, root_name)
            _require(
                record["id"] == recovery_id and hashlib.sha256(raw).hexdigest() == intent_sha256, "stale-selection"
            )
            allowed_parent = {root_name} | {database.name + suffix for suffix in ARTIFACTS.values()}
            parent_names = _names(parent, len(allowed_parent))
            _require(parent_names <= allowed_parent, "unknown-topology")
            root_names = _names(root, 5)
            _require(root_names <= {"intent.json"} | set(record["artifacts"]), "unknown-recovery-artifact")
            _require(sum(f["size"] for f in record["artifacts"].values()) <= MAX_EVIDENCE_BYTES, "evidence-bounds")
            pinned = []
            observed = {}
            identities = set()
            deadline = time.monotonic() + MAX_READ_SECONDS
            for role, suffix in ARTIFACTS.items():
                original_name = database.name + suffix
                locations = [(parent, original_name, "original"), (root, role, "retained")]
                existing = [
                    (directory, name, label)
                    for directory, name, label in locations
                    if name in (parent_names if directory == parent else root_names)
                ]
                facts = record["artifacts"].get(role)
                if facts is None:
                    _require(not existing, "unrecorded-sidecar")
                    continue
                _require(existing, "missing-evidence")
                identity = (facts["dev"], facts["ino"])
                _require(identity not in identities, "duplicate-role-inode")
                identities.add(identity)
                observed[role] = {}
                for directory, name, label in existing:
                    fd, info = _open(stack, name, directory)
                    _require(
                        stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == len(existing)
                    )
                    _require(
                        (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                        == (facts["dev"], facts["ino"], facts["size"], facts["mtime_ns"])
                    )
                    _require(_hash(fd, info, deadline) == facts["sha256"])
                    pinned.append((directory, name, fd, info, facts["sha256"]))
                    observed[role][label] = {
                        "mode": stat.S_IMODE(info.st_mode),
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "links": info.st_nlink,
                    }

            def recheck():
                _require(_identity(parent_info) == _identity(os.stat(database.parent)))
                _require(_identity(root_info) == _identity(os.stat(root_name, dir_fd=parent, follow_symlinks=False)))
                _require(
                    _identity(record_info) == _identity(os.stat("intent.json", dir_fd=root, follow_symlinks=False))
                )
                _require(_hash(record_fd, record_info, time.monotonic() + MAX_READ_SECONDS) == intent_sha256)
                _require(_names(root, 5) == root_names and _names(parent, len(allowed_parent)) == parent_names)
                for directory, name, fd, info, expected in pinned:
                    _require(_identity(info) == _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)))
                    _require(_hash(fd, info, time.monotonic() + MAX_READ_SECONDS) == expected)

            recheck()
            yield {
                "summary": {"recovery_id": recovery_id, "intent_sha256": intent_sha256, "artifacts": observed},
                "recheck": recheck, "parent": parent, "root": root, "record": record,
                "intent_info": record_info, "root_info": root_info, "parent_info": parent_info,
            }
    except RecoveryRefusal:
        raise
    except sqlite3.OperationalError:
        raise RecoveryRefusal("lifecycle-busy", 3) from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise RecoveryRefusal("evidence-conflict") from None


@contextmanager
def admit_evidence(database: Path, *, recovery_id: str, intent_sha256: str, resolved_topology: str):
    """Read-only admission retains exact unchanged-inventory exit validation."""
    with _admitted_evidence(database, recovery_id=recovery_id, intent_sha256=intent_sha256,
                            resolved_topology=resolved_topology) as admitted:
        yield admitted["summary"]
        admitted["recheck"]()


def _validate_candidate(candidate, candidate_digest):
    from history_service.schema_compatibility import classify_schema

    before = _identity(candidate.stat())
    for suffix in ("-wal", "-shm", "-journal"):
        _require(not os.path.lexists(str(candidate) + suffix), "candidate-sidecar")
    # Closed private standalone file, no external publisher and no ignored WAL.
    with closing(sqlite3.connect(candidate.as_uri() + "?mode=ro", uri=True, timeout=0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-8192")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
        deadline = time.monotonic() + 30
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        classification = classify_schema(connection)
        _require(
            classification.state == "supported" and classification.backfill_marker == 1, "candidate-schema-ineligible"
        )
        _require(connection.execute("PRAGMA integrity_check").fetchmany(2) == [("ok",)], "candidate-integrity")
        _require(connection.execute("PRAGMA foreign_key_check").fetchone() is None, "candidate-foreign-key")
    _require(_identity(candidate.stat()) == before)
    for suffix in ("-wal", "-shm", "-journal"):
        _require(not os.path.lexists(str(candidate) + suffix), "candidate-sidecar")
    with candidate.open("rb") as source:
        _require(hashlib.file_digest(source, "sha256").hexdigest() == candidate_digest)
    return classification


def _bundle_plan(bundle, expected, workspace):
    # Reuse the existing bounded archive validator, not ordinary restore or its
    # multi-group activation. This bounded slice accepts plaintext ZIP only.
    from history_service.system_backup import (
        SystemBackupService as Validator,
        MAX_BACKUP_ARCHIVE_BYTES,
        MAX_MANIFEST_BYTES,
        HISTORY_DB_KEY,
    )
    private = workspace / "bundle.zip"
    with ExitStack() as stack:
        fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_BACKUP_ARCHIVE_BYTES,
            "bundle-bounds",
        )
        deadline = time.monotonic() + MAX_READ_SECONDS
        _require(_hash(fd, info, deadline) == expected, "bundle-digest-conflict")
        os.lseek(fd, 0, os.SEEK_SET)
        with private.open("xb") as output:
            total = 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                _require(total <= info.st_size and time.monotonic() <= deadline, "bundle-bounds")
                _require(output.write(chunk) == len(chunk), "scratch-short-write")
            _require(total == info.st_size)
        _require(_identity(info) == _identity(os.fstat(fd)))
        _require(_identity(info) == _identity(os.stat(bundle, follow_symlinks=False)))
    private.chmod(0o600)
    with private.open("rb") as source:
        _require(hashlib.file_digest(source, "sha256").hexdigest() == expected, "bundle-digest-conflict")
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            Validator._preflight_zip_archive(mapped)
    with zipfile.ZipFile(private, "r") as archive:
        members = archive.infolist()
        Validator._validate_zip_members(members)
        paths = [m.filename for m in members if not m.is_dir()]
        Validator._validate_unique_physical_archive_paths(paths)
        meta = archive.getinfo("manifest.json")
        _require(meta.file_size <= MAX_MANIFEST_BYTES, "manifest-bounds")
        with archive.open(meta) as source:
            raw = Validator._read_bounded_stream(source, MAX_MANIFEST_BYTES)
        manifest = Validator._load_manifest(raw)
        Validator._validate_manifest_before_extraction(manifest)
        _require(
            type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1,
            "unsegmented-bundle-required",
        )
        _require(len(manifest.get("files", [])) == 1, "history-only-bundle-required")
        member = manifest["files"][0]
        _require(
            member.get("key") == HISTORY_DB_KEY
            and member.get("group_key") == HISTORY_DB_KEY
            and member.get("archive_path") == "history/history.sqlite3",
            "history-only-bundle-required",
        )
        _token(member.get("sha256"), 64)
        _require(type(member.get("size_bytes")) is int and member["size_bytes"] > 0, "member-size-required")
        _require(set(paths) == {"manifest.json", "history/history.sqlite3"}, "standalone-history-required")
        Validator._validate_supported_physical_archive_paths(paths, manifest)
        extract = workspace / "extract"
        extract.mkdir(mode=0o700)
        candidate = extract / "history.sqlite3"
        with archive.open("history/history.sqlite3") as source, candidate.open("xb") as output:
            total = 0
            deadline = time.monotonic() + MAX_READ_SECONDS
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                _require(total <= member["size_bytes"] and time.monotonic() <= deadline, "member-bounds")
                written = output.write(chunk)
                _require(type(written) is int and written == len(chunk), "scratch-short-write")
            _require(total == member["size_bytes"], "member-size-conflict")
            output.flush()
            os.fsync(output.fileno())
        candidate.chmod(0o600)
    _require(isinstance(candidate, Path), "private-candidate-required")
    with candidate.open("rb") as source:
        candidate_digest = hashlib.file_digest(source, "sha256").hexdigest()
    _require(
        candidate_digest == member["sha256"] and candidate.stat().st_size == member["size_bytes"],
        "member-digest-conflict",
    )
    classification = _validate_candidate(candidate, candidate_digest)
    return {
        "bundle_sha256": expected,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "history_sha256": candidate_digest,
        "history_bytes": member["size_bytes"],
        "schema": classification.state,
        "backfill_marker": classification.backfill_marker,
    }


def plan_bundle(
    database: Path,
    *,
    recovery_id: str,
    intent_sha256: str,
    bundle: Path,
    bundle_sha256: str,
    resolved_topology: str,
    scratch_parent: Path,
) -> dict:
    _token(recovery_id, 32)
    _token(intent_sha256, 64)
    _token(bundle_sha256, 64)
    try:
        database = _database(database)
        scratch = Path(scratch_parent).resolve(strict=True)
        _require(not scratch.is_relative_to(database.parent), "scratch-must-be-outside-target")
        scratch_info = scratch.stat()
        _require(
            stat.S_ISDIR(scratch_info.st_mode)
            and scratch_info.st_uid == os.geteuid()
            and stat.S_IMODE(scratch_info.st_mode) == 0o700,
            "private-scratch-required",
        )
        with admit_evidence(
            database, recovery_id=recovery_id, intent_sha256=intent_sha256, resolved_topology=resolved_topology
        ):
            with tempfile.TemporaryDirectory(prefix="history-recovery-plan-", dir=scratch) as workspace:
                result = _bundle_plan(Path(bundle), bundle_sha256, Path(workspace))
        return {
            "state": "admitted-plan-only",
            "restore_available": False,
            "recovery_id": recovery_id,
            "intent_sha256": intent_sha256,
            **result,
        }
    except RecoveryRefusal:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error, zipfile.BadZipFile, RuntimeError):
        raise RecoveryRefusal("bundle-admission-refused") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline paused-history admission, apply, resume and verified finalization.")
    commands = parser.add_subparsers(dest="command", required=True)
    observation = commands.add_parser("inspect")
    observation.add_argument("--database", required=True, type=Path)
    plan = commands.add_parser("restore-bundle", help="plan or apply verified history; apply deliberately keeps pause")
    plan.add_argument("--database", required=True, type=Path)
    plan.add_argument("--recovery-id", required=True)
    plan.add_argument("--intent-sha256", required=True)
    plan.add_argument("--bundle", required=True, type=Path)
    plan.add_argument("--bundle-sha256", required=True)
    plan.add_argument("--history-only", required=True, action="store_true")
    mode = plan.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    plan.add_argument("--offline", action="store_true")
    plan.add_argument("--accept-backup-history", action="store_true")
    plan.add_argument("--publication-uid", type=int)
    plan.add_argument("--publication-gid", type=int)
    plan.add_argument("--publication-mode", choices=["0600"])
    plan.add_argument("--resolved-topology", required=True, choices=["unsegmented"])
    plan.add_argument("--scratch-parent", required=True, type=Path)
    resume = commands.add_parser("resume", help="replay selected immutable journal; deliberately keeps pause")
    resume.add_argument("--database", required=True, type=Path)
    resume.add_argument("--recovery-id", required=True)
    resume.add_argument("--intent-sha256", required=True)
    resume.add_argument("--offline", required=True, action="store_true")
    resume.add_argument("--apply", required=True, action="store_true")
    resume.add_argument("--resolved-topology", choices=["unsegmented"])
    resume.add_argument("--operation-sha256")
    resume.add_argument("--operation-identity")
    resume.add_argument("--receipt-sha256")
    resume.add_argument("--receipt-identity")
    final = commands.add_parser("finalize", help="explicit verified archive completion or archived replay")
    final.add_argument("--database", required=True, type=Path)
    final.add_argument("--recovery-id", required=True)
    final.add_argument("--intent-sha256", required=True)
    final.add_argument("--offline", required=True, action="store_true")
    final.add_argument("--apply", required=True, action="store_true")
    final.add_argument("--resolved-topology", required=True, choices=["unsegmented"])
    for selected in ("operation", "receipt", "gate", "completion"):
        final.add_argument("--" + selected + "-sha256")
        final.add_argument("--" + selected + "-identity")
    args = parser.parse_args(argv)
    if not args.database.is_absolute():
        parser.error("--database must be an explicit absolute path")
    try:
        if args.command == "inspect":
            result = inspect(args.database)
            code = 0 if result["state"] in ("none", "required") else 4
        elif args.command == "finalize":
            from history_service.recovery_finalize import finalize

            result = finalize(args.database, recovery_id=args.recovery_id, intent_sha256=args.intent_sha256,
                              resolved_topology=args.resolved_topology, offline=args.offline,
                              **{key: getattr(args, key) for selected in ("operation", "receipt", "gate", "completion")
                                 for key in (selected + "_sha256", selected + "_identity")})
            code = 0
        elif args.command == "resume":
            from history_service.recovery_resume import resume as replay

            result = replay(args.database, recovery_id=args.recovery_id, intent_sha256=args.intent_sha256,
                            resolved_topology=args.resolved_topology, offline=args.offline,
                            operation_sha256=args.operation_sha256, operation_identity=args.operation_identity,
                            receipt_sha256=args.receipt_sha256, receipt_identity=args.receipt_identity)
            code = 5
        elif args.apply:
            from history_service.recovery_apply import apply_bundle

            result = apply_bundle(
                args.database, recovery_id=args.recovery_id, intent_sha256=args.intent_sha256,
                bundle=args.bundle, bundle_sha256=args.bundle_sha256, resolved_topology=args.resolved_topology,
                scratch_parent=args.scratch_parent, offline=args.offline, accept_backup_history=args.accept_backup_history,
                publication_uid=args.publication_uid, publication_gid=args.publication_gid,
                publication_mode=args.publication_mode,
            )
            code = 5
        else:
            result = plan_bundle(
                args.database,
                recovery_id=args.recovery_id,
                intent_sha256=args.intent_sha256,
                bundle=args.bundle,
                bundle_sha256=args.bundle_sha256,
                resolved_topology=args.resolved_topology,
                scratch_parent=args.scratch_parent,
            )
            code = 0
        print(json.dumps(result, sort_keys=True))
        return code
    except RecoveryRefusal as exc:
        result = {"state": "refused", "reason": exc.reason, "restore_available": False}
        if exc.reason == 'finalization-commit-outcome-unknown':
            result.update(state='outcome-unknown', recovery_completed=None)
        elif exc.reason == 'finalization-committed-replay-failed':
            result.update(state='completed-replay-failed', recovery_completed=True)
        print(json.dumps(result, sort_keys=True))
        return 2 if exc.reason == "invalid-selection-token" else exc.exit_code
