"""Admit a repeated log line once, then count its repeats (#1171).

A background refresh repeats the same advisory every few seconds. Logging each
one buries real failures, so callers ask ``RepeatedLogLimiter.admit`` before
logging: the first line for a key logs, identical repeats are counted, and the
next admitted line (state change or, optionally, the repeat interval) carries
the count with the first and last suppressed times.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SuppressedRepeats:
    """Identical lines withheld since this key last logged."""

    count: int = 0
    first: float | None = None
    last: float | None = None

    def suffix(self) -> str:
        if not self.count:
            return ""
        return (
            f" ({self.count} identical {'message' if self.count == 1 else 'messages'} suppressed, "
            f"first {_utc(self.first)}, last {_utc(self.last)})"
        )


def _utc(timestamp: float | None) -> str:
    if timestamp is None:
        return "unknown"
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: Hashable) -> bytes:
    return hashlib.blake2b(repr(value).encode("utf-8", "backslashreplace"), digest_size=16).digest()


@dataclass
class _Entry:
    state: bytes
    emitted_at: float
    seen_at: float
    count: int = 0
    first: float | None = None
    last: float | None = None


class RepeatedLogLimiter:
    """Thread-safe, bounded admission for repeated log lines.

    ``admit(key, state)`` returns ``None`` when the line should be withheld,
    or a ``SuppressedRepeats`` describing the lines withheld before it. A line
    is admitted the first time its key is seen, whenever its state differs from
    the last admitted state, and, when ``repeat_interval_seconds`` is set, once
    that interval has passed since the key last logged. ``forget(key)`` drops a
    key whose condition cleared, so its next occurrence logs again. Idle keys
    expire on the next use after ``max_idle_seconds``. The oldest keys are
    forgotten beyond ``max_keys``; a forgotten key simply logs again.

    Keys and states can carry remote-supplied text (device names, commands),
    so only fixed-size digests of them are retained: memory stays bounded by
    ``max_keys`` regardless of how long the identifiers are.
    """

    def __init__(
        self,
        *,
        repeat_interval_seconds: float | None,
        max_keys: int = 4096,
        max_idle_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.repeat_interval_seconds = repeat_interval_seconds
        self.max_keys = max(1, int(max_keys))
        self.max_idle_seconds = max(1.0, float(max_idle_seconds))
        self._clock = clock
        self._wall_clock = wall_clock
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        # Entries are ordered by their last observation. Expire lazily on use,
        # without a background thread or scanning every active key.
        while self._entries:
            if now - next(iter(self._entries.values())).seen_at < self.max_idle_seconds:
                break
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._entries)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()

    def forget(self, key: Hashable) -> None:
        digest = _digest(key)
        with self._lock:
            self._entries.pop(digest, None)

    def admit(self, key: Hashable, state: Hashable = None) -> SuppressedRepeats | None:
        key = _digest(key)
        state = _digest(state)
        with self._lock:
            now = self._clock()
            self._expire(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                entry.seen_at = now
                interval = self.repeat_interval_seconds
                due = interval is not None and now - entry.emitted_at >= interval
                if entry.state == state and not due:
                    wall = self._wall_clock()
                    entry.count += 1
                    entry.first = wall if entry.first is None else entry.first
                    entry.last = wall
                    return None
                summary = SuppressedRepeats(entry.count, entry.first, entry.last)
            else:
                summary = SuppressedRepeats()
            self._entries[key] = _Entry(state=state, emitted_at=now, seen_at=now)
            while len(self._entries) > self.max_keys:
                self._entries.popitem(last=False)
            return summary
