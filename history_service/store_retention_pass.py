"""Retention-pass orchestration behind the HistoryStore compatibility methods.

The store still owns state, connections, locks and transactions. Helpers use
its instance or class for sibling calls and store_module for module globals,
so existing replacements on history_service.store or HistoryStore still apply.
The SQL helpers and their released contract stay in store_retention.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from types import ModuleType
from typing import Any


def run_segmented_retention(
    store_module: ModuleType,
    self,
    backup_at: datetime | str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any] | None:
    with self.segmented_retention_write_lock():
        if not self.claim_segmented_retention_backup(
            backup_at,
            migration_lock_held=True,
        ):
            return None
        retention_completed = False
        try:
            result = operation()
            retention_completed = True
            self.finish_segmented_retention_backup(
                backup_at,
                has_more=bool(result.get("has_more")),
                migration_lock_held=True,
            )
            return result
        except BaseException:
            if not retention_completed:
                self.release_segmented_retention_backup(
                    backup_at,
                    migration_lock_held=True,
                )
            raise


def maintain_retention(
    store_module: ModuleType,
    self,
    *,
    now: datetime,
    raw_metric_retention_days: int,
    event_retention_days: int,
    hourly_rollup_retention_days: int,
    daily_rollup_retention_days: int,
    batch_size: int,
    max_batches: int,
    should_continue: Callable[[], bool] | None = None,
    migration_lock_held: bool = False,
) -> dict[str, Any]:
    retention_values = (
        raw_metric_retention_days,
        event_retention_days,
        hourly_rollup_retention_days,
        daily_rollup_retention_days,
    )
    if any(value < 0 for value in retention_values):
        raise ValueError("History retention days cannot be negative.")
    if batch_size < 1:
        raise ValueError("History retention batch size must be at least 1.")
    if max_batches < 1:
        raise ValueError("History retention max batches must be at least 1.")

    normalized_now = now if now.tzinfo else now.replace(tzinfo=store_module.timezone.utc)
    normalized_now = normalized_now.astimezone(store_module.timezone.utc)
    cutoffs = {
        "metric": self._retention_cutoff(normalized_now, raw_metric_retention_days),
        "event": self._retention_cutoff(normalized_now, event_retention_days),
        "hourly": self._retention_cutoff(normalized_now, hourly_rollup_retention_days),
        "daily": self._retention_cutoff(normalized_now, daily_rollup_retention_days),
    }
    summary: dict[str, Any] = {
        "metric_samples_removed": 0,
        "events_removed": 0,
        "hourly_rollups_removed": 0,
        "daily_rollups_removed": 0,
        "total_rows_removed": 0,
        "batches_completed": 0,
        "has_more": False,
        "interrupted": False,
    }

    # In-memory scan state (see _maintain_retention_batch). A pass that uses
    # its whole budget hands its unfinished floors on, so a kept set larger
    # than one pass can read is swept once, not from its start by every
    # pass (#1268). A row the sweep passed that has since become eligible
    # waits, like a row added behind a floor, for the next fresh pass.
    scan_floors = self._retention_carry
    self._retention_carry = {}
    finished_tables: set[str] = set()
    for _ in range(max_batches):
        if should_continue is not None and not should_continue():
            summary["interrupted"] = True
            break
        try:
            batch = self._execute_write(
                lambda connection: self._maintain_retention_batch(
                    connection,
                    cutoffs=cutoffs,
                    batch_size=batch_size,
                    scan_floors=scan_floors,
                    finished_tables=frozenset(finished_tables),
                ),
                migration_lock_held=migration_lock_held,
            )
        except Exception as exc:
            summary["has_more"] = True
            summary["total_rows_removed"] = self._retention_total_rows_removed(summary)
            setattr(exc, "retention_summary", dict(summary))
            raise
        summary["batches_completed"] += 1
        scan_floors.update(batch["scan_floors"])
        finished_tables.update(batch["finished_tables"])
        for key in (
            "metric_samples_removed",
            "events_removed",
            "hourly_rollups_removed",
            "daily_rollups_removed",
        ):
            summary[key] += int(batch[key])
        summary["has_more"] = bool(batch["has_more"])
        if not summary["has_more"]:
            break

    # A stopped pass hands nothing on: the service is shutting down.
    if summary["has_more"] and not summary["interrupted"]:
        self._retention_carry = {
            name: floor for name, floor in scan_floors.items() if name not in finished_tables
        }
    summary["total_rows_removed"] = self._retention_total_rows_removed(summary)
    summary["pages_reclaimed"] = 0
    if summary["total_rows_removed"] and not summary["interrupted"]:
        summary["pages_reclaimed"] = self._reclaim_free_pages(migration_lock_held=migration_lock_held)
    return summary


def _reclaim_free_pages(store_module: ModuleType, self, *, migration_lock_held: bool) -> int:
    """Hand pages freed by retention back to the filesystem and shrink the WAL.

        incremental_vacuum only does work on databases created in incremental mode;
        older databases keep reusing freed pages in place, exactly as before.
        """

    def operation(connection: sqlite3.Connection) -> int:
        before = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        if before:
            # Each sqlite3_step of incremental_vacuum frees one page, and
            # Connection.execute() steps only once; executescript() runs the
            # statement to completion, so every free page is returned.
            connection.executescript("PRAGMA incremental_vacuum;")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        after = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, before - after)

    try:
        return int(self._execute_write(operation, migration_lock_held=migration_lock_held))
    except store_module.sqlite3.Error as exc:
        store_module.logger.warning(
            "History retention could not return free pages to the filesystem this pass: %s",
            exc,
        )
        return 0


def _retention_total_rows_removed(store_module: ModuleType, summary: dict[str, Any]) -> int:
    return sum(
        int(summary[key])
        for key in (
            "metric_samples_removed",
            "events_removed",
            "hourly_rollups_removed",
            "daily_rollups_removed",
        )
    )


def _retention_cutoff(store_module: ModuleType, now: datetime, days: int) -> str | None:
    if days <= 0:
        return None
    return (now - store_module.timedelta(days=days)).isoformat()


def _maintain_retention_batch(
    store_module: ModuleType,
    cls,
    connection: sqlite3.Connection,
    *,
    cutoffs: dict[str, str | None],
    batch_size: int,
    scan_floors: dict[str, tuple[bytes | str, int]] | None = None,
    finished_tables: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    # One bounded page per table selects the batch for both rollups and
    # the delete, so the rows aggregated are exactly the rows removed.
    #
    # Work: rows whose text sorts at or after the cutoff are never visited.
    # Each table's page is the next candidates after its scan floor, in
    # index order, at most _RETENTION_PAGE_FACTOR times the batch size,
    # however many rows in the day before the cutoff the exact-time test
    # keeps (#1268). Two pieces of in-memory state, kept for one pass (and
    # carried to the next while work remains, see maintain_retention):
    # - a scan floor per table (the last row a batch read and did not
    #   leave for the next batch, kept rows included): the next batch
    #   starts after it;
    # - a finished set: a table whose candidates after its floor were all
    #   read with this cutoff has nothing eligible left, so it is not read
    #   again in this pass while other tables keep the pass running.
    # Both only narrow the scan. A row another writer adds behind the
    # floor, or in a finished table, waits for a later pass: a deletion
    # can be delayed, never advanced. A rollup this batch writes is known
    # to the pass, so when its bucket is already past that table's cutoff
    # the same batch deletes it by key, wherever it lies; has_more=false
    # therefore never leaves a row the pass wrote and could delete.
    cls._register_retention_functions(connection)
    floors = dict(scan_floors or {})
    finished = set(finished_tables)

    def active(name: str, cutoff: str | None) -> bool:
        return cutoff is not None and name not in finished

    def remove_page(name: str, table_name: str, timestamp_column: str, key_column: str,
                    cutoff: str, limit: int, **options: Any) -> int:
        removed, done = cls._remove_retention_page(
            connection,
            name=name,
            table_name=table_name,
            timestamp_column=timestamp_column,
            key_column=key_column,
            cutoff=cutoff,
            batch_size=limit,
            floors=floors,
            **options,
        )
        if done:
            finished.add(name)
        return removed

    metric_samples_removed = 0
    written_expired: dict[int, list[int]] = {3600: [], 86400: []}
    if active("metric_samples", cutoffs["metric"]):

        def roll_up(selected_rows: str, selected_parameters: tuple[Any, ...]) -> None:
            for bucket_seconds, kind in ((3600, "hourly"), (86400, "daily")):
                written_expired[bucket_seconds] = cls._roll_up_selected_rows(
                    connection,
                    selected_rows=selected_rows,
                    selected_parameters=selected_parameters,
                    bucket_seconds=bucket_seconds,
                    rollup_cutoff=cutoffs[kind],
                )

        metric_samples_removed = remove_page(
            "metric_samples", "metric_samples", "observed_at", "id",
            cutoffs["metric"], batch_size, roll_up=roll_up,
        )

    events_removed = 0
    if active("slot_events", cutoffs["event"]):
        events_removed = remove_page(
            "slot_events", "slot_events", "observed_at", "id", cutoffs["event"], batch_size,
        )
    rollups_removed = {}
    for bucket_seconds, kind in ((3600, "hourly"), (86400, "daily")):
        name = f"rollups_{bucket_seconds}"
        # At most one written rollup per raw row this batch selected, so
        # these never exceed the batch size; they count against it.
        removed = connection.executemany(
            "DELETE FROM metric_rollups WHERE rowid = ? AND bucket_seconds = ?",
            [(rowid, bucket_seconds) for rowid in written_expired[bucket_seconds]],
        ).rowcount if written_expired[bucket_seconds] else 0
        if active(name, cutoffs[kind]):
            # With no budget left the page is empty and only the one-row
            # check for candidates after the floor runs.
            removed += remove_page(
                name, "metric_rollups", "bucket_start", "rowid", cutoffs[kind], batch_size - removed,
                scope=("bucket_seconds = ? AND ", (bucket_seconds,)),
            )
        rollups_removed[bucket_seconds] = removed
    # A table not finished may hold eligible rows this batch did not read.
    has_more = any(
        active(name, cutoffs[kind])
        for name, kind in (
            ("metric_samples", "metric"),
            ("slot_events", "event"),
            ("rollups_3600", "hourly"),
            ("rollups_86400", "daily"),
        )
    )
    return {
        "metric_samples_removed": metric_samples_removed,
        "events_removed": events_removed,
        "hourly_rollups_removed": rollups_removed[3600],
        "daily_rollups_removed": rollups_removed[86400],
        "has_more": has_more,
        "scan_floors": floors,
        "finished_tables": frozenset(finished),
    }
