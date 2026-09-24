"""Change journal and change-driven config-only backups (#573).

Every configuration mutation (settings edit, system add/remove, mapping,
profile, rename) appends one entry to a small append-only journal. A
:class:`ConfigBackupCoalescer` waits for a quiet period after the last change
(capped by a maximum delay so a constant stream of edits still gets backed up),
hashes the canonical config set, and only when that hash differs from the last
config backup calls the injected ``make_backup(change_ids)`` callback. The
entries are then marked committed to that backup id, or committed as ``noop``
when the content hash did not change (an edit and its revert, or a re-save that
only moved a timestamp). A burst of twenty edits makes one backup, never twenty
identical copies.

Nothing here is wired into the UI or admin routes yet; the library is inert until
the integration PR constructs it.

Storage format: JSON lines, not SQLite. The journal is tiny, written a few times
per operator action, and read whole; JSON lines keeps it inspectable with a text
editor, lets the config backup ship it verbatim, and has a simple crash story:
each record is one ``write`` of one ``\\n``-terminated line followed by
``fsync``, so the only possible damage from a crash is an unterminated last line,
which readers ignore and the next writer truncates away. Commit markers are
appended as their own records instead of rewriting change records in place, so
normal operation never rewrites the file. Compaction (only when the file passes
``max_bytes``) writes a temporary file, fsyncs it, ``os.replace``s it over the
journal, and fsyncs the directory; it never drops an uncommitted change.

Ordering contract for callers: append the journal entry *after* the mutation is
durably on disk. The coalescer reads the pending entries before it snapshots the
config, so every entry it commits is contained in the snapshot it hashed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

try:
    import fcntl
except ImportError as exc:  # pragma: no cover - the backup sidecar runs on Linux
    raise RuntimeError("The config change journal requires POSIX flock support.") from exc

logger = logging.getLogger(__name__)

JOURNAL_FORMAT_VERSION = 1
DEFAULT_QUIET_PERIOD_SECONDS = 30.0
DEFAULT_MAX_DELAY_SECONDS = 600.0
DEFAULT_MAX_JOURNAL_BYTES = 1024 * 1024
DEFAULT_RETAIN_COMMITTED_ENTRIES = 256
MAX_LINE_BYTES = 64 * 1024
MAX_SUBJECT_CHARS = 256
# Change ids per commit record: 256 ids of <= 68 bytes stay far below MAX_LINE_BYTES.
COMMIT_IDS_PER_RECORD = 256
MAX_RETRY_EXPONENT = 16

# Dict keys whose values change on every save without changing what the operator
# configured. Matched by exact key name at any depth of a config document.
DEFAULT_VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "checked_at",
        "collected_at",
        "exported_at",
        "generated_at",
        "last_seen",
        "last_seen_at",
        "observed_at",
        "refreshed_at",
        "saved_at",
        "updated_at",
    }
)

_ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CHANGE_ID_PATTERN = re.compile(r"^chg_[0-9a-z_]{8,64}$")
# Artifact ids are opaque and server-issued (catalog); bound them so a commit
# record always fits the parser's line limit.
_BACKUP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

CommitOutcome = Literal["backup", "noop"]


class JournalError(RuntimeError):
    """The journal file is unusable (wrong type, symlink, unreadable)."""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    change_id: str
    at: datetime
    action: str
    subject: str
    before_hash: str | None
    after_hash: str | None
    status: Literal["pending", "backup", "noop"] = "pending"
    backup_id: str | None = None
    recovered: bool = False
    merged: int = 1  # >1 only for a journal.overflow entry standing in for several changes

    def as_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "at": _format_time(self.at),
            "action": self.action,
            "subject": self.subject,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "status": self.status,
            "backup_id": self.backup_id,
            "recovered": self.recovered,
            "merged": self.merged,
        }


@dataclass(slots=True)
class _JournalState:
    changes: dict[str, JournalEntry] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    commits: dict[str, tuple[CommitOutcome, str | None]] = field(default_factory=dict)
    commit_records: list[dict[str, Any]] = field(default_factory=list)
    last_backup_hash: str | None = None
    last_backup_id: str | None = None
    corrupt_lines: int = 0
    torn_tail_bytes: int = 0


@dataclass(frozen=True, slots=True)
class JournalStats:
    path: str
    size_bytes: int
    pending: int
    committed: int
    corrupt_lines: int
    torn_tail_bytes: int
    last_backup_hash: str | None
    last_backup_id: str | None
    over_budget: bool


def content_sha256(content: bytes | str | None) -> str | None:
    """Hex SHA-256 of a file's bytes, for ``before_hash``/``after_hash``."""

    if content is None:
        return None
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def canonicalize_config(value: Any, volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS) -> Any:
    """Return ``value`` with volatile keys dropped and mappings key-sorted.

    List order is kept: in config documents it is usually meaningful (system
    order, slot order). Sets are sorted because they have no order to keep.
    """

    if isinstance(value, Mapping):
        return {
            str(key): canonicalize_config(child, volatile_keys)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in volatile_keys
        }
    if isinstance(value, (list, tuple)):
        return [canonicalize_config(child, volatile_keys) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonicalize_config(child, volatile_keys) for child in value), key=_stable_json)
    if isinstance(value, datetime):
        return _format_time(value) if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_config_hash(
    config_set: Mapping[str, Any],
    volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
) -> str:
    """Stable SHA-256 of a config set: ``{logical name: parsed document}``.

    A missing document should be passed as ``None`` so that deleting a file is a
    change; the name itself is part of the hash.
    """

    return hashlib.sha256(_stable_json(canonicalize_config(dict(config_set), volatile_keys)).encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("journal timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _validate_hash(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase hex SHA-256 or None")
    return value


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:  # pragma: no cover - directory fsync is best effort off Linux
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ChangeJournal:
    """Append-only, fsync'd JSON-lines journal of configuration mutations.

    Safe to share between processes (the UI and the admin sidecar both mutate
    config): every read, append, and compaction holds an exclusive ``flock`` on a
    sibling ``.lock`` file, and appends reopen the journal by path so they never
    write into a file that compaction has already replaced.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
        retain_committed_entries: int = DEFAULT_RETAIN_COMMITTED_ENTRIES,
        utcnow: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = Path(path)
        if max_bytes < 4096:
            raise ValueError("max_bytes must be at least 4096")
        if retain_committed_entries < 0:
            raise ValueError("retain_committed_entries must not be negative")
        self.max_bytes = int(max_bytes)
        self.retain_committed_entries = int(retain_committed_entries)
        self._utcnow = utcnow
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._thread_lock = threading.RLock()
        self._ensure_directory()

    # -- public API -----------------------------------------------------------------

    def append(
        self,
        action: str,
        subject: str = "",
        *,
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> JournalEntry:
        """Durably record one mutation; returns the entry once it is fsync'd."""

        if not isinstance(action, str) or not _ACTION_PATTERN.fullmatch(action):
            raise ValueError("action must look like 'mapping.save' (lowercase, <= 64 chars)")
        if not isinstance(subject, str) or len(subject) > MAX_SUBJECT_CHARS:
            raise ValueError(f"subject must be a string of at most {MAX_SUBJECT_CHARS} characters")
        entry = JournalEntry(
            change_id="chg_" + uuid.uuid4().hex,
            at=self._utcnow(),
            action=action,
            subject=subject,
            before_hash=_validate_hash(before_hash, "before_hash"),
            after_hash=_validate_hash(after_hash, "after_hash"),
        )
        record = {
            "v": JOURNAL_FORMAT_VERSION,
            "type": "change",
            "change_id": entry.change_id,
            "at": _format_time(entry.at),
            "action": entry.action,
            "subject": entry.subject,
            "before": entry.before_hash,
            "after": entry.after_hash,
        }
        with self._locked():
            self._append_records_locked([record])
            if self._size_locked() > self.max_bytes:
                self._compact_locked()
        return entry

    def pending(self) -> list[JournalEntry]:
        """Uncommitted entries, oldest first (includes recovered placeholders)."""

        with self._locked():
            state = self._load_locked()
        return [state.changes[cid] for cid in state.order if state.changes[cid].status == "pending"]

    def entries(self, limit: int | None = None) -> list[JournalEntry]:
        """All retained entries, newest first."""

        with self._locked():
            state = self._load_locked()
        ordered = [state.changes[cid] for cid in reversed(state.order)]
        return ordered if limit is None else ordered[: max(0, int(limit))]

    def last_backup(self) -> tuple[str | None, str | None]:
        """``(config_hash, backup_id)`` of the most recent committed config backup."""

        with self._locked():
            state = self._load_locked()
        return state.last_backup_hash, state.last_backup_id

    def commit(
        self,
        change_ids: tuple[str, ...] | list[str],
        *,
        outcome: CommitOutcome,
        config_hash: str,
        backup_id: str | None = None,
    ) -> None:
        """Mark ``change_ids`` as captured by ``backup_id`` (or as a no-op)."""

        if outcome not in ("backup", "noop"):
            raise ValueError("outcome must be 'backup' or 'noop'")
        if outcome == "backup" and (not isinstance(backup_id, str) or not _BACKUP_ID_PATTERN.fullmatch(backup_id)):
            raise ValueError("a backup commit needs a backup_id of at most 128 safe characters")
        if outcome == "noop" and backup_id is not None:
            raise ValueError("a noop commit has no backup_id")
        _validate_hash(config_hash, "config_hash")
        ids = [str(cid) for cid in change_ids]
        at = _format_time(self._utcnow())
        # Large runs are split so no line reaches the parser's MAX_LINE_BYTES; all
        # chunks go out in one write and one fsync.
        records = [
            {
                "v": JOURNAL_FORMAT_VERSION,
                "type": "commit",
                "at": at,
                "change_ids": ids[start : start + COMMIT_IDS_PER_RECORD],
                "outcome": outcome,
                "backup_id": backup_id,
                "config_hash": config_hash,
            }
            for start in range(0, max(len(ids), 1), COMMIT_IDS_PER_RECORD)
        ]
        with self._locked():
            self._append_records_locked(records)
            if self._size_locked() > self.max_bytes:
                self._compact_locked()

    def compact(self) -> None:
        """Rewrite the journal without committed history beyond the retention."""

        with self._locked():
            self._compact_locked()

    def stats(self) -> JournalStats:
        with self._locked():
            state = self._load_locked()
            size = self._size_locked()
        pending = sum(1 for entry in state.changes.values() if entry.status == "pending")
        return JournalStats(
            path=str(self.path),
            size_bytes=size,
            pending=pending,
            committed=len(state.changes) - pending,
            corrupt_lines=state.corrupt_lines,
            torn_tail_bytes=state.torn_tail_bytes,
            last_backup_hash=state.last_backup_hash,
            last_backup_id=state.last_backup_id,
            over_budget=size > self.max_bytes,
        )

    def fingerprint(self) -> tuple[int, int, int] | None:
        """Cheap change detector for pollers: ``(inode, size, mtime_ns)``."""

        try:
            info = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return (info.st_ino, info.st_size, info.st_mtime_ns)

    # -- locking and file access -----------------------------------------------------

    def _ensure_directory(self) -> None:
        parent = self.path.parent
        missing: list[Path] = []
        probe = parent
        while not os.path.lexists(probe) and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                continue
            # A new directory entry is durable only once its parent is fsync'd.
            _fsync_directory(directory.parent)
        if not parent.is_dir():
            raise JournalError("journal directory is not a directory")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _open_journal(self, flags: int) -> int:
        try:
            fd = os.open(self.path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise JournalError("journal path is a symlink; refusing to follow it") from exc
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise JournalError("journal path is not a regular file")
        return fd

    def _size_locked(self) -> int:
        try:
            return os.stat(self.path, follow_symlinks=False).st_size
        except FileNotFoundError:
            return 0

    def _read_bytes_locked(self) -> bytes:
        try:
            fd = self._open_journal(os.O_RDONLY)
        except FileNotFoundError:
            return b""
        with os.fdopen(fd, "rb") as handle:
            return handle.read()

    def _append_records_locked(self, records: list[dict[str, Any]]) -> None:
        lines = [_stable_json(record).encode("utf-8") for record in records]
        if any(len(line) > MAX_LINE_BYTES for line in lines):
            # Never write a record the parser would later reject as corruption.
            raise ValueError("journal record exceeds the line limit")
        payload = b"".join(line + b"\n" for line in lines)
        created = not os.path.lexists(self.path)
        fd = self._open_journal(os.O_RDWR | os.O_CREAT | os.O_APPEND)
        try:
            if self._has_torn_tail(fd):
                # Terminate the torn line in the same write instead of truncating
                # it: the damaged line stays on disk as evidence and parses as a
                # pending ``journal.recovered`` change with the same id readers
                # already showed for it, so no crash point loses that change.
                logger.warning("Config change journal: terminating a torn last line")
                payload = b"\n" + payload
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_directory(self.path.parent)

    @staticmethod
    def _has_torn_tail(fd: int) -> bool:
        size = os.fstat(fd).st_size
        return size > 0 and os.pread(fd, 1, size - 1) != b"\n"

    # -- parsing ----------------------------------------------------------------------

    def _load_locked(self) -> _JournalState:
        return self._parse(self._read_bytes_locked())

    def _parse(self, data: bytes) -> _JournalState:
        state = _JournalState()
        lines = data.split(b"\n")
        tail = lines.pop()  # b"" when the file ends with a newline
        for number, raw in enumerate(lines, start=1):
            if not raw.strip():
                continue
            record = None
            if len(raw) <= MAX_LINE_BYTES:
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    record = None
            if not isinstance(record, dict) or not self._apply_record(state, record):
                self._add_recovered_placeholder(state, number, raw)
        if tail:
            # A record missing only its newline is complete and counts; anything
            # else is a torn write that keeps a backup pending.
            record = None
            if len(tail) <= MAX_LINE_BYTES:
                try:
                    record = json.loads(tail)
                except (ValueError, UnicodeDecodeError):
                    record = None
            if isinstance(record, dict) and self._apply_record(state, record):
                state.torn_tail_bytes = len(tail)
            else:
                self._add_recovered_placeholder(state, len(lines) + 1, tail, torn=True)
        for change_id, (outcome, backup_id) in state.commits.items():
            entry = state.changes.get(change_id)
            if entry is not None:
                state.changes[change_id] = _replace_status(entry, outcome, backup_id)
        return state

    def _apply_record(self, state: _JournalState, record: dict[str, Any]) -> bool:
        kind = record.get("type")
        try:
            if kind == "change":
                change_id = record["change_id"]
                if not isinstance(change_id, str) or not _CHANGE_ID_PATTERN.fullmatch(change_id):
                    return False
                action = record["action"]
                subject = record.get("subject", "")
                if not isinstance(action, str) or not isinstance(subject, str):
                    return False
                entry = JournalEntry(
                    change_id=change_id,
                    at=_parse_time(record["at"]),
                    action=action,
                    subject=subject,
                    before_hash=_validate_hash(record.get("before"), "before"),
                    after_hash=_validate_hash(record.get("after"), "after"),
                    recovered=bool(record.get("recovered", False)),
                    merged=_positive_int(record.get("merged", 1)),
                )
                if change_id not in state.changes:
                    state.order.append(change_id)
                state.changes[change_id] = entry
                return True
            if kind == "commit":
                outcome = record["outcome"]
                ids = record["change_ids"]
                config_hash = _validate_hash(record["config_hash"], "config_hash")
                backup_id = record.get("backup_id")
                if outcome not in ("backup", "noop") or not isinstance(ids, list):
                    return False
                if outcome == "backup" and (not isinstance(backup_id, str) or not _BACKUP_ID_PATTERN.fullmatch(backup_id)):
                    return False
                for change_id in ids:
                    if isinstance(change_id, str):
                        state.commits[change_id] = (outcome, backup_id if outcome == "backup" else None)
                state.commit_records.append(record)
                if outcome == "backup":
                    state.last_backup_hash = config_hash
                    state.last_backup_id = backup_id
                return True
            if kind == "checkpoint":
                state.last_backup_hash = _validate_hash(record.get("last_backup_hash"), "last_backup_hash")
                backup_id = record.get("last_backup_id")
                state.last_backup_id = backup_id if isinstance(backup_id, str) else None
                return True
        except (KeyError, TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _add_recovered_placeholder(state: _JournalState, number: int, raw: bytes, *, torn: bool = False) -> None:
        """A damaged line may have been a change: keep a backup pending.

        The placeholder id is derived from the line's position and bytes, so it is
        stable across reloads and a commit record can close it; compaction then
        drops the damaged line with the other committed history.
        """

        if torn:
            state.torn_tail_bytes = len(raw)
        else:
            state.corrupt_lines += 1
        entry = _recovered_entry(number, raw, torn=torn)
        if entry.change_id in state.changes:
            return
        state.order.append(entry.change_id)
        state.changes[entry.change_id] = entry

    # -- compaction ---------------------------------------------------------------------

    def _committed_within_budget(self, state: _JournalState, committed: list[str]) -> list[str]:
        """Newest committed entries, capped by count and by a quarter of ``max_bytes``.

        Pending entries may use up to half the budget (see the overflow merge), so
        after compaction the file sits well under ``max_bytes`` and is not
        rewritten on every following append.
        """

        if not self.retain_committed_entries:
            return []
        record_of: dict[str, int] = {}
        for index, commit in enumerate(state.commit_records):
            for cid in commit.get("change_ids", []):
                record_of[cid] = index
        budget = self.max_bytes // 4
        counted_records: set[int] = set()
        kept: list[str] = []
        for cid in reversed(committed):
            if len(kept) >= self.retain_committed_entries:
                break
            cost = self._pending_bytes(state, [cid]) + len(cid) + 3
            index = record_of.get(cid)
            if index is not None and index not in counted_records:
                commit = state.commit_records[index]
                cost += len(_stable_json({**commit, "change_ids": []})) + 1
            if cost > budget:
                break
            budget -= cost
            if index is not None:
                counted_records.add(index)
            kept.append(cid)
        kept.reverse()
        return kept

    @staticmethod
    def _pending_bytes(state: _JournalState, ids: list[str]) -> int:
        return sum(len(_stable_json(state.changes[cid].as_dict())) + 1 for cid in ids)

    def _compact_locked(self) -> None:
        state = self._load_locked()
        pending = [cid for cid in state.order if state.changes[cid].status == "pending"]
        committed = [cid for cid in state.order if state.changes[cid].status != "pending" and not state.changes[cid].recovered]
        kept_committed = self._committed_within_budget(state, committed)
        records: list[dict[str, Any]] = []
        pending_budget = self.max_bytes // 2
        if len(pending) > 1 and self._pending_bytes(state, pending) > pending_budget:
            # Backups have been failing for a long time. Pending entries are never
            # dropped silently: the oldest are merged into one overflow change that
            # keeps a backup pending, so the file stays bounded.
            keep_newest: list[str] = []
            budget = pending_budget - 512
            for cid in reversed(pending):
                budget -= self._pending_bytes(state, [cid])
                if budget < 0:
                    break
                keep_newest.insert(0, cid)
            merged = [cid for cid in pending if cid not in set(keep_newest)]
            merged_count = sum(state.changes[cid].merged for cid in merged)
            first = state.changes[merged[0]]
            overflow = JournalEntry(
                change_id="chg_overflow_" + uuid.uuid4().hex[:24],
                at=first.at,
                action="journal.overflow",
                subject=f"{merged_count} older uncommitted changes merged",
                before_hash=first.before_hash,
                after_hash=state.changes[merged[-1]].after_hash,
                recovered=True,
                merged=merged_count,
            )
            logger.warning("Config change journal over budget: merged %d uncommitted changes", len(merged))
            state.changes[overflow.change_id] = overflow
            state.order = [overflow.change_id] + [cid for cid in state.order if cid not in set(merged)]
            pending = [overflow.change_id, *keep_newest]
        kept = set(pending) | set(kept_committed)
        for cid in state.order:
            if cid not in kept:
                continue
            entry = state.changes[cid]
            record = {
                "v": JOURNAL_FORMAT_VERSION,
                "type": "change",
                "change_id": entry.change_id,
                "at": _format_time(entry.at),
                "action": entry.action,
                "subject": entry.subject,
                "before": entry.before_hash,
                "after": entry.after_hash,
            }
            if entry.recovered:
                record["recovered"] = True
            if entry.merged > 1:
                record["merged"] = entry.merged
            records.append(record)
        kept_committed_set = set(kept_committed)
        for commit in state.commit_records:
            ids = [cid for cid in commit.get("change_ids", []) if cid in kept_committed_set]
            if ids:
                records.append({**commit, "change_ids": ids})
        # The checkpoint goes last so it, not a retained older commit, sets the
        # last-backup hash when the compacted file is read back.
        records.append(
            {
                "v": JOURNAL_FORMAT_VERSION,
                "type": "checkpoint",
                "at": _format_time(self._utcnow()),
                "last_backup_hash": state.last_backup_hash,
                "last_backup_id": state.last_backup_id,
            }
        )
        payload = b"".join(_stable_json(record).encode("utf-8") + b"\n" for record in records)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp_path, self.path)
        except BaseException:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise
        _fsync_directory(self.path.parent)
        if len(payload) > self.max_bytes:
            logger.warning(
                "Config change journal is %d bytes after compaction (budget %d); %d entries are still uncommitted",
                len(payload),
                self.max_bytes,
                len(pending),
            )


def _recovered_entry(number: int, raw: bytes, *, torn: bool) -> JournalEntry:
    digest = hashlib.sha256(number.to_bytes(8, "big") + raw).hexdigest()[:24]
    return JournalEntry(
        change_id=f"chg_recovered_{digest}",
        at=datetime.fromtimestamp(0, timezone.utc),
        action="journal.recovered",
        subject=f"{'torn' if torn else 'unreadable'} journal line {number}",
        before_hash=None,
        after_hash=None,
        recovered=True,
    )


def _replace_status(entry: JournalEntry, outcome: CommitOutcome, backup_id: str | None) -> JournalEntry:
    return JournalEntry(
        change_id=entry.change_id,
        at=entry.at,
        action=entry.action,
        subject=entry.subject,
        before_hash=entry.before_hash,
        after_hash=entry.after_hash,
        status=outcome,
        backup_id=backup_id,
        recovered=entry.recovered,
        merged=entry.merged,
    )


def _positive_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("merged must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class CoalescerResult:
    status: Literal["backup", "noop", "idle", "not_due", "busy", "failed"]
    change_ids: tuple[str, ...] = ()
    config_hash: str | None = None
    backup_id: str | None = None
    record: Any = None
    error: str | None = None


def _artifact_id(record: Any) -> str:
    value = record.get("artifact_id") if isinstance(record, Mapping) else getattr(record, "artifact_id", None)
    if not isinstance(value, str) or not _BACKUP_ID_PATTERN.fullmatch(value):
        raise ValueError("make_backup must return a record with an artifact_id of at most 128 safe characters")
    return value


class ConfigBackupCoalescer:
    """Turn journal entries into at most one config backup per real change set.

    ``tick()`` is meant to be called periodically by the backup sidecar (a few
    seconds apart); ``record_change()`` is the in-process shortcut that appends
    and restarts the quiet period immediately. Entries appended by other
    processes are noticed by the next ``poll()``.

    Timing uses the injected monotonic ``clock``. A backup is due when entries are
    pending and either ``quiet_period`` has passed since the newest change was
    seen or ``max_delay`` has passed since the oldest pending change was seen. A
    failed ``make_backup`` keeps the entries pending and retries with exponential
    backoff (starting at ``quiet_period``, capped at ``max_delay``).

    Single flight: one run at a time per process (non-blocking thread lock) and
    per journal across processes (non-blocking ``flock``); a concurrent trigger
    returns ``busy`` instead of making a second backup.
    """

    def __init__(
        self,
        journal: ChangeJournal,
        *,
        snapshot_config: Callable[[], Mapping[str, Any]],
        make_backup: Callable[[tuple[str, ...]], Any],
        clock: Callable[[], float] = time.monotonic,
        quiet_period: float = DEFAULT_QUIET_PERIOD_SECONDS,
        max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
        volatile_keys: frozenset[str] = DEFAULT_VOLATILE_KEYS,
    ) -> None:
        if quiet_period < 0 or max_delay <= 0 or max_delay < quiet_period:
            raise ValueError("need 0 <= quiet_period <= max_delay and max_delay > 0")
        self.journal = journal
        self._snapshot_config = snapshot_config
        self._make_backup = make_backup
        self._clock = clock
        self.quiet_period = float(quiet_period)
        self.max_delay = float(max_delay)
        self.volatile_keys = volatile_keys
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._run_lock_path = journal.path.with_name(journal.path.name + ".backup.lock")
        self._seen: set[str] = set()
        self._first_seen_at: float | None = None
        self._last_seen_at: float | None = None
        self._retry_at: float | None = None
        self._failures = 0
        self._fingerprint: tuple[int, int, int] | None | bool = False
        self.last_error: str | None = None
        # Entries left uncommitted by a previous run (crash, restart, failed
        # backup) start a quiet period now, so they are backed up soon.
        self.poll()

    # -- change intake --------------------------------------------------------------------

    def record_change(
        self,
        action: str,
        subject: str = "",
        *,
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> JournalEntry:
        entry = self.journal.append(action, subject, before_hash=before_hash, after_hash=after_hash)
        with self._state_lock:
            self._note_new_ids_locked({entry.change_id})
        return entry

    def poll(self) -> int:
        """Pick up entries appended by other processes; returns how many are pending."""

        fingerprint = self.journal.fingerprint()
        with self._state_lock:
            if fingerprint is not None and fingerprint == self._fingerprint:
                return len(self._seen)
        pending_ids = {entry.change_id for entry in self.journal.pending()}
        with self._state_lock:
            self._fingerprint = fingerprint
            self._seen &= pending_ids
            self._note_new_ids_locked(pending_ids - self._seen)
            if not self._seen:
                self._reset_timers_locked()
            return len(self._seen)

    def _note_new_ids_locked(self, ids: set[str]) -> None:
        if not ids:
            return
        now = self._clock()
        self._seen |= ids
        self._last_seen_at = now
        if self._first_seen_at is None:
            self._first_seen_at = now

    def _reset_timers_locked(self) -> None:
        self._first_seen_at = None
        self._last_seen_at = None
        self._retry_at = None
        self._failures = 0

    # -- scheduling -----------------------------------------------------------------------

    def seconds_until_due(self) -> float | None:
        """``None`` when nothing is pending, else seconds until a run is due (>= 0)."""

        with self._state_lock:
            if not self._seen or self._first_seen_at is None or self._last_seen_at is None:
                return None
            due_at = min(self._last_seen_at + self.quiet_period, self._first_seen_at + self.max_delay)
            if self._retry_at is not None:
                due_at = max(due_at, self._retry_at)
            return max(0.0, due_at - self._clock())

    def due(self) -> bool:
        remaining = self.seconds_until_due()
        return remaining is not None and remaining <= 0

    def tick(self) -> CoalescerResult:
        """Poll the journal and run a backup if one is due."""

        self.poll()
        if not self._seen:
            return CoalescerResult(status="idle")
        if not self.due():
            return CoalescerResult(status="not_due")
        return self.run()

    # -- the backup run -------------------------------------------------------------------

    def run(self) -> CoalescerResult:
        """Evaluate pending entries now, ignoring the debounce (also used on shutdown)."""

        if not self._run_lock.acquire(blocking=False):
            return CoalescerResult(status="busy")
        try:
            fd = os.open(self._run_lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return CoalescerResult(status="busy")
                try:
                    return self._run_locked()
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            self._run_lock.release()

    def _run_locked(self) -> CoalescerResult:
        # Read pending entries *before* the snapshot: callers journal after the
        # write is on disk, so the snapshot contains every entry committed here.
        pending = self.journal.pending()
        if not pending:
            with self._state_lock:
                self._seen.clear()
                self._reset_timers_locked()
            return CoalescerResult(status="idle")
        change_ids = tuple(entry.change_id for entry in pending)
        try:
            config_hash = canonical_config_hash(self._snapshot_config(), self.volatile_keys)
        except Exception as exc:  # the snapshot callback is integration code
            return self._record_failure(change_ids, f"config snapshot failed: {type(exc).__name__}")
        last_hash, _ = self.journal.last_backup()
        if config_hash == last_hash:
            try:
                self.journal.commit(change_ids, outcome="noop", config_hash=config_hash)
            except (OSError, JournalError) as exc:
                return self._record_failure(change_ids, f"journal commit failed: {type(exc).__name__}")
            self._record_success(change_ids)
            return CoalescerResult(status="noop", change_ids=change_ids, config_hash=config_hash)
        try:
            record = self._make_backup(change_ids)
            backup_id = _artifact_id(record)
        except Exception as exc:  # the backup callback is integration code
            return self._record_failure(change_ids, f"config backup failed: {type(exc).__name__}")
        try:
            self.journal.commit(change_ids, outcome="backup", config_hash=config_hash, backup_id=backup_id)
        except (OSError, JournalError) as exc:
            # The backup exists but the journal could not say so; the entries stay
            # pending and a retry may take one extra (identical) backup.
            result = self._record_failure(change_ids, f"journal commit failed: {type(exc).__name__}")
            return CoalescerResult(
                status="failed", change_ids=change_ids, config_hash=config_hash,
                backup_id=backup_id, record=record, error=result.error,
            )
        self._record_success(change_ids)
        return CoalescerResult(
            status="backup",
            change_ids=change_ids,
            config_hash=config_hash,
            backup_id=backup_id,
            record=record,
        )

    def _record_success(self, change_ids: tuple[str, ...]) -> None:
        with self._state_lock:
            self.last_error = None
            self._seen -= set(change_ids)
            self._failures = 0
            self._retry_at = None
            if not self._seen:
                self._reset_timers_locked()
            else:
                # Changes that arrived during the run start their own window.
                now = self._clock()
                self._first_seen_at = now
                self._last_seen_at = now
        # Pick up changes other processes appended while the backup ran, so their
        # quiet period starts now rather than at the next tick.
        self.poll()

    def _record_failure(self, change_ids: tuple[str, ...], error: str) -> CoalescerResult:
        logger.warning("Config backup attempt failed; %d changes stay pending: %s", len(change_ids), error)
        with self._state_lock:
            self.last_error = error
            self._failures += 1
            exponent = min(self._failures - 1, MAX_RETRY_EXPONENT)
            delay = min(self.max_delay, max(self.quiet_period, 1.0) * (2**exponent))
            self._retry_at = self._clock() + delay
        return CoalescerResult(status="failed", change_ids=change_ids, error=error)


__all__ = [
    "DEFAULT_MAX_DELAY_SECONDS",
    "DEFAULT_QUIET_PERIOD_SECONDS",
    "DEFAULT_VOLATILE_KEYS",
    "ChangeJournal",
    "CoalescerResult",
    "ConfigBackupCoalescer",
    "JournalEntry",
    "JournalError",
    "JournalStats",
    "canonical_config_hash",
    "canonicalize_config",
    "content_sha256",
]
