"""Store-local full admission followed by filesystem-observable generation checks.

Warm checks do not prove payload integrity against metadata-invisible damage.
No TTL, automatic re-certification, shared cache, or persisted authority is used.
The caller serializes observation and forwarding with ``lock``. Cold observation
also requires lifecycle ownership; warm observations never recursively acquire it.
"""
from contextlib import ExitStack
import os
import stat
import threading

from history_service.recovery_state import MAX_RECORD_BYTES, ARTIFACTS

RECORDS = frozenset(('intent.json', 'recovery-operation.json', 'completed.json',
                     'finalization-gate.json', 'finalized.json'))


def _parent_identity(info):
    # Live SQLite sidecars and unrelated children may change size/times/nlink.
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _topology(database, parent, archive):
    from history_service import explicit_recovery as admission
    names = admission._names(parent, 4096)
    archives = {name for name in names if name.startswith(database.name + '.recovery-archive-')}
    admission._require(archives == ({archive} if archive is not None else set()))
    admission._require(database.name + '.recovery-required' not in names
                       and database.name + '.recovery-finalizing' not in names)


def _inventory(database, parent, archive):
    """Bounded descriptor/path readback; payload descriptors are never read."""
    from history_service import explicit_recovery as admission
    parent_info = _parent_identity(os.fstat(parent))
    admission._require(parent_info == _parent_identity(database.parent.stat()))
    _topology(database, parent, archive)
    result: dict = dict(parent=parent_info, archive=archive, members={})
    if archive is None:
        return result
    with ExitStack() as stack:
        root, info = admission._open(stack, archive, parent, directory=True)
        admission._require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid())
        result['root'] = admission._identity(info)
        names = admission._names(root, 10)
        terminals = {name for name in names if name.startswith('committed-')}
        admission._require(len(terminals) == 1 and RECORDS <= names
                           and names <= RECORDS | set(ARTIFACTS) | terminals)
        opened = []
        for name in sorted(names):
            terminal = name in terminals
            fd, metadata = admission._open(stack, name, root, directory=terminal)
            if terminal:
                admission._require(stat.S_IMODE(metadata.st_mode) == 0o700
                                   and metadata.st_uid == os.geteuid()
                                   and not admission._names(fd, 0))
            else:
                admission._require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1)
            raw = None
            if name in RECORDS:
                from history_service.recovery_apply import MAX_OPERATION_BYTES
                maximum = MAX_RECORD_BYTES if name == 'intent.json' else MAX_OPERATION_BYTES
                admission._require(metadata.st_size <= maximum
                                   and metadata.st_uid == os.geteuid()
                                   and stat.S_IMODE(metadata.st_mode) == 0o600)
                raw = os.read(fd, maximum + 1)
                admission._require(len(raw) == metadata.st_size)
            identity = admission._identity(metadata)
            result['members'][name] = (identity, raw)
            opened.append((name, fd, identity))
        for name, fd, identity in opened:
            admission._require(identity == admission._identity(os.fstat(fd))
                               == admission._identity(os.stat(name, dir_fd=root, follow_symlinks=False)))
        admission._require(names == admission._names(root, 10))
        admission._require(result['root'] == admission._identity(os.fstat(root))
                           == admission._identity(os.stat(archive, dir_fd=parent, follow_symlinks=False)))
    _topology(database, parent, archive)
    admission._require(parent_info == _parent_identity(os.fstat(parent))
                       == _parent_identity(database.parent.stat()))
    return result


class ValidatedGeneration:
    def __init__(self):
        self.lock = threading.RLock()
        self.token = None
        self.pid = os.getpid()
        self.failed = False

    def chmod_parent(self, descriptor, mode):
        """Only the store's schema-admitted, explicit permission repair may do this.

        Update exactly its intended mode, never take replacement evidence as a
        new authority. All archive anchors and every other parent fact survive.
        """
        from history_service import explicit_recovery as admission
        with self.lock:
            before = _parent_identity(os.fstat(descriptor))
            admission._require(not self.failed and self.pid == os.getpid())
            if self.token is not None:
                admission._require(before == self.token['parent'])
            expected = before[:2] + (stat.S_IFDIR | mode,) + before[3:]
            os.fchmod(descriptor, mode)
            admission._require(_parent_identity(os.fstat(descriptor)) == expected)
            if self.token is not None:
                self.token = dict(self.token, parent=expected)

    def check_archive(self, database, parent, archive):
        from history_service import explicit_recovery as admission
        admission._require(self.pid == os.getpid())
        if self.failed and self.token is None:
            return True
        before = _inventory(database, parent, archive)
        if self.token is not None:
            admission._require(before == self.token, 'recovery-generation-changed')
            return True
        if archive is not None:
            from history_service.recovery_finalize import committed_archive
            admission._require(committed_archive(database, parent, archive))
        after = _inventory(database, parent, archive)
        admission._require(before == after, 'recovery-generation-changed')
        self.token = after
        return True
