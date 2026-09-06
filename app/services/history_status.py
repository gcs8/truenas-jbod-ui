from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
    return projected
