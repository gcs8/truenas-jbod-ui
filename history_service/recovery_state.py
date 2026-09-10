"""Forward-only pause journal. Observation never acknowledges or automatically recovers.

All mutation requires the caller's history lifecycle lock. Like admission, this
is a cooperative filesystem boundary, not protection against hostile rename/ABA
or in-place writers. A directory reservation is itself a fail-closed intent;
incomplete metadata never grants permission to initialize or clear anything.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from contextlib import ExitStack

MAX_RECORD_BYTES = 8192
ARTIFACTS = {"main": "", "wal": "-wal", "shm": "-shm", "journal": "-journal"}


class HistoryRecoveryRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("History recovery required; collection and mutation are paused.")


def recovery_path(database: Path) -> Path:
    return database.with_name(database.name + ".recovery-required")


def finalization_path(database: Path) -> Path:
    return database.with_name(database.name + ".recovery-finalizing")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate recovery field")
        result[key] = value
    return result


def inspect_recovery(database: Path) -> str:
    """Bounded no-SQLite observation, including terminal archive validation."""
    try:
        finalization_path(database).lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return "unavailable"
    else:
        # Any reservation blocks startup, including partial, unsafe or malformed
        # metadata. Only explicit verified finalization archives this gate.
        return "required"
    root = recovery_path(database)
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return _inspect_archives(database)
    except OSError:
        return "unavailable"
    try:
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()):
            return "invalid"
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            actual = os.fstat(directory)
            if (actual.st_dev, actual.st_ino) != (metadata.st_dev, metadata.st_ino):
                return "invalid"
            descriptor = os.open("intent.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()
                    or info.st_size > MAX_RECORD_BYTES):
                return "invalid"
            raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
            if len(raw) != info.st_size:
                return "invalid"
        record = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(record, dict) or set(record) != {"version", "id", "policy", "phase", "artifacts"}:
            return "invalid"
        if (type(record["version"]) is not int or record["version"] != 1
                or record["policy"] != "pause" or record["phase"] != "intent"
                or not isinstance(record["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["id"])):
            return "invalid"
        artifacts = record["artifacts"]
        if not isinstance(artifacts, dict) or "main" not in artifacts or not set(artifacts) <= ARTIFACTS.keys():
            return "invalid"
        for facts in artifacts.values():
            if not isinstance(facts, dict) or set(facts) != {"dev", "ino", "size", "mtime_ns", "sha256"}:
                return "invalid"
            if any(type(facts[key]) is not int or facts[key] < 0 for key in ("dev", "ino", "size", "mtime_ns")):
                return "invalid"
            if not isinstance(facts["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", facts["sha256"]):
                return "invalid"
        return "required"
    except (OSError, ValueError, TypeError, RecursionError):
        return "invalid"


def _inspect_archives(database: Path) -> str:
    # Gate removal alone is never completion. A retained archive without an
    # authenticated terminal decision is a durable startup refusal reservation.
    try:
        with ExitStack() as stack:
            # Match lifecycle-lock canonical-parent alias handling. Do not follow
            # symlinks for archive or receipt entries beneath that parent.
            parent = os.open(database.parent.resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, parent)
            archives = []
            with os.scandir(parent) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > 4096:
                        return "unavailable"
                    if entry.name.startswith(database.name + '.recovery-archive-'):
                        archives.append(entry.name)
                        if len(archives) > 1:
                            return "invalid"
            if not archives:
                return "none"
            from history_service.recovery_finalize import committed_archive

            return "none" if committed_archive(database, parent, archives[0]) else "required"
    except FileNotFoundError:
        # A missing parent is a normal first-run case; missing archive evidence
        # after parent admission must fail closed instead.
        return "invalid" if 'parent' in locals() else "none"
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return "invalid"


def require_recovery_clear(database: Path) -> None:
    """Refuse normal publishers on any reservation, including incomplete evidence.

    Call before work and again under lifecycle ownership. This is only
    normal-operation admission, never permission to recover or clear evidence;
    terminal archive observation validates retained evidence.
    """
    if inspect_recovery(database) != "none":
        raise HistoryRecoveryRequired()


def pause_and_quarantine(database: Path) -> None:
    """Reserve + sync intent before any unlink. Never overwrite or retry moves.

    Hard-link publication preserves the original inode and is non-overwriting.
    A crash between link and unlink leaves two names, both deliberately retained.
    Every failure retains the reservation and all remaining source evidence.
    """
    root = recovery_path(database)
    root.mkdir(mode=0o700)  # Exclusive; an existing record is never replaced.
    _sync_directory(database.parent)
    with ExitStack() as stack:
        sources = {}
        facts = {}
        for role, suffix in ARTIFACTS.items():
            path = Path(str(database) + suffix)
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                if role == "main":
                    raise HistoryRecoveryRequired() from None
                continue
            stack.callback(os.close, descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise HistoryRecoveryRequired()
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise HistoryRecoveryRequired()
            os.fsync(descriptor)
            sources[role] = (path, identity)
            facts[role] = dict(dev=metadata.st_dev, ino=metadata.st_ino, size=metadata.st_size,
                               mtime_ns=metadata.st_mtime_ns, sha256=digest.hexdigest())
        record = dict(version=1, id=uuid.uuid4().hex, policy="pause", phase="intent", artifacts=facts)
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(raw) > MAX_RECORD_BYTES:
            raise HistoryRecoveryRequired()
        descriptor = os.open(root / "intent.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                if output.write(raw) != len(raw):
                    raise OSError("Incomplete recovery intent")
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(root)
        for role, (path, identity) in sources.items():
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity or current.st_nlink != 1:
                raise HistoryRecoveryRequired()
            os.link(path, root / role, follow_symlinks=False)
            _sync_directory(root)
            current = path.lstat()
            published = (root / role).lstat()
            if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity
                    or (published.st_dev, published.st_ino) != identity[:2] or current.st_nlink != 2):
                raise HistoryRecoveryRequired()
            path.unlink()
            _sync_directory(database.parent)
