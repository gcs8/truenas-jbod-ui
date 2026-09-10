"""Explicit offline finalization; immutable evidence, no automatic live unlatch.

The parent gate survives archive publication; the archive remains a startup gate
until an atomic terminal decision follows every archive/main verification.
Existing later records require trusted external digest AND inode selections.
"""
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
import re
import stat
import sqlite3
import sys

from history_service import explicit_recovery as admission
from history_service import recovery_apply as apply
from history_service import recovery_resume as replay
from history_service.migration_lock import _history_lifecycle_lock, _database_path_is_mount_point
from history_service.recovery_state import recovery_path, finalization_path

GATE = 'finalization-gate.json'
FINAL = 'finalized.json'
TERMINAL = 'committed-'


def _terminal_name(facts):
    return f"{TERMINAL}{facts['sha256']}-{facts['dev']}-{facts['ino']}"


def _terminal_check(root, name):
    with ExitStack() as stack:
        fd, info = admission._open(stack, name, root, directory=True)
        admission._require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid()
                           and not admission._names(fd, 0), 'invalid-terminal-commit')


def committed_archive(database, parent, archive):
    """Read-only startup predicate, rooted in the explicit atomic commit anchor.

    The current-owner private terminal name stores the selected completion digest
    and inode. It is a local protocol receipt, NOT external provenance discovery.
    Validate only immutable archive evidence; normal writes may change active DB.
    """
    with ExitStack() as stack:
        root, root_info, _, intent_info, raw, record = admission._record(stack, parent, archive)
        admission._require(archive == database.name + '.recovery-archive-' + record['id'])
        names = admission._names(root, 10)
        terminals = [n for n in names if n.startswith(TERMINAL)]
        admission._require(len(terminals) == 1, 'missing-terminal-commit')
        terminal = terminals[0]
        match = re.fullmatch(r'committed-([0-9a-f]{64})-([0-9]{1,20})-([0-9]{1,20})', terminal)
        admission._require(match is not None, 'invalid-terminal-commit')
        anchor = replay._anchor(match[1], match[2] + ':' + match[3])
        _terminal_check(root, terminal)
        final, final_facts = replay._metadata(stack, root, FINAL, anchor)
        admission._require(terminal == _terminal_name(final_facts))
        replay._shape(final, ('version', 'phase', 'recovery_id', 'intent_sha256', 'operation',
                             'receipt', 'gate', 'candidate', 'root_identity', 'schema',
                             'backfill_marker', 'recovery_completed'))
        values = {}
        facts = {}
        for name, key in ((apply.OPERATION, 'operation'), (apply.COMPLETED, 'receipt'), (GATE, 'gate')):
            selected = final[key]
            replay._facts(selected)
            values[key], facts[key] = replay._metadata(
                stack, root, name, (selected['sha256'], (selected['dev'], selected['ino'])))
            _equal(facts[key], selected)
        intent_digest = hashlib.sha256(raw).hexdigest()
        admitted = dict(record=record, intent_info=intent_info,
                        summary=dict(recovery_id=record['id'], intent_sha256=intent_digest))
        operation = values['operation']
        replay._operation(operation, database, admitted)
        replay._receipt(values['receipt'], admitted,
                        {apply.OPERATION: facts['operation'], apply.CANDIDATE: operation['candidate']})
        common = dict(version=1, recovery_id=record['id'], intent_sha256=intent_digest,
                      operation=facts['operation'], receipt=facts['receipt'],
                      candidate=operation['candidate'], root_identity=list(apply._directory_identity(root_info)))
        _equal(values['gate'], dict(common, phase='finalizing', archive_name=archive))
        _equal(final, dict(common, phase='verified-archive', gate=facts['gate'],
                           schema='supported', backfill_marker=1, recovery_completed=True))
        admission._require(names == {'intent.json', apply.OPERATION, apply.COMPLETED, GATE, FINAL, terminal}
                           | set(record['artifacts']), 'unknown-recovery-artifact')
        for role, expected in record['artifacts'].items():
            observed = next(iter(operation['observed'][role].values()))
            apply._file(root, role, dict(expected, **{k: observed[k] for k in ('mode', 'uid', 'gid')}))
        admission._require(apply._directory_identity(root_info) == apply._directory_identity(
            os.stat(archive, dir_fd=parent, follow_symlinks=False)))
    return True


def _rename_noreplace(source_parent, source, target_parent, target):
    admission._require(sys.platform == 'linux', 'linux-noreplace-required')
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_parent, os.fsencode(source), target_parent, os.fsencode(target), 1):
        error = ctypes.get_errno()
        raise OSError(error, 'No-replace archive publication refused')


def _equal(actual, expected):
    admission._require(json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True),
                       'finalization-record-conflict')


def finalize(database, *, recovery_id, intent_sha256, resolved_topology, offline,
             operation_sha256=None, operation_identity=None, receipt_sha256=None, receipt_identity=None,
             gate_sha256=None, gate_identity=None, completion_sha256=None, completion_identity=None):
    """Finalize selected applied-paused state or replay selected archived state.

    An existing gate/completion is never self-selected or rewritten. Partial
    metadata remains a preservation/refusal case. Call only before writers restart.
    """
    admission._token(recovery_id, 32)
    admission._token(intent_sha256, 64)
    admission._require(offline is True, 'offline-consent-required')
    admission._require(resolved_topology == 'unsegmented', 'resolved-unsegmented-topology-required')
    op_anchor = replay._anchor(operation_sha256, operation_identity)
    receipt_anchor = replay._anchor(receipt_sha256, receipt_identity)
    gate_anchor = replay._anchor(gate_sha256, gate_identity) if gate_sha256 is not None or gate_identity is not None else None
    final_anchor = replay._anchor(completion_sha256, completion_identity) if completion_sha256 is not None or completion_identity is not None else None
    mutated = locked = commit_attempted = already_committed = False
    try:
        database = admission._database(database)
        admission._require(not _database_path_is_mount_point(database), 'database-file-mount-unsupported')
        admission._require(sys.platform == 'linux' and hasattr(ctypes.CDLL(None), 'renameat2'), 'linux-noreplace-required')
        pending = recovery_path(database).name
        archive = database.name + '.recovery-archive-' + recovery_id
        gate_name = finalization_path(database).name
        with _history_lifecycle_lock(database, blocking=False) as parent, ExitStack() as stack:
            locked = True
            names = admission._names(parent, 4)
            admission._require((pending in names) != (archive in names), 'archive-topology-conflict')
            archived = archive in names
            root_name = archive if archived else pending
            root, root_info, _, intent_info, raw, record = admission._record(stack, parent, root_name)
            admission._require(record['id'] == recovery_id and hashlib.sha256(raw).hexdigest() == intent_sha256,
                               'stale-selection')
            admitted = dict(parent=parent, root=root, parent_info=os.fstat(parent), root_info=root_info,
                            intent_info=intent_info, record=record,
                            summary=dict(recovery_id=recovery_id, intent_sha256=intent_sha256))
            operation, op_facts = replay._metadata(stack, root, apply.OPERATION, op_anchor)
            replay._operation(operation, database, admitted)
            produced = {apply.OPERATION: op_facts, apply.CANDIDATE: operation['candidate']}
            receipt, receipt_facts = replay._metadata(stack, root, apply.COMPLETED, receipt_anchor)
            replay._receipt(receipt, admitted, produced)
            produced[apply.COMPLETED] = receipt_facts
            root_names = admission._names(root, 10)
            released = GATE in root_names
            admission._require(not released or (archived and gate_name not in names), 'gate-topology-conflict')
            gate_value = dict(version=1, phase='finalizing', recovery_id=recovery_id,
                              intent_sha256=intent_sha256, operation=op_facts, receipt=receipt_facts,
                              root_identity=list(apply._directory_identity(root_info)),
                              candidate=operation['candidate'], archive_name=archive)
            gate_facts = final_facts = None
            if released or gate_name in names:
                admission._require(gate_anchor is not None, 'gate-selection-required')
                value, gate_facts = replay._metadata(stack, root if released else parent,
                                                     GATE if released else gate_name, gate_anchor)
                _equal(value, gate_value)
            else:
                admission._require(not archived and gate_anchor is None, 'missing-selected-gate')
            final_value = dict(version=1, phase='verified-archive', recovery_id=recovery_id,
                               intent_sha256=intent_sha256, operation=op_facts, receipt=receipt_facts,
                               gate=gate_facts, candidate=operation['candidate'],
                               root_identity=list(apply._directory_identity(root_info)),
                               schema='supported', backfill_marker=1, recovery_completed=True)
            if FINAL in root_names:
                admission._require(archived and final_anchor is not None and gate_facts is not None,
                                   'completion-selection-required')
                value, final_facts = replay._metadata(stack, root, FINAL, final_anchor)
                _equal(value, final_value)
            else:
                admission._require(final_anchor is None and not released, 'missing-selected-completion')

            terminal_names = {n for n in root_names if n.startswith(TERMINAL)}
            terminal_name = _terminal_name(final_facts) if final_facts else None
            if terminal_names:
                admission._require(released and terminal_names == {terminal_name}, 'terminal-conflict')
                _terminal_check(root, terminal_name)
                already_committed = committed_archive(database, parent, archive)

            def verify():
                # Reopen the exact root independently at every readback. Never
                # SQLite-open any original evidence. Directory mtime is mutable.
                with ExitStack() as check:
                    check_root, info = admission._open(check, root_name, parent, directory=True)
                    admission._require(apply._directory_identity(info) == apply._directory_identity(root_info))
                    admission._require(apply._directory_identity(os.stat(database.parent)) ==
                                       apply._directory_identity(admitted['parent_info']))
                    allowed_parent = {database.name, root_name} | ({gate_name} if gate_facts and not released else set())
                    admission._require(admission._names(parent, 4) == allowed_parent, 'unknown-topology')
                    allowed_root = {'intent.json', apply.OPERATION, apply.COMPLETED} | set(record['artifacts'])
                    allowed_root |= ({FINAL} if final_facts else set()) | ({GATE} if released else set())
                    allowed_root |= terminal_names
                    admission._require(admission._names(check_root, 10) == allowed_root, 'unknown-recovery-artifact')
                    for name in terminal_names:
                        _terminal_check(check_root, name)
                    apply._file(check_root, 'intent.json', apply._facts(intent_info, intent_sha256))
                    for name in (apply.OPERATION, apply.COMPLETED):
                        apply._file(check_root, name, produced[name])
                    apply._file(parent, database.name, operation['candidate'])
                    for role, facts in record['artifacts'].items():
                        observed = next(iter(operation['observed'][role].values()))
                        apply._file(check_root, role, dict(facts, **{k: observed[k] for k in ('mode', 'uid', 'gid')}))
                    if gate_facts:
                        apply._file(check_root if released else parent, GATE if released else gate_name, gate_facts)
                    if final_facts:
                        apply._file(check_root, FINAL, final_facts)

            def readback():
                apply._step('finalize:evidence-readback', verify)
                apply._step('finalize:schema-readback', lambda: admission._validate_candidate(
                    database, operation['candidate']['sha256']))
                apply._step('finalize:post-schema-readback', verify)

            readback()
            mutated = True
            if gate_facts is None:
                gate_facts = apply._receipt(parent, gate_name, gate_value)
                final_value['gate'] = gate_facts
            apply._step('finalize:gate-readback', verify)
            # Re-establish all selected durability before either rename.
            for name in ('intent.json', apply.OPERATION, apply.COMPLETED, *record['artifacts']):
                replay._sync_file(root, name)
            replay._sync_file(parent, database.name)
            replay._sync_file(root if released else parent, GATE if released else gate_name)
            apply._step('finalize:root-fsync', lambda: os.fsync(root))
            apply._step('finalize:parent-fsync', lambda: os.fsync(parent))
            readback()
            if not archived:
                apply._step('finalize:archive-rename', lambda: _rename_noreplace(parent, pending, parent, archive))
                root_name = archive
                archived = True
            apply._step('finalize:archive-parent-fsync', lambda: os.fsync(parent))
            readback()
            if final_facts is None:
                final_facts = apply._receipt(root, FINAL, final_value)
            else:
                replay._sync_file(root, FINAL)
                apply._step('finalize:completion-directory-fsync', lambda: os.fsync(root))
            readback()
            if not released:
                # Preserve the parent gate in the archive. The archive itself
                # still refuses startup until the subsequent terminal decision.
                apply._step('finalize:gate-archive-rename', lambda: _rename_noreplace(parent, gate_name, root, GATE))
                released = True
            apply._step('finalize:gate-archive-fsync', lambda: os.fsync(root))
            apply._step('finalize:clear-parent-fsync', lambda: os.fsync(parent))
            readback()
            if not already_committed:
                # Atomic terminal decision, only after every archive/main check.
                # The name durably binds the selected receipt without another
                # partially written JSON record. Never undo this decision.
                terminal_name = _terminal_name(final_facts)
                def commit():
                    nonlocal commit_attempted
                    commit_attempted = True
                    os.mkdir(terminal_name, mode=0o700, dir_fd=root)
                apply._step('finalize:terminal-commit', commit)
            apply._step('finalize:terminal-readback', lambda: committed_archive(database, parent, archive))
            with ExitStack() as terminal_stack:
                terminal_fd, _ = admission._open(terminal_stack, terminal_name, root, directory=True)
                apply._step('finalize:terminal-directory-fsync', lambda: os.fsync(terminal_fd))
            # A failed barrier may have persisted. It cannot truthfully be called
            # incomplete. Startup evaluates the terminal receipt independently.
            apply._step('finalize:terminal-fsync', lambda: os.fsync(root))
        return dict(state='completed', recovery_completed=True, restart_required=True,
                    operation=op_facts, receipt=receipt_facts, gate=gate_facts, completion=final_facts)
    except Exception as exc:
        if already_committed:
            raise admission.RecoveryRefusal('finalization-committed-replay-failed', 5) from None
        if commit_attempted:
            raise admission.RecoveryRefusal('finalization-commit-outcome-unknown', 5) from None
        if mutated:
            raise admission.RecoveryRefusal('finalization-incomplete-inspect-and-replay', 5) from None
        if isinstance(exc, admission.RecoveryRefusal):
            raise
        if isinstance(exc, sqlite3.OperationalError) and not locked:
            raise admission.RecoveryRefusal('lifecycle-busy', 3) from None
        raise admission.RecoveryRefusal('finalization-evidence-conflict') from None
