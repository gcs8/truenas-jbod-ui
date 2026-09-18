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
from pathlib import Path
from typing import Any, Callable

from history_service.segment_catalog import (
    MIGRATION_PENDING_MARKER,
    activation_pending_path,
    path_entry_exists,
)

__all__ = [
    "ACTIVATION_PENDING_REASON",
    "MIGRATION_RECOVERY_FAILED_REASON",
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
