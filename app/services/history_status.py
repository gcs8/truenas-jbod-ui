from __future__ import annotations

from collections.abc import Mapping
from typing import Any


HISTORY_RECOVERY_DETAIL = "History recovery is required; collection is paused."
PUBLIC_RECOVERY_STATES = ("required", "invalid", "unavailable")
MAX_RECOVERY_STATUS_BYTES = 8192


def project_public_recovery_status(status: object) -> dict[str, Any]:
    """Observe a pause, never approve recovery or copy journal metadata."""
    if not isinstance(status, Mapping) or status.get("recovery_required") is not True:
        return {}
    state = status.get("recovery_state")
    return {
        "recovery_required": True,
        "ready": False,
        "collection_paused": True,
        "recovery_state": state if isinstance(state, str) and state in PUBLIC_RECOVERY_STATES else "unavailable",
    }


def project_public_history_status(status: object, *, last_error_detail: str) -> dict[str, Any]:
    """Keep the optional-backend status shape and fail closed on a recovery pause."""
    if not isinstance(status, Mapping):
        return {}
    collector = project_public_collector_status(status.get("collector"), last_error_detail=last_error_detail)
    recovery = project_public_recovery_status(status) or project_public_recovery_status(collector)
    if recovery:
        return {
            "configured": status.get("configured") is True,
            "available": False,
            "status": "recovery_required",
            "detail": HISTORY_RECOVERY_DETAIL,
            "counts": {},
            "scopes": [],
            "collector": dict(recovery),
            **recovery,
        }
    return {
        **{field: status[field] for field in ("configured", "available", "detail", "counts", "scopes") if field in status},
        "collector": collector,
    }


PUBLIC_COLLECTOR_STATUS_FIELDS = (
    "collector_running",
    "collection_running",
    "collection_kind",
    "collection_activity",
    "collection_elapsed_seconds",
    "last_collection_inventory_forced",
    "last_collection_duration_seconds",
    "last_background_overrun_seconds",
    "background_consecutive_failures",
    "background_backoff_until",
    "background_backoff_seconds_remaining",
    "next_collection_at",
    "last_inventory_at",
    "last_fast_metrics_at",
    "last_slow_metrics_at",
    "last_success_at",
    "last_completed_at",
    "last_backup_at",
    "last_retention_at",
    "last_retention_duration_seconds",
    "last_retention_rows_removed",
    "last_retention_has_more",
    "last_retention_error",
    "last_error",
)


def project_public_collector_status(
    status: object,
    *,
    last_error_detail: str,
) -> dict[str, Any]:
    """Select the stable public collector status fields from an internal status."""

    if not isinstance(status, Mapping):
        return {}
    projected = {
        field: status[field]
        for field in PUBLIC_COLLECTOR_STATUS_FIELDS
        if field in status
    }
    if projected.get("last_error"):
        projected["last_error"] = last_error_detail
    projected.update(project_public_recovery_status(status))
    return projected
