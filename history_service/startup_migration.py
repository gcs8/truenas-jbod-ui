"""Finish an interrupted history migration during ordinary startup.

A supported upgrade is meant to be three commands: pin ``JBOD_UI_IMAGE``,
``docker compose pull``, ``docker compose up -d``.  Required durable-state
migrations have to complete automatically and idempotently inside that, without
the operator running migration commands by hand.

``HistoryStore`` already applies its own schema migrations when it opens the
database, and it refuses to touch the hot file while a segmented-history
lifecycle marker is pending -- correctly, because a half-applied journal must
not be mutated further.  That refusal raised out of module import, so a
container interrupted mid-migration (a host reboot, an OOM kill, a
``docker compose up -d`` during a migration) came back up in a restart loop
printing a traceback, and the only way forward was a manual recovery command:
exactly the partially applied manual repair a normal upgrade must never need.

This module closes that gap.  Before the store is opened, a pending migration
marker is recovered once, in place, through the same bounded and idempotent
``recover_pending_migration`` the maintenance script uses.  If recovery cannot
complete, startup fails closed with one concise operator line -- reported
through ``/healthz`` instead of a crash loop -- and the durable state is left
exactly as recovery found it.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, TypeVar

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
    path_entry_exists,
)
from history_service.startup import HistoryStartupError

__all__ = [
    "ACTIVATION_PENDING_REASON",
    "MIGRATION_RECOVERY_FAILED_REASON",
    "open_history_store_after_recovery",
    "pending_history_lifecycle_marker",
    "pending_migration_marker_path",
    "recover_pending_history_migration",
]

logger = logging.getLogger(__name__)

# Concise, single-line operator reasons.  They never carry a traceback, a
# database row or a credential; the exception detail goes to the service log.
ACTIVATION_PENDING_REASON = (
    "Segmented history activation is pending from an interrupted rotation or restore. "
    "History is stopped to protect the database. Recover it with "
    "scripts/rotate_segmented_history.py --recover, then start the stack again."
)
MIGRATION_RECOVERY_FAILED_REASON = (
    "An interrupted segmented history migration could not be recovered automatically. "
    "History is stopped and the database was left as recovery found it; see the service "
    "log for the recovery error."
)

RecoverCallable = Callable[[Path, Path], Any]
StoreT = TypeVar("StoreT")


def _default_recover(source: Path, segments_directory: Path) -> Any:
    # Imported lazily: the migration path needs POSIX file locking, which must
    # not be a prerequisite for importing this module.
    from history_service.segment_migration import recover_pending_migration

    return recover_pending_migration(
        source=source,
        segments_directory=segments_directory,
        apply=True,
    )


def pending_migration_marker_path(segment_catalog_path: str | Path | None) -> Path | None:
    """Where an interrupted migration leaves its marker, if segments are configured."""
    if not segment_catalog_path:
        return None
    return Path(segment_catalog_path).absolute().parent / MIGRATION_PENDING_MARKER


def pending_history_lifecycle_marker(
    *,
    sqlite_path: str | Path,
    segment_catalog_path: str | Path | None,
) -> bool:
    """Whether a rotation, restore or migration marker is waiting on disk.

    A pure existence check: it opens nothing and writes nothing.
    """
    if path_entry_exists(activation_pending_path(sqlite_path)):
        return True
    marker_path = pending_migration_marker_path(segment_catalog_path)
    return marker_path is not None and path_entry_exists(marker_path)


def open_history_store_after_recovery(
    *,
    sqlite_path: str | Path,
    segment_catalog_path: str | Path | None,
    build_store: Callable[[], StoreT],
    recover: RecoverCallable | None = None,
) -> StoreT:
    """Open the history store, recovering a migration only if that is what blocks it.

    Recovery is a write, and the store's admission checks are what decide
    whether this build may write to this database at all -- most importantly the
    on-disk schema-version gate, which refuses a database a newer release wrote
    and leaves it byte-identical (#416). Running recovery first would mutate the
    durable state of a database this build has already been told not to touch,
    so the store is asked first and recovery only answers the one refusal it can
    actually fix.

    That refusal is the pending-lifecycle-marker check, which raises
    ``sqlite3.OperationalError`` from inside the migration lock, before the store
    reads or writes the database. Every other refusal -- an unsupported schema
    version, an unwritable directory, a corrupt file -- propagates untouched and
    recovery never runs. With no marker on disk there is nothing to recover, so
    the store is simply opened.
    """
    if not pending_history_lifecycle_marker(
        sqlite_path=sqlite_path,
        segment_catalog_path=segment_catalog_path,
    ):
        return build_store()

    try:
        return build_store()
    except sqlite3.OperationalError as exc:
        # Only the store's explicit lifecycle-marker refusal authorizes
        # recovery. Lock contention and every other operational error remain
        # retryable/diagnostic and must propagate unchanged.
        if "migration recovery is pending" not in str(exc).lower():
            raise
        logger.warning(
            "The history store refused a pending segmented history lifecycle marker; "
            "attempting recovery before starting."
        )

    failure = recover_pending_history_migration(
        sqlite_path=sqlite_path,
        segment_catalog_path=segment_catalog_path,
        recover=recover,
    )
    if failure is not None:
        raise HistoryStartupError(failure)
    return build_store()


def recover_pending_history_migration(
    *,
    sqlite_path: str | Path,
    segment_catalog_path: str | Path | None,
    recover: RecoverCallable | None = None,
) -> str | None:
    """Return ``None`` when startup may proceed, or one concise failure reason.

    Recovery runs at most once per start and only when a marker is actually
    present, so an ordinary startup -- and a retried startup after a successful
    recovery -- does no migration work at all.
    """
    if path_entry_exists(activation_pending_path(sqlite_path)):
        # A pending rotation or restore is a different lifecycle operation with
        # its own recovery; fail closed rather than guess at it.
        logger.error("%s", ACTIVATION_PENDING_REASON)
        return ACTIVATION_PENDING_REASON

    marker_path = pending_migration_marker_path(segment_catalog_path)
    if marker_path is None or not path_entry_exists(marker_path):
        return None

    logger.warning(
        "Recovering an interrupted segmented history migration before startup continues."
    )
    try:
        receipt = (recover or _default_recover)(Path(sqlite_path).absolute(), marker_path.parent)
    except Exception:
        logger.exception("Segmented history migration recovery failed.")
        return MIGRATION_RECOVERY_FAILED_REASON

    logger.info(
        "Segmented history migration recovery completed: %s",
        (receipt or {}).get("recovery_state") if isinstance(receipt, dict) else "completed",
    )
    return None
