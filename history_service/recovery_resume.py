"""Explicit replay of accepted v1 apply journals. Never archives or clears pause.

Caller-selected digest and device/inode anchors are mandatory for the immutable
operation and for any existing apply receipt. The v1 intent cannot authenticate
files created later. Do not silently derive trusted anchors from those files.
These are stale-selection guards, not signatures or protection from an owner
who can rewrite both the evidence and the externally supplied selection.
"""
from contextlib import ExitStack
import hashlib
import json
import os
import re
import sqlite3
import stat

from history_service import explicit_recovery as admission
from history_service import recovery_apply as apply
from history_service.migration_lock import _history_lifecycle_lock, _database_path_is_mount_point
from history_service.recovery_state import ARTIFACTS, _unique_object, recovery_path


def _shape(value, keys):
    admission._require(type(value) is dict and set(value) == set(keys), "operation-schema")


def _integers(value, keys):
    admission._require(all(type(value[k]) is int and 0 <= value[k] <= 2**64 - 1 for k in keys),
                       "operation-schema")


def _facts(value, *, extended=True):
    numbers = ("dev", "ino", "size", "mtime_ns") + (("mode", "uid", "gid") if extended else ())
    _shape(value, (*numbers, "sha256"))
    _integers(value, numbers)
    admission._token(value["sha256"], 64)
    admission._require(value["size"] <= admission.MAX_EVIDENCE_BYTES, "evidence-bounds")
    if extended:
        admission._require(value["mode"] <= 0o7777 and value["uid"] == os.geteuid())


def _anchor(digest, identity):
    admission._require(digest is not None and identity is not None, "journal-selection-required")
    admission._token(digest, 64)
    admission._require(type(identity) is str and re.fullmatch(
        r"(?:0|[1-9][0-9]{0,19}):(?:0|[1-9][0-9]{0,19})", identity) is not None,
        "invalid-journal-identity")
    result = tuple(int(part) for part in identity.split(":"))
    admission._require(all(n <= 2**64 - 1 for n in result), "invalid-journal-identity")
    return digest, result


def _metadata(stack, root, name, anchor):
    fd, info = admission._open(stack, name, root)
    admission._require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                       and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid()
                       and 0 < info.st_size <= apply.MAX_OPERATION_BYTES, "operation-bounds")
    raw = os.read(fd, apply.MAX_OPERATION_BYTES + 1)
    admission._require(len(raw) == info.st_size and admission._identity(info) == admission._identity(os.fstat(fd)))
    digest = hashlib.sha256(raw).hexdigest()
    admission._require((digest, (info.st_dev, info.st_ino)) == anchor, "stale-journal-selection")
    admission._require(admission._identity(info) == admission._identity(
        os.stat(name, dir_fd=root, follow_symlinks=False)))
    value = json.loads(raw, object_pairs_hook=_unique_object)
    return value, apply._facts(info, digest)


def _operation(value, database, admitted):
    _shape(value, ("version", "operation", "phase", "recovery_id", "intent_sha256", "intent_identity",
                   "source", "candidate", "candidate_name", "archive_name", "artifacts", "observed",
                   "publication_policy"))
    admission._require(type(value["version"]) is int and value["version"] == 1
                       and value["operation"] == "restore-bundle" and value["phase"] == "prepared"
                       and value["publication_policy"] == "apply-only-keep-paused", "operation-schema")
    admission._require(value["candidate_name"] == apply.CANDIDATE
                       and value["archive_name"] == database.name + ".recovery-archive-" + admitted["record"]["id"])
    admission._require(value["recovery_id"] == admitted["summary"]["recovery_id"]
                       and value["intent_sha256"] == admitted["summary"]["intent_sha256"])
    identity = value["intent_identity"]
    admission._require(type(identity) is list and len(identity) == 9
                       and all(type(n) is int and 0 <= n <= 2**64 - 1 for n in identity)
                       and identity == list(admission._identity(admitted["intent_info"])))
    artifacts = value["artifacts"]
    _shape(artifacts, admitted["record"]["artifacts"])
    for facts in artifacts.values():
        _facts(facts, extended=False)
    admission._require(artifacts == admitted["record"]["artifacts"])
    admission._require(sum(f["size"] for f in artifacts.values()) <= admission.MAX_EVIDENCE_BYTES)
    identities = {(f["dev"], f["ino"]) for f in artifacts.values()}
    admission._require(len(identities) == len(artifacts), "duplicate-role-inode")
    observed = value["observed"]
    _shape(observed, artifacts)
    for locations in observed.values():
        admission._require(type(locations) is dict and set(locations) in
                           ({"original"}, {"retained"}, {"original", "retained"}), "operation-schema")
        for facts in locations.values():
            _shape(facts, ("mode", "uid", "gid", "links"))
            _integers(facts, facts)
            admission._require(facts["links"] == len(locations) and facts["mode"] <= 0o7777
                               and facts["uid"] == os.geteuid())
        admission._require(all(f == next(iter(locations.values())) for f in locations.values()))
    candidate = value["candidate"]
    _facts(candidate)
    admission._require(candidate["mode"] == 0o600 and candidate["gid"] == os.getegid()
                       and candidate["size"] > 0 and (candidate["dev"], candidate["ino"]) not in identities)
    source = value["source"]
    _shape(source, ("bundle_sha256", "manifest_sha256", "history_sha256", "history_bytes", "schema", "backfill_marker"))
    for key in ("bundle_sha256", "manifest_sha256", "history_sha256"):
        admission._token(source[key], 64)
    _integers(source, ("history_bytes", "backfill_marker"))
    admission._require(source["history_bytes"] == candidate["size"]
                       and source["history_sha256"] == candidate["sha256"] and source["schema"] == "supported"
                       and source["backfill_marker"] == 1)
    admitted["summary"]["artifacts"] = observed


def _receipt_value(admitted, produced):
    return dict(version=1, phase="applied-paused", recovery_id=admitted["summary"]["recovery_id"],
                intent_sha256=admitted["summary"]["intent_sha256"], operation=produced[apply.OPERATION],
                candidate=produced[apply.CANDIDATE], schema="supported", backfill_marker=1, recovery_completed=False)


def _receipt(value, admitted, produced):
    expected = _receipt_value(admitted, produced)
    _shape(value, expected)
    _facts(value["operation"])
    _facts(value["candidate"])
    # Canonical JSON equality also distinguishes bool/int and float/int.
    admission._require(json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True), "receipt-conflict")


def _sync_file(parent, name):
    with ExitStack() as stack:
        fd, _ = admission._open(stack, name, parent)
        apply._step(name + ":resume-file-fsync", lambda: os.fsync(fd))


def resume(database, *, recovery_id, intent_sha256, resolved_topology, offline,
           operation_sha256=None, operation_identity=None, receipt_sha256=None, receipt_identity=None):
    """Authenticate selected v1 journal and converge only to applied-paused.

    Missing/partial records and mismatched external anchors refuse before writes.
    Complete records interrupted before fsync are re-synced, never rewritten.
    No new bundle is consulted, selected, extracted or staged during replay.
    """
    admission._token(recovery_id, 32)
    admission._token(intent_sha256, 64)
    admission._require(offline is True, "offline-consent-required")
    admission._require(resolved_topology == "unsegmented", "resolved-unsegmented-topology-required")
    operation_anchor = _anchor(operation_sha256, operation_identity)
    receipt_anchor = None
    if receipt_sha256 is not None or receipt_identity is not None:
        receipt_anchor = _anchor(receipt_sha256, receipt_identity)
    mutated = False
    locked = False
    try:
        database = admission._database(database)
        admission._require(not _database_path_is_mount_point(database), "database-file-mount-unsupported")
        with _history_lifecycle_lock(database, blocking=False) as parent, ExitStack() as stack:
            locked = True
            root, root_info, _, intent_info, raw, record = admission._record(stack, parent, recovery_path(database).name)
            admission._require(record["id"] == recovery_id and hashlib.sha256(raw).hexdigest() == intent_sha256,
                               "stale-selection")
            admitted = dict(parent=parent, root=root, parent_info=os.fstat(parent), root_info=root_info,
                            intent_info=intent_info, record=record,
                            summary=dict(recovery_id=recovery_id, intent_sha256=intent_sha256))
            operation, operation_facts = _metadata(stack, root, apply.OPERATION, operation_anchor)
            _operation(operation, database, admitted)
            produced = {apply.OPERATION: operation_facts, apply.CANDIDATE: operation["candidate"]}
            parent_names = admission._names(parent, 5)
            root_names = admission._names(root, 8)
            active = (database.name in parent_names and
                      os.stat(database.name, dir_fd=parent, follow_symlinks=False).st_ino == operation["candidate"]["ino"])
            if apply.COMPLETED in root_names:
                admission._require(receipt_anchor is not None, "receipt-selection-required")
                receipt, receipt_facts = _metadata(stack, root, apply.COMPLETED, receipt_anchor)
                _receipt(receipt, admitted, produced)
                admission._require(active and apply.CANDIDATE not in root_names, "receipt-topology-conflict")
                produced[apply.COMPLETED] = receipt_facts
            else:
                admission._require(receipt_anchor is None, "missing-selected-receipt")
            apply._verify(database, admitted, produced, active=active)
            candidate_path = database if active else recovery_path(database) / apply.CANDIDATE
            apply._step("resume:candidate-schema-readback", lambda: admission._validate_candidate(
                candidate_path, produced[apply.CANDIDATE]["sha256"]))
            apply._verify(database, admitted, produced, active=active)
            mutated = True
            # Re-establish durability before replaying any link/unlink. A complete
            # but not-yet-synced journal is admissible only with explicit anchors.
            for name in ("intent.json", apply.OPERATION):
                _sync_file(root, name)
            _sync_file(parent if active else root, database.name if active else apply.CANDIDATE)
            apply._step("resume:prepared-directory-fsync", lambda: os.fsync(root))
            apply._verify(database, admitted, produced, active=active)
            if not active:
                for role, suffix in ARTIFACTS.items():
                    if role not in record["artifacts"]:
                        continue
                    apply._verify(database, admitted, produced)
                    name = database.name + suffix
                    if role not in admission._names(root, 8):
                        apply._step(role + ":retain-link", lambda: os.link(
                            name, role, src_dir_fd=parent, dst_dir_fd=root, follow_symlinks=False))
                    apply._step(role + ":retained-directory-fsync", lambda: os.fsync(root))
                    apply._verify(database, admitted, produced)
                    if name in admission._names(parent, 5):
                        apply._step(role + ":original-unlink", lambda: os.unlink(name, dir_fd=parent))
                    apply._step(role + ":original-directory-fsync", lambda: os.fsync(parent))
                apply._verify(database, admitted, produced)
                apply._step("candidate:publish-link", lambda: os.link(
                    apply.CANDIDATE, database.name, src_dir_fd=root, dst_dir_fd=parent, follow_symlinks=False))
            apply._step("candidate:published-directory-fsync", lambda: os.fsync(parent))
            apply._verify(database, admitted, produced, active=True)
            if apply.CANDIDATE in admission._names(root, 8):
                apply._step("candidate:staging-unlink", lambda: os.unlink(apply.CANDIDATE, dir_fd=root))
            apply._step("candidate:staging-directory-fsync", lambda: os.fsync(root))
            apply._verify(database, admitted, produced, active=True)
            _sync_file(parent, database.name)
            apply._step("active:directory-fsync", lambda: os.fsync(parent))
            apply._step("active:schema-readback", lambda: admission._validate_candidate(
                database, produced[apply.CANDIDATE]["sha256"]))
            apply._verify(database, admitted, produced, active=True)
            if apply.COMPLETED not in produced:
                produced[apply.COMPLETED] = apply._receipt(root, apply.COMPLETED, _receipt_value(admitted, produced))
            else:
                _sync_file(root, apply.COMPLETED)
                apply._step("receipt:directory-fsync", lambda: os.fsync(root))
            apply._step("final:evidence-readback", lambda: apply._verify(database, admitted, produced, active=True))
        return dict(state="applied-paused", recovery_completed=False, resume_available=True,
                    reason="explicit-finalization-required", operation=produced[apply.OPERATION], receipt=produced[apply.COMPLETED])
    except Exception as exc:
        if mutated:
            raise admission.RecoveryRefusal("resume-incomplete-still-paused", 5) from None
        if isinstance(exc, admission.RecoveryRefusal):
            if exc.reason == "invalid-selection-token":
                raise admission.RecoveryRefusal("operation-schema") from None
            raise
        if isinstance(exc, sqlite3.OperationalError) and not locked:
            raise admission.RecoveryRefusal("lifecycle-busy", 3) from None
        raise admission.RecoveryRefusal("resume-evidence-conflict") from None
