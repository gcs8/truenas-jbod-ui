"""Open the history database without crash-looping on an unwritable directory.

A bind mount Docker created as root:root makes `HistoryStore.__init__` raise
PermissionError at import, and `restart: unless-stopped` then loops the
container with a bare traceback. This module retries a bounded number of times
(an operator fixing ownership while the container comes up is the common case)
and then fails once with the path, the owner and the command to run, recording
the same reason for `/healthz` and for anything else that wants to report it.

The caller decides what a terminal failure means. `history_service.main` keeps
the process up and serves the recorded reason, because restarting does not
change a directory's ownership: a rethrow here would only repeat the retries
and the traceback under `restart: unless-stopped`.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, TypeVar

from app.services.storage_writability import (
    describe_unwritable_directory,
    is_unwritable_error,
)

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 2.0

logger = logging.getLogger(__name__)

_startup_failure_reason: str | None = None


class HistoryStartupError(RuntimeError):
    """The history database could not be opened; `reason` is the operator line."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def recorded_startup_failure() -> str | None:
    """The reason the last startup attempt failed, or None if it succeeded."""
    return _startup_failure_reason


def _record(reason: str | None) -> None:
    global _startup_failure_reason
    _startup_failure_reason = reason


def _resolve_directory(directory: Path | str | Callable[[], Path | str]) -> Path | str:
    """Resolve a directory that may only be knowable after settings load."""
    if callable(directory):
        try:
            return directory()
        except Exception:  # pragma: no cover - the reason must survive this
            return "the history directory"
    return directory


def open_history_store_with_retries(
    factory: Callable[[], T],
    *,
    directory: Path | str | Callable[[], Path | str],
    attempts: int = DEFAULT_ATTEMPTS,
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `factory`, retrying only permission failures, then fail with a reason.

    Anything that is not a permission failure is raised unchanged on the first
    attempt: a pending lifecycle marker or a corrupt database is not something a
    retry can fix.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    backoff = initial_backoff_seconds
    for attempt in range(1, attempts + 1):
        try:
            store = factory()
        except Exception as exc:
            if not is_unwritable_error(exc):
                _record(None)
                raise
            reason = describe_unwritable_directory(_resolve_directory(directory))
            if attempt >= attempts:
                _record(reason)
                logger.error("%s", reason)
                raise HistoryStartupError(reason) from exc
            logger.warning("%s Retrying in %.1fs.", reason, backoff)
            sleep(backoff)
            backoff *= 2
        else:
            _record(None)
            return store
    raise AssertionError("unreachable")
