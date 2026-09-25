"""Fixed-vocabulary diagnostics for the history collector.

The history dashboard is read by operators who cannot open a container log, so
every failure it shows has to be a sentence they can act on. Raw exception text
is not safe to publish: it carries URLs, filesystem paths and appliance replies.
This module maps failures onto a closed vocabulary of kinds, each with one fixed
sentence plus an optional bounded detail built only from safe scalars (a status
code, a timeout, an exception class name). The raw text stays in the logs.
"""

from __future__ import annotations

import errno

MAX_DIAGNOSTIC_SUMMARY_CHARS = 160

UNEXPECTED_COLLECTION_SUMMARY = "Unexpected collector error; see the service logs."
UNEXPECTED_RETENTION_SUMMARY = "Unexpected retention error; see the service logs."
UNEXPECTED_BACKUP_SUMMARY = "Unexpected backup error; see the service logs."

# Retention guard vocabulary. Retention must never wait on a failing backup
# forever, so it either waits with a published deadline or says it went ahead.
RETENTION_SKIP_WAITING_FOR_BACKUP = (
    "Waiting for a successful database backup before pruning."
)
RETENTION_RAN_WITHOUT_BACKUP = (
    "Pruned without a recent database backup because backups are failing."
)
# The wait anchor is durable state. If it cannot be read or written, the bound
# on the wait cannot be honoured, so pruning stops rather than guessing.
RETENTION_SKIP_ANCHOR_UNAVAILABLE = (
    "Not pruning: the retention wait record could not be read or written."
)

SOURCE_FAILURE_SENTENCES: dict[str, str] = {
    "source_unreachable": "Could not reach the main UI service.",
    "source_timeout": "The main UI did not answer within the request timeout.",
    "source_rejected": "The main UI rejected the request.",
    "source_bad_payload": "The main UI returned a response the collector could not read.",
    "source_error_reply": "The main UI reported an error for this request.",
}

RETENTION_FAILURE_SENTENCES: dict[str, str] = {
    "retention_batch_too_large": (
        "Retention batch size is too large; lower HISTORY_RETENTION_BATCH_SIZE."
    ),
    "database_read_only": "The history database is read-only.",
    "disk_full": "The disk holding the history database is full.",
    "disk_io_error": "The history database reported a disk I/O error.",
    "database_locked": "The history database was locked by another writer.",
    "permission_denied": "The history service may not write its database directory.",
    "missing_database": "The history database file is missing.",
    "backup_status_mode": (
        "The scheduled backup status file has unsafe permissions (expected 0640); "
        "segmented cleanup waits until it is fixed."
    ),
}

# Ordered most specific first; matched against the lowercased exception text.
_RETENTION_MESSAGE_RULES: tuple[tuple[str, str], ...] = (
    ("too many sql variables", "retention_batch_too_large"),
    ("readonly database", "database_read_only"),
    ("read-only database", "database_read_only"),
    ("attempt to write a readonly database", "database_read_only"),
    ("database or disk is full", "disk_full"),
    ("no space left on device", "disk_full"),
    ("disk i/o error", "disk_io_error"),
    ("database is locked", "database_locked"),
    ("permission denied", "permission_denied"),
    ("unable to open database file", "missing_database"),
)

BACKUP_FAILURE_SENTENCES: dict[str, str] = {
    "database_read_only": "The history database is read-only.",
    "disk_full": "The disk holding the history backups is full.",
    "disk_io_error": "The history database reported a disk I/O error.",
    "database_locked": "The history database was locked by another writer.",
    "permission_denied": "The history service may not write the backup directory.",
    "missing_database": "The history backup directory is missing.",
    "retention_batch_too_large": "The backup could not be written in one statement.",
}

_RETENTION_ERRNO_KINDS: dict[int, str] = {
    errno.ENOSPC: "disk_full",
    errno.EDQUOT: "disk_full",
    errno.EIO: "disk_io_error",
    errno.EACCES: "permission_denied",
    errno.EPERM: "permission_denied",
    errno.EROFS: "database_read_only",
    errno.ENOENT: "missing_database",
}


def _bounded(summary: str) -> str:
    if len(summary) <= MAX_DIAGNOSTIC_SUMMARY_CHARS:
        return summary
    return summary[: MAX_DIAGNOSTIC_SUMMARY_CHARS - 1].rstrip() + "…"


def _with_detail(sentence: str, detail: str | None) -> str:
    return _bounded(f"{sentence} ({detail})" if detail else sentence)


class HistorySourceError(RuntimeError):
    """A main-UI request failure that already knows how to describe itself."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        detail: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        # The HTTP status of a rejected request, so callers can tell a route
        # the main UI does not have (404/405) from a real failure.
        self.status_code = status_code
        self.summary = _with_detail(
            SOURCE_FAILURE_SENTENCES.get(kind, UNEXPECTED_COLLECTION_SUMMARY),
            detail,
        )

    @classmethod
    def unreachable(cls, message: str) -> "HistorySourceError":
        return cls(message, kind="source_unreachable")

    @classmethod
    def timeout(cls, message: str, *, timeout_seconds: int | float) -> "HistorySourceError":
        return cls(message, kind="source_timeout", detail=f"timeout {int(timeout_seconds)} s")

    @classmethod
    def rejected(cls, message: str, *, status_code: int) -> "HistorySourceError":
        return cls(
            message,
            kind="source_rejected",
            detail=f"HTTP {int(status_code)}",
            status_code=int(status_code),
        )

    @classmethod
    def bad_payload(cls, message: str) -> "HistorySourceError":
        return cls(message, kind="source_bad_payload")

    @classmethod
    def error_reply(cls, message: str) -> "HistorySourceError":
        return cls(message, kind="source_error_reply")


def classify_collection_failure(exc: BaseException) -> tuple[str, str]:
    """Return the fixed (kind, sentence) pair for a collection failure."""

    if isinstance(exc, HistorySourceError):
        return exc.kind, exc.summary
    return "unexpected", _with_detail(UNEXPECTED_COLLECTION_SUMMARY, type(exc).__name__)


def _classify_storage_failure(
    exc: BaseException,
    sentences: dict[str, str],
    fallback: str,
) -> tuple[str, str]:
    kind: str | None = None
    raw_errno = getattr(exc, "errno", None)
    if isinstance(raw_errno, int):
        kind = _RETENTION_ERRNO_KINDS.get(raw_errno)
    if kind is None:
        message = str(exc).lower()
        for needle, candidate in _RETENTION_MESSAGE_RULES:
            if needle in message:
                kind = candidate
                break
    detail = type(exc).__name__
    if kind is None or kind not in sentences:
        return "unexpected", _with_detail(fallback, detail)
    return kind, _with_detail(sentences[kind], detail)


def classify_retention_failure(exc: BaseException) -> tuple[str, str]:
    """Return the fixed (kind, sentence) pair for a retention failure."""

    return _classify_storage_failure(
        exc,
        RETENTION_FAILURE_SENTENCES,
        UNEXPECTED_RETENTION_SUMMARY,
    )


def classify_backup_failure(exc: BaseException) -> tuple[str, str]:
    """Return the fixed (kind, sentence) pair for a backup snapshot failure."""

    return _classify_storage_failure(
        exc,
        BACKUP_FAILURE_SENTENCES,
        UNEXPECTED_BACKUP_SUMMARY,
    )
