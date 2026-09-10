"""Bounded offline journaled apply, deliberately without pause clearing.

This is not completed recovery: every path retains the original recovery root.
Explicit resume authenticates v1 journals in recovery_resume. No normal store is
constructed and no automatic recovery or live unlatch exists here.
Only explicit offline apply/resume call the mutation helpers in this module.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from history_service import explicit_recovery as admission
from history_service.recovery_state import ARTIFACTS, recovery_path

CANDIDATE = "candidate.sqlite3"
OPERATION = "recovery-operation.json"
COMPLETED = "completed.json"
MAX_OPERATION_BYTES = 16384


def _boundary(name):
    """Synthetic process-fault seam; no environment-controlled production faults."""


def _step(name, action):
    _boundary("before:" + name)
    result = action()
    _boundary("after:" + name)
    return result


def _facts(info, digest):
    return dict(dev=info.st_dev, ino=info.st_ino, size=info.st_size,
                mtime_ns=info.st_mtime_ns, sha256=digest,
                mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid)


def _file(parent, name, expected=None, links=1):
    with ExitStack() as stack:
        fd, info = admission._open(stack, name, parent)
        admission._require(stat.S_ISREG(info.st_mode) and info.st_nlink == links
                           and info.st_uid == os.geteuid())
        facts = _facts(info, admission._hash(fd, info, time.monotonic() + admission.MAX_READ_SECONDS))
        admission._require(admission._identity(info) == admission._identity(
            os.stat(name, dir_fd=parent, follow_symlinks=False)))
        if expected is not None:
            admission._require(facts == expected)
        return facts


def _exclusive_file(parent, name, chunks):
    """Leave even partial files in place on failure. Never adopt or clean them."""
    fd = _step(name + ":create", lambda: os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent))
    try:
        _step(name + ":mode", lambda: os.fchmod(fd, 0o600))
        for index, chunk in enumerate(chunks):
            written = _step(name + ":write:" + str(index), lambda: os.write(fd, chunk))
            admission._require(type(written) is int and written == len(chunk), "target-short-write")
        _step(name + ":file-fsync", lambda: os.fsync(fd))
    finally:
        os.close(fd)
    _step(name + ":directory-fsync", lambda: os.fsync(parent))
    return _step(name + ":readback", lambda: _file(parent, name))


def _receipt(parent, name, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    admission._require(len(raw) <= MAX_OPERATION_BYTES, "operation-bounds")
    facts = _exclusive_file(parent, name, [raw])
    admission._require(facts["sha256"] == hashlib.sha256(raw).hexdigest())
    return facts


def _directory_identity(info):
    # Directory entries and ctime intentionally change. Identity/ownership do not.
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _verify(database, admitted, produced, *, active=False):
    return _step("evidence-readback", lambda: _verify_locked(database, admitted, produced, active=active))


def _verify_locked(database, admitted, produced, *, active=False):
    """Authenticate complete topology at each mutation; never SQLite-open originals."""
    parent, root = admitted["parent"], admitted["root"]
    admission._require(_directory_identity(admitted["parent_info"]) ==
                       _directory_identity(os.stat(database.parent)))
    admission._require(_directory_identity(admitted["root_info"]) == _directory_identity(
        os.stat(recovery_path(database).name, dir_fd=parent, follow_symlinks=False)))
    intent = admitted["intent_info"]
    admission._require(admission._identity(intent) == admission._identity(
        os.stat("intent.json", dir_fd=root, follow_symlinks=False)))
    _file(root, "intent.json", _facts(intent, admitted["summary"]["intent_sha256"]))
    original_names = {database.name + suffix for suffix in ARTIFACTS.values()}
    parent_names = admission._names(parent, 5)
    root_names = admission._names(root, 8)
    admission._require(parent_names <= original_names | {recovery_path(database).name}, "unknown-topology")
    admission._require(root_names <= {"intent.json"} | set(admitted["record"]["artifacts"]) | set(produced),
                       "unknown-recovery-artifact")
    for name, facts in produced.items():
        if name == CANDIDATE and active:
            links = 1 + int(name in root_names)
            _file(parent, database.name, facts, links)
            if name in root_names:
                _file(root, name, facts, links)
        else:
            admission._require(name in root_names, "missing-produced-artifact")
            _file(root, name, facts)
    for role, suffix in ARTIFACTS.items():
        name = database.name + suffix
        locations = []
        if name in parent_names and not (role == "main" and active):
            locations.append((parent, name))
        if role in root_names:
            locations.append((root, role))
        facts = admitted["record"]["artifacts"].get(role)
        if facts is None:
            admission._require(not locations, "unrecorded-sidecar")
            continue
        admission._require(locations, "missing-evidence")
        observed = next(iter(admitted["summary"]["artifacts"][role].values()))
        expected = dict(facts, **{k: observed[k] for k in ("mode", "uid", "gid")})
        for directory, entry in locations:
            _file(directory, entry, expected, len(locations))
        if active:
            admission._require(locations == [(root, role)], "original-evidence-not-retained")


def apply_bundle(database, *, recovery_id, intent_sha256, bundle, bundle_sha256,
                 resolved_topology, scratch_parent, offline, accept_backup_history,
                 publication_uid, publication_gid, publication_mode):
    """Publish verified bytes but leave durable pause. Returns incomplete (CLI 5).

    A fresh process must NOT start writing before separate explicit finalization.
    A complete interrupted journal may be selected for explicit paused replay.
    """
    admission._token(recovery_id, 32)
    admission._token(intent_sha256, 64)
    admission._token(bundle_sha256, 64)
    admission._require(offline is True and accept_backup_history is True, "offline-consent-required")
    admission._require(type(publication_uid) is int and publication_uid == os.geteuid()
                       and type(publication_gid) is int and publication_gid == os.getegid()
                       and publication_mode == "0600", "private-current-owner-publication-required")
    mutated = False
    try:
        database = admission._database(database)
        scratch = Path(scratch_parent).resolve(strict=True)
        info = scratch.stat()
        admission._require(not scratch.is_relative_to(database.parent), "scratch-must-be-outside-target")
        admission._require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                           and stat.S_IMODE(info.st_mode) == 0o700, "private-scratch-required")
        with admission._admitted_evidence(database, recovery_id=recovery_id, intent_sha256=intent_sha256,
                                          resolved_topology=resolved_topology) as admitted:
            parent, root = admitted["parent"], admitted["root"]
            with tempfile.TemporaryDirectory(prefix="history-recovery-apply-", dir=scratch) as workspace:
                selected = admission._bundle_plan(Path(bundle), bundle_sha256, Path(workspace))
                admitted["recheck"]()
                mutated = True
                with (Path(workspace) / "extract/history.sqlite3").open("rb") as source:
                    candidate = _exclusive_file(root, CANDIDATE, iter(lambda: source.read(1024 * 1024), b""))
                admission._require(candidate["sha256"] == selected["history_sha256"]
                                   and candidate["size"] == selected["history_bytes"]
                                   and candidate["mode"] == 0o600 and candidate["uid"] == publication_uid
                                   and candidate["gid"] == publication_gid)
                produced = {CANDIDATE: candidate}
                _verify(database, admitted, produced)
                _step("candidate:schema-readback", lambda: admission._validate_candidate(
                    recovery_path(database) / CANDIDATE, candidate["sha256"]))
                _verify(database, admitted, produced)
                operation = dict(version=1, operation="restore-bundle", phase="prepared",
                                 recovery_id=recovery_id, intent_sha256=intent_sha256,
                                 intent_identity=list(admission._identity(admitted["intent_info"])),
                                 source=selected, candidate=candidate, candidate_name=CANDIDATE,
                                 archive_name=database.name + ".recovery-archive-" + recovery_id,
                                 artifacts=admitted["record"]["artifacts"], observed=admitted["summary"]["artifacts"],
                                 publication_policy="apply-only-keep-paused")
                produced[OPERATION] = _receipt(root, OPERATION, operation)
                # Every original inode is durable under its retained name before
                # its original alias is removed. Existing authentic links stay.
                for role, suffix in ARTIFACTS.items():
                    if role not in admitted["record"]["artifacts"]:
                        continue
                    _verify(database, admitted, produced)
                    name = database.name + suffix
                    if role not in admission._names(root, 8):
                        _step(role + ":retain-link", lambda: os.link(
                            name, role, src_dir_fd=parent, dst_dir_fd=root, follow_symlinks=False))
                    _step(role + ":retained-directory-fsync", lambda: os.fsync(root))
                    _verify(database, admitted, produced)
                    if name in admission._names(parent, 5):
                        _step(role + ":original-unlink", lambda: os.unlink(name, dir_fd=parent))
                    _step(role + ":original-directory-fsync", lambda: os.fsync(parent))
                _verify(database, admitted, produced)
                _step("candidate:publish-link", lambda: os.link(
                    CANDIDATE, database.name, src_dir_fd=root, dst_dir_fd=parent, follow_symlinks=False))
                _step("candidate:published-directory-fsync", lambda: os.fsync(parent))
                _verify(database, admitted, produced, active=True)
                _step("candidate:staging-unlink", lambda: os.unlink(CANDIDATE, dir_fd=root))
                _step("candidate:staging-directory-fsync", lambda: os.fsync(root))
                _verify(database, admitted, produced, active=True)
                with ExitStack() as stack:
                    active_fd, _ = admission._open(stack, database.name, parent)
                    _step("active:file-fsync", lambda: os.fsync(active_fd))
                _step("active:directory-fsync", lambda: os.fsync(parent))
                _step("active:schema-readback", lambda: admission._validate_candidate(database, candidate["sha256"]))
                _verify(database, admitted, produced, active=True)
                produced[COMPLETED] = _receipt(root, COMPLETED, dict(
                    version=1, phase="applied-paused", recovery_id=recovery_id, intent_sha256=intent_sha256,
                    operation=produced[OPERATION], candidate=candidate, schema="supported", backfill_marker=1,
                    recovery_completed=False))
                _step("final:evidence-readback", lambda: _verify(database, admitted, produced, active=True))
        return dict(state="applied-paused", recovery_completed=False, resume_available=True,
                    reason="explicit-finalization-required", operation=produced[OPERATION], receipt=produced[COMPLETED])
    except Exception as exc:
        if mutated:
            raise admission.RecoveryRefusal("apply-incomplete-still-paused", 5) from None
        if isinstance(exc, admission.RecoveryRefusal):
            raise
        raise admission.RecoveryRefusal("bundle-admission-refused") from None
